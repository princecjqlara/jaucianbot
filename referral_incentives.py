"""Recruiter attribution and daily shares from recorded, identifiable paid sales."""
from __future__ import annotations

import datetime as dt
import os
import re
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP

from cloud_store import activity_messages, enqueue_scheduled_action, receipt_messages, request
from daily_automation import GROUPS, MANILA, TRABAWHO, TRABAWHO_CHAT_ID, money, split_message
from deal_parser import PAGE_RE, client_name, collect_deals, normalized_text
from hourly_availability import read_records
from receipt_parser import collect_receipts

RECRUITS_CHAT = -1003555676168
SHARES_CHAT = -1004389276294
SHARES_THREAD = 2
RECRUITERS = ("Farah", "Athena", "Grace Canites", "Elle", "Shan", "Sen",
              "Pogi Michael", "Nami", "Melo", "Mark Joshua")
WORK_TEAMS = set(GROUPS) | {TRABAWHO_CHAT_ID}
RATE = Decimal("0.05")


def enabled() -> bool:
    return os.environ.get("REFERRAL_INCENTIVES_ENABLED", "").lower().strip() == "true"


def timestamp(value) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def start_date() -> dt.date:
    return dt.date.fromisoformat(os.environ["REFERRAL_START_DATE"].strip())


def insert_record(key: str, payload: dict, now: dt.datetime) -> bool:
    return bool(request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": RECRUITS_CHAT, "action_type": "message", "status": "cancelled",
        "dedupe_key": key, "scheduled_for": now.isoformat(), "payload": payload,
    }, prefer="resolution=ignore-duplicates,return=representation"))


def poll_context(poll_id: str) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{RECRUITS_CHAT}",
        "dedupe_key": "like.referral-poll:*", "payload->>referral_poll_id": f"eq.{poll_id}", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else None


def poll_registered(action_id: int) -> bool:
    return bool(request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "dedupe_key": f"eq.referral-poll:{action_id}", "limit": 1,
    })))


def register_poll(action: dict, message: dict, now: dt.datetime) -> bool:
    return insert_record(f"referral-poll:{action['id']}", {
        "referral_poll_id": message["poll"]["id"], "options": list(RECRUITERS),
        "telegram_message_id": message["message_id"],
    }, now) or poll_registered(int(action["id"]))


def save_answer(update: dict, allowed: set[int]) -> bool | None:
    if not enabled() or RECRUITS_CHAT not in allowed:
        return None
    answer = update.get("poll_answer") or {}
    context = poll_context(answer.get("poll_id", ""))
    if context is None:
        return None
    user = answer.get("user") or {}
    selected = answer.get("option_ids") or []
    if not user.get("id") or len(selected) > 1 or any(
        isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(context["options"]) for i in selected
    ):
        return False
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or str(user["id"])
    recruiter = context["options"][selected[0]] if selected else None
    if recruiter and name.casefold().strip() == recruiter.casefold():
        return False
    now = dt.datetime.now(dt.timezone.utc)
    key = f"referral-person:{user['id']}"
    payload = {"recruit_id": int(user["id"]), "recruit_name": name, "recruiter": recruiter,
               "update_id": int(update.get("update_id", 0)), "voted_at": now.isoformat()}
    if recruiter:
        payload["first_voted_at"] = now.isoformat()
    if insert_record(key, payload, now):
        return True
    for _ in range(4):
        rows = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id,payload,updated_at", "dedupe_key": f"eq.{key}", "limit": 1,
        })) or []
        if not rows:
            raise RuntimeError("Referral response record disappeared")
        row = rows[0]
        stored = row["payload"]
        if int(stored["update_id"]) >= payload["update_id"]:
            return True
        # Credit cannot silently switch after the first commissioned sale.
        if stored.get("locked_recruiter") and recruiter != stored["locked_recruiter"]:
            return True
        merged = {**stored, **payload}
        first_vote = stored.get("first_voted_at") or (stored.get("voted_at") if stored.get("recruiter") else None)
        if first_vote:
            merged["first_voted_at"] = first_vote
        changed = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id", "id": f"eq.{row['id']}", "updated_at": f"eq.{row['updated_at']}",
        }), {"payload": merged, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation")
        if changed:
            return True
    raise RuntimeError("Referral vote update conflicted")


def observe_join(update: dict, allowed: set[int], now: dt.datetime) -> None:
    if not enabled() or RECRUITS_CHAT not in allowed:
        return
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id not in WORK_TEAMS.intersection(allowed):
        return
    joined = dt.datetime.fromtimestamp(message.get("date") or now.timestamp(), dt.timezone.utc)
    for user in message.get("new_chat_members") or []:
        if user.get("is_bot") or not user.get("id"):
            continue
        insert_record(f"referral-join:{user['id']}:{chat_id}", {
            "recruit_id": int(user["id"]), "work_chat_id": chat_id, "joined_at": joined.isoformat(),
            "join_message_id": message.get("message_id"),
        }, now)


def lock_credit(referral: dict, now: dt.datetime) -> None:
    """Once a share is computed, later votes cannot move it to another recruiter."""
    key = f"referral-person:{referral['recruit_id']}"
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,payload,updated_at", "dedupe_key": f"eq.{key}", "limit": 1,
    })) or []
    if not rows:
        raise RuntimeError("Referral credit record disappeared")
    row = rows[0]
    stored = row["payload"]
    if stored.get("locked_recruiter") == referral["recruiter"]:
        return
    if stored.get("recruiter") != referral["recruiter"]:
        raise RuntimeError("Referral credit changed during report calculation")
    changed = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "id": f"eq.{row['id']}", "updated_at": f"eq.{row['updated_at']}",
    }), {"payload": dict(stored, locked_recruiter=referral["recruiter"]),
         "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation")
    if not changed:
        raise RuntimeError("Referral credit lock conflicted")


def _reference(row: dict, page: str) -> str | None:
    text = normalized_text(row.get("text") or "")
    if page == "Trabawho":
        pages = PAGE_RE.findall(text)
        if len(set(p.casefold().strip() for p in pages)) == 1:
            page += ":" + pages[0].casefold().strip()
    match = re.search(r"(?im)^\s*(?:order|invoice)(?:\s*(?:id|number|no\.?))?\s*[:#]\s*(\S+)\s*$", text)
    if match:
        return page.casefold() + ":order:" + match[1].casefold()
    # Explicit client labels avoid treating page names or amount lines as identities.
    match = re.search(r"(?im)^\s*(?:client|customer)(?:\s+name)?\s*:\s*(.+)$", text)
    if match:
        name = " ".join(match[1].split()).casefold()
    elif not page.startswith("Trabawho"):
        name = client_name(text)
    else:
        name = None
    return page.casefold() + ":client:" + name if name else None


def sales_from_rows(chat_id: int, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep tips/extras and unidentified receipts out of first-sale incentives."""
    sales, review = [], []
    if chat_id == TRABAWHO_CHAT_ID:
        # Order IDs contain digits, but are metadata rather than monetary lines.
        originals = {r["message_id"]: r for r in rows}
        captions = [dict(r, text=re.sub(
            r"(?im)^\s*(?:(?:order|invoice)(?:\s*(?:id|number|no\.?))?\s*[:#]|"
            r"(?:client|customer)(?:\s+name)?\s*:|page(?:\s+name)?\s*:)[^\n]*$",
            "", normalized_text(r.get("text") or "")
        )) for r in rows]
        parsed = collect_receipts(captions, TRABAWHO["receipts"])
        review.extend(entry["row"] for entry in parsed["review"])
        review.extend(parsed["unresolved_topic_rows"])
        for entry in parsed["entries"]:
            row = originals[entry["row"]["message_id"]]
            amounts = [line for line in entry["lines"] if line["kind"] != "tip"]
            reference = _reference(row, "Trabawho")
            if not reference or not row.get("author_id") or len(amounts) != 1 or re.search(
                r"\b(?:revision|revission|extra|dagdag|upgrade)\b", row.get("text") or "", re.I
            ):
                review.append(row)
                continue
            sales.append({"user_id": int(row["author_id"]), "at": timestamp(row["sent_utc"]),
                          "gross": amounts[0]["gross"], "earnings": amounts[0]["salary"],
                          "reference": reference, "chat_id": chat_id, "message_id": row["message_id"]})
    else:
        # Shared team summaries deduplicate by client/day. Recruiter incentives
        # instead use order IDs, so two genuine same-client orders remain distinct.
        entries = []
        for row in rows:
            parsed = collect_deals([row], GROUPS[chat_id])
            review.extend(parsed["uncertain_rows"])
            entries.extend(parsed["entries"])
        for entry in entries:
            if entry["kind"] != "dd":
                continue
            row = entry["row"]
            reference = _reference(row, entry["page"])
            if not reference or not row.get("author_id") or re.search(
                r"\b(?:unpaid|refund(?:ed)?|cancel(?:led|ed)?|not\s+paid|hindi\s+paid)\b",
                row.get("text") or "", re.I
            ):
                review.append(row)
                continue
            sales.append({"user_id": int(row["author_id"]), "at": timestamp(row["sent_utc"]),
                          "gross": entry["gross"], "earnings": None, "reference": reference,
                          "chat_id": chat_id, "message_id": row["message_id"]})
    return sales, review


def calculate(referrals: list[dict], joins: list[dict], sales: list[dict],
              work_date: dt.date, now: dt.datetime, basis: str, window_start: str) -> list[dict]:
    """Calculate shares as of the report, without guessing missing eligibility dates."""
    result = []
    owners = defaultdict(set)
    for sale in sales:
        if sale["at"] <= now:
            owners[sale["reference"]].add((sale["user_id"], sale["gross"]))
    for referral in referrals:
        if not referral.get("recruiter"):
            continue
        uid = int(referral["recruit_id"])
        seen, orders = set(), []
        for sale in sorted((s for s in sales if s["user_id"] == uid and s["at"] <= now
                            and len(owners[s["reference"]]) == 1),
                           key=lambda s: (s["at"], s["chat_id"], s["message_id"])):
            if sale["reference"] not in seen:
                seen.add(sale["reference"])
                orders.append(sale)
        joined = min((timestamp(j["joined_at"]) for j in joins if int(j["recruit_id"]) == uid), default=None)
        voted = referral.get("first_voted_at") or referral.get("voted_at")
        starts = [value for value in (joined, timestamp(voted) if voted else None) if value is not None]
        start = min(starts) if starts and window_start == "join_or_vote" else None
        eligible = [s for s in orders if start is not None and s["at"] >= start]
        bonus = len(eligible) >= 8 and eligible[7]["at"] <= start + dt.timedelta(hours=72)
        cap = 16 if bonus else 8
        selected = eligible[:cap]
        today = [s for s in selected if s["at"].astimezone(MANILA).date() == work_date]
        amount_key = "gross" if basis == "gross" else "earnings"
        pending = [s for s in selected if s.get(amount_key) is None]
        def share(orders):
            return sum(((s[amount_key] * RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        for s in orders if s.get(amount_key) is not None), Decimal("0"))
        result.append({**referral, "start": start, "bonus": bonus, "cap": cap,
                       "sales_count": len(eligible), "commissioned_sales": len(selected),
                       "today_sales": len(today), "today_share": share(today), "total_share": share(selected),
                       "pending_amounts": len(pending), "eligible": start is not None and basis in {"gross", "earnings"},
                       "evidence": [{"chat_id": s["chat_id"], "message_id": s["message_id"],
                                     "at": s["at"].isoformat()} for s in selected]})
    return result


def build_report(work_date: dt.date, now: dt.datetime, allowed: set[int]) -> str:
    referrals = [r["payload"] for r in read_records("referral-person:", {RECRUITS_CHAT})]
    joins = [r["payload"] for r in read_records("referral-join:", {RECRUITS_CHAT})]
    basis = os.environ.get("REFERRAL_COMMISSION_BASIS", "").strip()
    window = os.environ.get("REFERRAL_WINDOW_START", "").strip()
    report = [f"🌟 RECRUITER SHARES • {work_date:%B %d, %Y} (PHT)", "",
              "Hi team! Here are today's recorded referral shares. 💛"]
    if basis not in {"gross", "earnings"} or window != "join_or_vote":
        return "\n".join(report + ["Commission basis and the 3-day start rule are awaiting confirmation.",
                                  "No commission amounts have been assumed."])
    begin = dt.datetime.combine(start_date(), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    if joins:
        begin = min(begin, min(timestamp(j["joined_at"]) for j in joins))
    end = min(now, dt.datetime.combine(work_date + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc))
    sources = {}
    if referrals:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for chat_id in sorted(WORK_TEAMS.intersection(allowed)):
                if chat_id == TRABAWHO_CHAT_ID:
                    sources[chat_id] = pool.submit(receipt_messages, chat_id, begin, end)
                else:
                    sources[chat_id] = pool.submit(activity_messages, chat_id, begin, end, {GROUPS[chat_id]["done"]})
            all_sales, review = [], []
            for chat_id, future in sources.items():
                sales, held = sales_from_rows(chat_id, future.result())
                all_sales.extend(sales)
                review.extend(held)
    else:
        all_sales, review = [], []
    result = calculate(referrals, joins, all_sales, work_date, end, basis, window)
    owners = defaultdict(set)
    for sale in all_sales:
        owners[sale["reference"]].add((sale["user_id"], sale["gross"]))
    conflicts = [s for s in all_sales if len(owners[s["reference"]]) > 1]
    grouped = defaultdict(list)
    for recruit in result:
        if recruit["eligible"] and recruit["commissioned_sales"]:
            lock_credit(recruit, now)
        grouped[recruit["recruiter"]].append(recruit)
    report += [f"5% of {'sale amounts' if basis == 'gross' else 'recorded member earnings'} • first 8 sales",
               "8 more sales unlock when the first 8 finish within 72 hours of joining or voting.",
               "Neither the first 8 nor the unlocked bonus 8 expires.", ""]
    total = Decimal(0)
    for name in RECRUITERS:
        amount = sum((r["today_share"] for r in grouped[name] if r["eligible"]), Decimal(0))
        total += amount
        report.append(f"• {name}: {money(amount)} today • {len(grouped[name])} recruit(s)")
    report += ["", f"Total recorded shares today: {money(total)}", "", "BY RECRUIT"]
    for recruit in result:
        if not recruit["eligible"]:
            detail = "Team joining date or recruiter vote needs confirmation; commission is pending."
        else:
            detail = (f"{recruit['commissioned_sales']}/{recruit['cap']} commissioned sales • "
                      f"{money(recruit['today_share'])} today • {money(recruit['total_share'])} total"
                      + (" • bonus 8 unlocked 🌟" if recruit["bonus"] else "")
                      + (f" • {recruit['pending_amounts']} earnings amount(s) pending" if recruit["pending_amounts"] else ""))
        report.append(f"• {recruit['recruit_name']} → {recruit['recruiter']}: {detail}")
    if not result:
        report.append("No recruit-to-recruiter votes have been recorded yet. New members, please answer the recruiter poll.")
    linked = {int(r["recruit_id"]) for r in referrals if r.get("recruiter")}
    held = [r for r in review if r.get("author_id") in linked]
    if held:
        report += ["", f"Needs review: {len(held)} recorded sale/receipt post(s). Unverified amounts are held out of shares."]
    if conflicts:
        report += ["", f"Order details need review: {len(conflicts)} post(s) have conflicting sellers or amounts; shares are held."]
    report += ["", "Based on recorded paid client orders; payout confirmation is tracked separately.",
               "Thank you for helping our teams grow! 💛"]
    return "\n".join(report)


def prepare_report(action: dict, now: dt.datetime, allowed: set[int]) -> None:
    date = dt.date.fromisoformat(action["payload"]["referral_report_date"])
    chunks = split_message(build_report(date, now, allowed))
    action["payload"]["text"] = chunks[0]
    for part, text in enumerate(chunks[1:], start=1):
        enqueue_scheduled_action(chat_id=SHARES_CHAT, action_type="message",
            payload={"text": text, "message_thread_id": SHARES_THREAD, "disable_notification": False},
            scheduled_for=now, dedupe_key=f"referral-report:{date.isoformat()}:part-{part}")


def queue_automation(now: dt.datetime, allowed: set[int]) -> int:
    if not enabled() or RECRUITS_CHAT not in allowed:
        return 0
    queued = int(enqueue_scheduled_action(chat_id=RECRUITS_CHAT, action_type="poll",
        payload={"question": "Who invited you to join the team? 👋\nChoose your recruiter so we can credit their referral incentive.",
                 "options": list(RECRUITERS), "is_anonymous": False, "allows_multiple_answers": False,
                 "disable_notification": False, "referral_poll": True},
        scheduled_for=now, dedupe_key="referral-recruiter-poll:v1"))
    queued += int(enqueue_scheduled_action(chat_id=RECRUITS_CHAT, action_type="message",
        payload={"text": (
            "🌟 Team referral rewards are here!\n\n"
            "New members, please choose the person who invited you in the recruiter poll. 💛\n\n"
            "Your recruiter earns 5% of your first 8 paid client sales, with no expiry.\n"
            "Finish those 8 sales within 3 days and unlock another 8 commissioned sales—"
            "up to 16 in total! The bonus sales have no expiry too.\n\n"
            "The 3 days start when you join a work team or first vote in this poll, whichever comes first. "
            "Changing your vote won't restart the timer.\n\n"
            "Daily shares will be posted in Team Recuiters at 11:59 PM, Philippine time. "
            "Please include the client name or order ID in sale receipts so each sale can be credited accurately.\n\n"
            "Thank you for bringing great people into our teams! 🙌"
        ), "disable_notification": False},
        scheduled_for=now, dedupe_key="referral-rules:v1"))
    if SHARES_CHAT in allowed:
        today = now.astimezone(MANILA).date()
        # Durable scheduled reports are refreshed from actual sales at delivery time.
        for date in (today - dt.timedelta(days=1), today, today + dt.timedelta(days=1)):
            if date < start_date():
                continue
            at = dt.datetime.combine(date, dt.time(23, 59), tzinfo=MANILA).astimezone(dt.timezone.utc)
            queued += int(enqueue_scheduled_action(chat_id=SHARES_CHAT, action_type="message",
                payload={"text": "Preparing today's recruiter shares…", "message_thread_id": SHARES_THREAD,
                         "disable_notification": False, "referral_report_date": date.isoformat()},
                scheduled_for=at, dedupe_key=f"referral-report:{date.isoformat()}"))
    return queued


def status(allowed: set[int], now: dt.datetime) -> dict:
    active = enabled() and RECRUITS_CHAT in allowed
    basis = os.environ.get("REFERRAL_COMMISSION_BASIS", "").strip()
    window = os.environ.get("REFERRAL_WINDOW_START", "").strip()
    polls = read_records("referral-poll:", {RECRUITS_CHAT}) if active else []
    people = read_records("referral-person:", {RECRUITS_CHAT}) if active else []
    reports = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,scheduled_for,telegram_message_id,report_date:payload->>referral_report_date",
        "chat_id": f"eq.{SHARES_CHAT}", "dedupe_key": "like.referral-report:*",
        "order": "id.desc", "limit": 20,
    })) if active and SHARES_CHAT in allowed else []
    return {"enabled": active, "recruits_chat_id": RECRUITS_CHAT, "shares_chat_id": SHARES_CHAT,
            "shares_topic": SHARES_THREAD, "report_time_pht": "23:59", "commission_percent": 5,
            "base_sales": 8, "maximum_sales": 16, "bonus_window_hours": 72,
            "base_sales_expire": False, "bonus_sales_expire": False,
            "commission_basis": basis or None, "window_start": window or None,
            "policy_confirmed": basis in {"gross", "earnings"} and window == "join_or_vote",
            "polls": [{"message_id": r["payload"]["telegram_message_id"]} for r in polls],
            "recorded_referrals": len(people), "reports": reports or []}

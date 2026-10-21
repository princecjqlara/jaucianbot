"""Read-only Suno receipt activity and owner-confirmed song delivery planning.

Payment posts do not establish unique orders or completed songs. Delivery
targets use durable song confirmations and declared availability separately.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import member_capacity
from cloud_store import archive_status, poll_answers_for_range, receipt_messages, song_jobs
from cloud_store import enqueue_scheduled_action, record_automation_marker, request, allowed_chat_ids
from daily_automation import DAILY_REPORTS_CHAT_ID, MANILA, TRABAWHO, TRABAWHO_CHAT_ID, SUNO_GROUPS
from receipt_parser import collect_receipts

VERSION = 2


def stamp(value) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def summarize(rows: list[dict], answers: list[dict], jobs: list[dict] | None, start: dt.date, end: dt.date, *, chat_id: int = TRABAWHO_CHAT_ID) -> dict:
    config = SUNO_GROUPS[chat_id]
    cutoff = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA)
    people, names = {}, defaultdict(set)
    for row in rows:
        if row.get("author_id") is not None and row.get("author_name"):
            names[row["author_name"].strip().casefold()].add(int(row["author_id"]))
    for answer in answers:
        if answer.get("user_id") is not None and answer.get("user_name"):
            names[answer["user_name"].strip().casefold()].add(int(answer["user_id"]))

    def person(uid, name):
        name = " ".join((name or "Unknown member").split())
        if uid is None and len(names[name.casefold()]) == 1:
            uid = next(iter(names[name.casefold()]))
        key = ("id", int(uid)) if uid is not None else ("name", name.casefold())
        return people.setdefault(key, {"user_id": int(uid) if uid is not None else None, "name": name,
            "receipts": Counter(), "delivered": Counter(), "attendance": defaultdict(set),
            "receipt_evidence": [], "delivery_evidence": [], "seen": set(), "open_songs": 0,
            "overdue_songs": 0, "on_time": 0, "late": 0, "review_posts": 0})

    def evidence(row):
        return {"team": config["name"], "chat_id": chat_id, "message_id": row["message_id"],
                "sent_utc": row["sent_utc"], "source": row.get("source") or "unknown",
                "url": f"https://t.me/c/{str(abs(chat_id))[3:]}/{row['message_id']}"}

    for answer in answers:
        poll = answer.get("daily_polls") or {}
        poll = poll[0] if isinstance(poll, list) and poll else poll
        try:
            day = dt.date.fromisoformat(poll.get("work_date", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if start <= day <= end and isinstance(answer.get("active"), bool):
            p = person(answer.get("user_id"), answer.get("user_name"))
            p["attendance"][day].add(answer["active"])
            p["seen"].add(day)

    parsed = collect_receipts([r for r in rows if int(r.get("chat_id", chat_id)) == chat_id], config["receipts"])
    sources = Counter()
    for entry in parsed["entries"]:
        row = entry["row"]
        sent = stamp(row.get("sent_utc"))
        if not sent or not start <= sent.astimezone(MANILA).date() <= end:
            continue
        day = sent.astimezone(MANILA).date()
        p = person(row.get("author_id"), row.get("author_name"))
        p["receipts"][day] += 1  # One readable payment post, never a unique order.
        p["receipt_evidence"].append(evidence(row))
        p["seen"].add(day)
        sources[row.get("source") or "unknown"] += 1
    for item in parsed["review"]:
        row = item["row"]
        sent = stamp(row.get("sent_utc"))
        if sent and start <= sent.astimezone(MANILA).date() <= end:
            p = person(row.get("author_id"), row.get("author_name"))
            p["review_posts"] += 1
            p["seen"].add(sent.astimezone(MANILA).date())

    # A job is one accepted assignment. A receipt amount or a file upload is
    # never promoted to a confirmed song delivery.
    unique, conflicts = {}, set()
    for job in jobs or []:
        if int(job.get("chat_id", chat_id)) != chat_id:
            continue
        p = job.get("payload") or {}
        if p.get("song_cancelled_at") or not p.get("song_assignment_id"):
            continue
        key = int(p["song_assignment_id"])
        if key in unique and any(unique[key].get(k) != p.get(k) for k in
                ("song_assignee_id", "song_completed_at", "song_completion_message_id")):
            conflicts.add(key)
        unique.setdefault(key, p)
    for key in conflicts:
        unique.pop(key)
    completion_ids = Counter(p.get("song_completion_message_id") for p in unique.values() if p.get("song_completed_at"))
    rejected = len(conflicts)
    for p in unique.values():
        began, completed, deadline = stamp(p.get("song_started_at")), stamp(p.get("song_completed_at")), stamp(p.get("song_deadline_at"))
        if not began or began >= cutoff:
            continue
        member = person(p.get("song_assignee_id"), p.get("song_assignee_name"))
        member["seen"].add(min(began.astimezone(MANILA).date(), end))
        if completed and completed < cutoff:
            mid = p.get("song_completion_message_id")
            if completed < began or not mid or completion_ids[mid] != 1:
                rejected += 1
                continue
            day = completed.astimezone(MANILA).date()
            if start <= day <= end:
                member["delivered"][day] += 1
                if deadline:
                    member["on_time" if completed <= deadline else "late"] += 1
                member["delivery_evidence"].append(evidence({"message_id": mid,
                    "sent_utc": completed.astimezone(dt.timezone.utc).isoformat(), "source": "bot"}))
                member["seen"].add(day)
        else:
            member["open_songs"] += 1
            member["overdue_songs"] += int(bool(deadline and deadline < cutoff))
            member["seen"].add(end)

    dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    ordered = []
    for p in people.values():
        daily = [{"date": d.isoformat(), "done_deals": p["receipts"].get(d, 0),
                  "attendance": "Active" if p["attendance"][d] == {True} else
                                "Inactive" if p["attendance"][d] == {False} else "Unknown"} for d in dates]
        active = [d for d in dates if p["attendance"][d] == {True}]
        recent_receipts = [n for d, n in p["receipts"].items() if d >= end - dt.timedelta(days=29)]
        repeated_receipts = sorted(recent_receipts, reverse=True)
        total = sum(p["receipts"].values())
        deliveries = sum(p["delivered"].values())
        plan = {**member_capacity.reviewed_planning({"daily": daily}, end),
            "basis": "receipt_posts", "unit": "receipt posts/day"}
        import report_topics
        if not report_topics.enabled():
            plan["policy"] = "suno-receipts-v2"
        ordered.append({"user_id": p["user_id"], "name": p["name"],
            "last_activity_date": max(p["seen"]).isoformat() if p["seen"] else None,
            "receipt_posts": total, "receipt_output_days": len(p["receipts"]),
            "receipt_output_day_average": round(total / len(p["receipts"]), 2) if p["receipts"] else None,
            "recent_receipt_output_day_average": round(sum(recent_receipts) / len(recent_receipts), 2) if recent_receipts else None,
            "recent_receipt_days": len(recent_receipts), "recent_receipt_daily_peak": max(recent_receipts, default=0),
            "repeated_receipt_posts_per_day": repeated_receipts[2] if len(repeated_receipts) >= 3 else None,
            "receipt_daily_peak": max(p["receipts"].values(), default=0),
            "receipt_active_day_average": round(sum(p["receipts"].get(d, 0) for d in active) / len(active), 2) if active else None,
            "confirmed_deliveries": deliveries if jobs is not None else None,
            "delivery_daily_peak": max(p["delivered"].values(), default=0) if jobs is not None else None,
            "planning": plan, "open_songs": p["open_songs"] if jobs is not None else None,
            "overdue_songs": p["overdue_songs"] if jobs is not None else None,
            "on_time_deliveries": p["on_time"] if jobs is not None else None,
            "late_deliveries": p["late"] if jobs is not None else None,
            "on_time_percent": round(p["on_time"] * 100 / (p["on_time"] + p["late"]), 1) if p["on_time"] + p["late"] else None,
            "unique_paid_orders": None, "maximum_capacity": None, "review_posts": p["review_posts"],
            "receipt_evidence": p["receipt_evidence"], "delivery_evidence": p["delivery_evidence"]})
    ordered.sort(key=lambda m: (m["name"].casefold(), str(m["user_id"])))
    return {"ok": True, "read_only": True, "completed_days_only": True, "team": config["name"],
        "chat_id": chat_id, "basis": "receipt_posts", "unit": "receipt posts/day",
        "start_date": start.isoformat(), "end_date": end.isoformat(), "members": ordered,
        "source_counts": dict(sources), "coverage": {"review_posts": sum(m["review_posts"] for m in ordered),
            "image_only_posts": parsed["attachment_only_posts"], "unresolved_topic_posts": len(parsed["unresolved_topic_rows"]),
            "invalid_delivery_confirmations": rejected},
        "definitions": {"receipts": "Readable payment posts, including possible deposits, tips, extras and repeat payments. They do not establish unique clients or completed songs; identical captions alone cannot prove duplicate payments.",
            "deliveries": "Distinct accepted assignment jobs with an owner's stored SONG SENT/DONE confirmation, counted on the confirmation's Philippine date. Confirmations are self-reported client delivery, not independent verification.",
            "planning": member_capacity.definitions()["planning"] + " Suno output means readable receipt posts, including clients outside the bot; never completed-song counts.",
            "attendance": "Declared availability, including zero recorded receipts on Active dates. Missing activity is Unknown, never automatically Inactive.",
            "coverage": "Exports and live bot archive; unreadable captions, image-only receipts and unresolved topic ancestry are excluded. Historical song deliveries without a matching bot job/confirmation are unknown. Receipt authors receive credit; Telegram IDs combine only unambiguous identity matches. No verified maximum or exact production capacity is inferred."}}


def report(now: dt.datetime, *, days: int = 30, all_history: bool = False, chat_id: int = TRABAWHO_CHAT_ID) -> dict:
    end = now.astimezone(MANILA).date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    if all_history:
        status = archive_status({chat_id})
        first = next((stamp(g.get("first_message_utc")) for g in status if int(g["chat_id"]) == chat_id), None)
        start = min(first.astimezone(MANILA).date(), end) if first else start
    begin = dt.datetime.combine(start, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    until = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = pool.submit(receipt_messages, chat_id, begin, until)
        votes = pool.submit(poll_answers_for_range, chat_id, start, end)
        return summarize(receipts.result(), votes.result(), None, start, end, chat_id=chat_id)


def render(snapshot: dict, end: dt.date) -> list[str]:
    import report_topics
    if report_topics.enabled():
        from organized_reports import capacity_text
        from daily_automation import split_message
        return split_message(capacity_text(snapshot["chat_id"], end, snapshot["members"]), limit=1700)
    header = f"<b>SUNO CAPACITY PLAN</b>\n{html.escape(snapshot.get('team', 'Suno'))}\nThrough {end:%b %d, %Y} (PHT)\n30-day estimates in receipt posts/day"
    parts = [header + "\n\nAll readable receipts count, including clients outside the bot."]
    cutoff = (end - dt.timedelta(days=29)).isoformat()
    for m in snapshot["members"]:
        if (m.get("last_activity_date") or "") < cutoff:
            continue
        name = html.escape(m["name"][:65])
        sample = next(iter(m["receipt_evidence"]), None)
        if sample:
            name = f'<a href="{html.escape(sample["url"], quote=True)}">{name}</a>'
        p = m["planning"]
        safe = p["safe_plan_per_day"]
        safe_text = f"{safe:.2f}/day" if safe is not None else "More Active/receipt days needed"
        target = p["reachable_target_per_day"]
        target_text = (f"{target}/day ({p['reachable_support_days']}/{p['known_active_days']} Active days)"
            if target else "Below 1/day" if target == 0 else "More history needed")
        receipt_avg = m["recent_receipt_output_day_average"]
        receipt_text = f"{receipt_avg:.2f}/receipt day" if receipt_avg is not None else "No readable recent receipts"
        avg = p["active_day_average"]
        active_text = f"{avg:.2f}/Active day ({p['known_active_days']} days)" if avg is not None else "No Active-day history"
        block = (f"<b>{name}</b>\nAverage recorded: {active_text}\nConservative: {safe_text}\nTypical target: {target_text}\n"
                 f"Receipt-day average: {receipt_text}\nRecent peak: {m.get('recent_receipt_daily_peak', 0)}/day")
        candidate = parts[-1] + "\n\n" + block
        visible = html.unescape(re.sub(r"<[^>]*>", "", candidate))
        if len(visible.encode("utf-16-le")) // 2 > 3400:
            parts.append(header)
        parts[-1] += "\n\n" + block
    footer = ("Active-day average includes zero receipt dates. Conservative = 80% of that average.\n"
        "Targets need 7 Active and 3 receipt dates; typical levels need success on at least half of Active dates.\n"
        "Receipt-day averages exclude unknown days. Payments may repeat; they are not unique orders or completed-song counts.\n"
        "Sources: exports + live bot; unreadable/missing records excluded. Capacity remains an estimate. Updated daily after 12:10 AM PHT.")
    visible = html.unescape(re.sub(r"<[^>]*>", "", parts[-1] + footer))
    if len(visible.encode("utf-16-le")) // 2 > 3400:
        parts.append(header)
    parts[-1] += "\n\n" + footer
    return parts


def _saved(key: str) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}", "dedupe_key": f"eq.{key}", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else None


def queue_automation(now: dt.datetime, approved: set[int]) -> int:
    from automation_controls import chat_enabled
    from member_capacity_reports import enabled, latest_report_date
    if not enabled() or not {TRABAWHO_CHAT_ID, DAILY_REPORTS_CHAT_ID}.issubset(approved) or not chat_enabled(DAILY_REPORTS_CHAT_ID):
        return 0
    local = now.astimezone(MANILA)
    if local.hour == 0 and local.minute < 10:
        return 0
    end = latest_report_date(now)
    key = f"daily-suno-capacity:v{VERSION}:{end}"
    if _saved(key + ":complete") is not None:
        return 0
    saved = _saved(key + ":snapshot")
    if saved is None:
        snapshot = report(now, all_history=True)
        saved = {"parts": render(snapshot, end), "report": snapshot}
        request("scheduled_actions?on_conflict=dedupe_key&select=id", {
            "chat_id": DAILY_REPORTS_CHAT_ID, "action_type": "message", "status": "cancelled",
            "scheduled_for": now.isoformat(), "dedupe_key": key + ":snapshot", "payload": saved,
        }, prefer="resolution=ignore-duplicates,return=representation")
        saved = _saved(key + ":snapshot")
        if saved is None:
            raise RuntimeError("Suno capacity snapshot was not saved")
    queued = 0
    for number, part in enumerate(saved["parts"], 1):
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message",
            scheduled_for=now, dedupe_key=f"{key}:part:{number}", payload={
                "text": part + (f"\n\nSuno · {number}/{len(saved['parts'])}" if len(saved["parts"]) > 1 else ""),
                "parse_mode": "HTML", "disable_notification": number > 1,
                "suno_capacity_date": end.isoformat(), "suno_capacity_version": VERSION,
            }))
    record_automation_marker(DAILY_REPORTS_CHAT_ID, key + ":complete", now)
    return queued


def delivery_allowed(action: dict, now: dt.datetime) -> bool:
    p = action["payload"]
    if not p.get("suno_capacity_date"):
        return True
    from member_capacity_reports import enabled, latest_report_date
    return (enabled() and int(action["chat_id"]) == DAILY_REPORTS_CHAT_ID and p.get("suno_capacity_version") == VERSION
            and {TRABAWHO_CHAT_ID, DAILY_REPORTS_CHAT_ID}.issubset(allowed_chat_ids())
            and p["suno_capacity_date"] == latest_report_date(now).isoformat())

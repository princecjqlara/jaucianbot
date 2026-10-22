"""Explicit Makata setup, isolated daily reporting and private subscription."""
from __future__ import annotations
import datetime as dt
import html
import os
import re
import urllib.parse

from cloud_store import request, enqueue_scheduled_action, record_automation_marker, daily_poll_counts, receipt_messages
from daily_automation import MAKATA, MAKATA_CHAT_ID, SUNO_GROUPS, AVAILABILITY_GROUPS, DAILY_REPORTS_CHAT_ID, MANILA, queue_daily_polls, money, split_message

RECIPIENT_USERNAME = "mmadss25"


def enabled() -> bool:
    return MAKATA_CHAT_ID in SUNO_GROUPS


def _read(key: str) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "payload", "dedupe_key": f"eq.{key}", "limit": 1})) or []
    return rows[0]["payload"] if rows else None


def _save(key: str, payload: dict, now: dt.datetime) -> dict:
    request("scheduled_actions?on_conflict=dedupe_key&select=id", {"chat_id": MAKATA_CHAT_ID,
        "action_type": "message", "status": "cancelled", "scheduled_for": now.isoformat(),
        "dedupe_key": key, "payload": payload}, prefer="resolution=ignore-duplicates,return=representation")
    saved = _read(key)
    if saved is None:
        raise RuntimeError("Makata state was not persisted")
    return saved


def hydrate(approved: set[int]) -> None:
    if not enabled() or MAKATA_CHAT_ID not in approved:
        return
    for field in ("active", "songs"):
        if not MAKATA.get(field):
            state = _read(f"makata-topic-config:{field}")
            if state and isinstance(state.get("thread_id"), int) and state["thread_id"] > 0:
                MAKATA[field] = state["thread_id"]


def recipient() -> dict | None:
    if not enabled():
        return None
    p = _read("makata-report-recipient:mmadss25")
    return p if p and p.get("username") == RECIPIENT_USERNAME and isinstance(p.get("chat_id"), int) and p["chat_id"] > 0 else None


def private_enabled(chat_id: int) -> bool:
    raw_allowed = {int(s.strip()) for s in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if s.strip()}
    raw_active = {int(s.strip()) for s in os.environ.get("AUTOMATION_CHAT_IDS", "").split(",") if s.strip()}
    if MAKATA_CHAT_ID not in raw_allowed or MAKATA_CHAT_ID not in raw_active:
        return False
    p = recipient()
    return bool(p and p["chat_id"] == int(chat_id))


def delivery_ids(active: set[int]) -> set[int]:
    if enabled() and MAKATA_CHAT_ID in active:
        p = recipient()
        if p:
            return active | {p["chat_id"]}
    return active


def register_private(update: dict, approved: set[int], now: dt.datetime) -> bool:
    message = update.get("message") or {}
    chat, user = message.get("chat") or {}, message.get("from") or {}
    if (not enabled() or MAKATA_CHAT_ID not in approved or chat.get("type") != "private"
            or not isinstance(chat.get("id"), int) or isinstance(chat.get("id"), bool) or chat["id"] <= 0
            or user.get("is_bot") or user.get("id") != chat.get("id")
            or str(user.get("username") or "").casefold() != RECIPIENT_USERNAME):
        return False
    return _bind_recipient(int(chat["id"]), now, source="private_message")


def _bind_recipient(chat_id: int, now: dt.datetime, *, source: str) -> bool:
    saved = _save("makata-report-recipient:mmadss25", {"chat_id": chat_id,
        "username": RECIPIENT_USERNAME, "registered_utc": now.isoformat(), "source": source}, now)
    if saved["chat_id"] != chat_id:
        return False  # Never silently bind a reused username to a different person.
    request("chats?on_conflict=chat_id", {"chat_id": chat_id, "title": "@" + RECIPIENT_USERNAME,
        "chat_type": "private", "last_seen_utc": now.isoformat()}, prefer="resolution=merge-duplicates,return=minimal")
    enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"makata-recipient-welcome:{chat_id}", payload={"text": "Makata daily reports are connected.",
            "makata_private_report": True, "makata_source_chat_id": MAKATA_CHAT_ID})
    if source == "private_message":
        _retry_private_reports(chat_id, now)
    return True


def _retry_private_reports(chat_id: int, now: dt.datetime) -> None:
    """A fresh authenticated private message makes previously rejected sends due.

    Keep the original outbox row and report; never duplicate a delivered report
    or change a message another scheduler worker has already claimed.
    """
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,attempts,payload,updated_at", "chat_id": f"eq.{chat_id}",
        "status": "in.(pending,failed)", "attempts": "gt.0", "limit": 100,
        "order": "created_at.desc",
    })) or []
    for row in rows:
        p = row.get("payload") or {}
        if not p.get("makata_private_report") or p.get("makata_source_chat_id") != MAKATA_CHAT_ID:
            continue
        filters = {"id": f"eq.{row['id']}", "chat_id": f"eq.{chat_id}", "status": f"eq.{row['status']}"}
        if row.get("updated_at"):
            filters["updated_at"] = f"eq.{row['updated_at']}"
        request("scheduled_actions?" + urllib.parse.urlencode(filters), {
            "status": "pending", "scheduled_for": now.isoformat(), "updated_at": now.isoformat(),
            "last_error": None, "attempts": 0, "payload": {**p, "_delivery_failures": 0},
        }, method="PATCH", prefer="return=minimal")


def connect_recipient(approved: set[int], now: dt.datetime) -> dict:
    """Recover the requested recipient after an earlier private hello was ignored.

    Only the fixed recipient is eligible. Resolve a real Makata sender through
    Telegram, then confirm their private chat; never guess IDs or replay updates.
    All outgoing messages remain in the scheduled outbox.
    """
    from automation_controls import chat_enabled
    from telegram_sender import telegram_call, TelegramError
    if not enabled() or MAKATA_CHAT_ID not in approved or not chat_enabled(MAKATA_CHAT_ID):
        return {"ok": False, "error": "makata_not_active"}
    bound = recipient()
    if bound:
        connected = _bind_recipient(bound["chat_id"], now, source="group_verified_recovery")
        if connected:
            _retry_private_reports(bound["chat_id"], now)
        return {"ok": connected, "connected": connected}
    rows = request("messages?" + urllib.parse.urlencode({
        "select": "author_id", "chat_id": f"eq.{MAKATA_CHAT_ID}",
        "author_id": "gt.0", "sent_utc": f"gte.{(now - dt.timedelta(days=30)).isoformat()}",
        "order": "sent_utc.desc,message_id.desc", "limit": 1000,
    })) or []
    seen = set()
    for row in rows:
        uid = row.get("author_id")
        if not isinstance(uid, int) or uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        if len(seen) > 30:
            break
        try:
            member = telegram_call("getChatMember", {"chat_id": MAKATA_CHAT_ID, "user_id": uid})
        except TelegramError:
            continue
        user = member.get("user") or {}
        if (user.get("id") != uid or user.get("is_bot")
                or str(user.get("username") or "").casefold() != RECIPIENT_USERNAME):
            continue
        try:
            private = telegram_call("getChat", {"chat_id": uid})
        except TelegramError:
            return {"ok": False, "error": "private_chat_unavailable"}
        if (private.get("id") != uid or private.get("type") != "private"
                or str(private.get("username") or "").casefold() != RECIPIENT_USERNAME):
            return {"ok": False, "error": "recipient_identity_mismatch"}
        connected = _bind_recipient(uid, now, source="group_verified_recovery")
        return {"ok": connected, "connected": connected,
            **({"error": "recipient_already_bound"} if not connected else {})}
    return {"ok": False, "error": "recipient_not_found_in_makata"}


def queue_setup(now: dt.datetime, approved: set[int]) -> int:
    if not enabled() or MAKATA_CHAT_ID not in approved:
        return 0
    from automation_controls import chat_enabled
    if not chat_enabled(MAKATA_CHAT_ID):
        return 0
    hydrate(approved)
    queued = 0
    for field, name in (("active", "ACTIVE TOMORROW"), ("songs", "SONGS")):
        if not MAKATA.get(field):
            queued += int(enqueue_scheduled_action(chat_id=MAKATA_CHAT_ID, action_type="message", scheduled_for=now,
                dedupe_key=f"makata-create-topic:{field}", payload={"suno_create_topic": field, "topic_name": name}))
    if MAKATA.get("active"):
        tomorrow = now.astimezone(MANILA).date() + dt.timedelta(days=1)
        queued += queue_daily_polls(tomorrow, now, {MAKATA_CHAT_ID})
    return queued


def setup_delivery(action: dict) -> dict | None:
    field = action["payload"].get("suno_create_topic")
    if not field:
        return None
    saved = _read(f"makata-topic-config:{field}")
    return {"message_id": saved["thread_id"], "_telegram_operation": "createForumTopic"} if saved else None


def record_topic(action: dict, result: dict, now: dt.datetime) -> None:
    field = action["payload"].get("suno_create_topic")
    if field:
        saved = _save(f"makata-topic-config:{field}", {"thread_id": int(result["message_id"])}, now)
        MAKATA[field] = saved["thread_id"]


def private_delivery_allowed(action: dict) -> bool:
    if not enabled() or int(action["chat_id"]) <= 0:
        return True
    p = action["payload"]
    return bool(p.get("makata_private_report") and p.get("makata_source_chat_id") == MAKATA_CHAT_ID
                and private_enabled(int(action["chat_id"])))


def build_report(day: dt.date, now: dt.datetime) -> str:
    from trabawho_receipts import receipt_report
    from trabawho_songs import song_report_lines
    begin = dt.datetime.combine(day, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    report = receipt_report(receipt_messages(MAKATA_CHAT_ID, begin, end), day, day, chat_id=MAKATA_CHAT_ID)
    total = report["totals"]
    tomorrow = day + dt.timedelta(days=1)
    count = daily_poll_counts({MAKATA_CHAT_ID}, tomorrow).get(MAKATA_CHAT_ID)
    lines = ["SUNO MAKATA — DAILY REPORT", f"{day:%b %d, %Y} (PHT)",
        f"Recorded receipts: {total['receipt_posts']}", f"Gross: {money(total['gross'])} · Recorded earnings: {money(total['salary'])}"]
    for worker in report["workers"]:
        lines.append(f"{worker['name']}: {worker['receipt_posts']} receipts · gross {money(worker['gross'])} · earnings {money(worker['salary'])}")
    if not total["receipt_posts"]:
        lines.append("No readable receipts in the archive for this date; this does not prove zero sales.")
    lines += ["", *song_report_lines(day, now, MAKATA_CHAT_ID), "",
        f"Active for {tomorrow:%b %d}: {int(count['active_workers']) if count else 'pending votes'}",
        "Receipt posts are payments, not unique orders. Earnings are not proof of payout.",
        f"Excluded: {len(report['review'])} review posts, {report['coverage']['attachment_only_posts']} image-only receipts. Archive coverage may be incomplete."]
    import suno_capacity
    capacity = suno_capacity.report(now, chat_id=MAKATA_CHAT_ID)
    reference = None
    import report_topics
    if report_topics.enabled():
        import organized_reports
        import nightly_reports
        from cloud_store import allowed_chat_ids
        approved = allowed_chat_ids()
        history_day = day - dt.timedelta(days=1) if nightly_reports.report_date(now) == day else min(
            day, now.astimezone(MANILA).date() - dt.timedelta(days=1))
        current = organized_reports.current_capacities(organized_reports.load_capacity(history_day, now, approved),
            organized_reports.sources(approved), now)
        if any(c.get("pending_verification") for c in current.get("membership", {}).values()):
            raise RuntimeError("Capacity membership verification pending")
        capacity["members"] = current["members"].get(str(MAKATA_CHAT_ID), [])
        reference = current.get("defaults", {}).get("suno")
        if nightly_reports.report_date(now) == day:
            previous = organized_reports._read(f"nightly-personal-capacity:{day - dt.timedelta(days=1)}:"
                f"{organized_reports._scope(organized_reports.sources(approved))}")
            changes = nightly_reports.changes(current, previous)
            for member in capacity["members"]:
                change = changes.get(("suno", int(member["user_id"])))
                if change:
                    member["planning"] = {**member["planning"], "daily_capacity_change": change["text"]}
    lines += ["", "RECEIPT CAPACITY — ESTIMATES"]
    if not capacity["members"]:
        lines.append("Pending receipt and attendance history.")
    for member in capacity["members"]:
        plan = member["planning"]
        if plan.get("policy") == "top-three-14d-v1":
            estimate, safe, peak = plan.get("planning_average_per_day"), plan["safe_plan_per_day"], plan["observed_daily_peak"]
            if estimate is None and plan.get("estimate_origin") == "insufficient_personal_history":
                lines.append(f"{member['name']}: capacity Unknown · safe target Unknown · recorded peak {peak if peak is not None else 'unknown'}")
                continue
            label = "provisional" if plan.get("provisional") else "estimate"
            lines.append(f"{member['name']}: {label} {f'{estimate:.2f}/day' if estimate is not None else 'pending'} · "
                f"safe {str(safe) + '/day' if safe is not None else 'pending'} · recorded peak {peak if peak is not None else 'unknown'}")
            if plan.get("daily_capacity_change"):
                lines.append(f"Change: {plan['daily_capacity_change']}")
            continue
        safe, reach = plan["safe_plan_per_day"], plan["reachable_target_per_day"]
        avg = member.get("recent_receipt_output_day_average")
        lines.append(f"{member['name']}: observed {f'{avg:.2f}/receipt day' if avg is not None else 'pending'} · "
            f"conservative {f'{safe:.2f}/day' if safe is not None else 'pending'} · typical {str(reach) + '/day' if reach is not None else 'pending'}")
    lines.append("Counts all readable receipts, including clients outside the bot. Production limits remain unverified.")
    if reference is not None:
        lines += organized_reports.suno_reference_text(reference, "receipt posts/day")
    return "\n".join(lines)


def previous_report_parts(day: dt.date, target: int) -> dict[int, int]:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "dedupe_key,telegram_message_id", "chat_id": f"eq.{target}",
        "dedupe_key": f"like.makata-daily-report:v*:{day}:{target}:part:*",
        "telegram_message_id": "gt.0", "order": "id.asc", "limit": 1000,
    })) or []
    return {int(row["dedupe_key"].rsplit(":", 1)[-1]): int(row["telegram_message_id"]) for row in rows}


def queue_reports(now: dt.datetime, approved: set[int]) -> int:
    from automation_controls import chat_enabled
    if not enabled() or MAKATA_CHAT_ID not in approved or not chat_enabled(MAKATA_CHAT_ID):
        return 0
    local = now.astimezone(MANILA)
    import nightly_reports
    day = nightly_reports.report_date(now)
    if day is None:
        return 0
    import report_topics
    targets = [DAILY_REPORTS_CHAT_ID] if (DAILY_REPORTS_CHAT_ID in approved
        and chat_enabled(DAILY_REPORTS_CHAT_ID) and not report_topics.enabled()) else []
    person = recipient()
    if person:
        targets.append(person["chat_id"])
    queued = 0
    if not targets:
        return 0
    reference_policy = report_topics.enabled()
    base = f"makata-daily-report:v7:{day}"
    missing = [target for target in targets if _read(f"{base}:complete:{target}") is None]
    if not missing:
        return 0
    saved = _read(base + ":snapshot")
    if saved is None:
        saved = _save(base + ":snapshot", {"parts": split_message(build_report(day, now))}, now)
    for target in missing:
        prior = previous_report_parts(day, target) if reference_policy else {}
        for number, text in enumerate(saved["parts"], 1):
            queued += int(enqueue_scheduled_action(chat_id=target, action_type="message", scheduled_for=now,
                dedupe_key=f"{base}:{target}:part:{number}", payload={"text": text,
                    **nightly_reports.payload(day),
                    "disable_notification": number > 1, "makata_private_report": target > 0,
                    "makata_source_chat_id": MAKATA_CHAT_ID, "makata_report_date": day.isoformat(),
                    **({"edit_message_id": prior[number]} if number in prior else {})}))
        for number, message_id in prior.items():
            if number <= len(saved["parts"]):
                continue
            queued += int(enqueue_scheduled_action(chat_id=target, action_type="message", scheduled_for=now,
                dedupe_key=f"{base}:{target}:cleanup:{message_id}", payload={"text": "Makata report updated above.",
                    **nightly_reports.payload(day),
                    "edit_message_id": message_id, "disable_notification": True,
                    "makata_private_report": target > 0, "makata_source_chat_id": MAKATA_CHAT_ID,
                    "makata_report_date": day.isoformat()}))
        record_automation_marker(MAKATA_CHAT_ID, f"{base}:complete:{target}", now)
    return queued


def queue_requested_report(day: dt.date, now: dt.datetime, approved: set[int], request_id: str) -> dict:
    """Queue a dated report only to the configured recipient, without replacing daily closeout."""
    today = now.astimezone(MANILA).date()
    if not re.fullmatch(r"[0-9a-f]{32}", request_id) or not today - dt.timedelta(days=31) <= day <= today:
        raise ValueError("invalid report date or request ID")
    person = recipient()
    if MAKATA_CHAT_ID not in approved or not person or not private_enabled(person["chat_id"]):
        return {"ok": False, "error": "makata_recipient_unavailable"}
    base = f"makata-requested-report:v1:{request_id}"
    saved = _read(base + ":snapshot")
    if saved is None:
        text = build_report(day, now)
        if day == today:
            lines = text.splitlines()
            lines.insert(2, f"As of {now.astimezone(MANILA):%I:%M %p} PHT · Day still in progress.")
            text = "\n".join(lines)
        saved = _save(base + ":snapshot", {"parts": split_message(text), "date": day.isoformat(),
            "as_of": now.isoformat(), "provisional": day == today, "recipient_id": person["chat_id"]}, now)
    if saved["date"] != day.isoformat() or saved["recipient_id"] != person["chat_id"]:
        raise ValueError("report request already belongs to another date or recipient")
    queued = 0
    for number, text in enumerate(saved["parts"], 1):
        queued += int(enqueue_scheduled_action(chat_id=person["chat_id"], action_type="message", scheduled_for=now,
            dedupe_key=f"{base}:part:{number}", payload={"text": text, "disable_notification": number > 1,
                "makata_private_report": True, "makata_source_chat_id": MAKATA_CHAT_ID,
                "makata_report_date": saved["date"], "makata_report_as_of": saved["as_of"],
                "makata_report_provisional": saved["provisional"], "makata_report_requested": True}))
    return {"ok": True, "queued": queued, "date": saved["date"], "provisional": saved["provisional"]}


def status(approved: set[int]) -> dict:
    hydrate(approved)
    from telegram_sender import telegram_call
    bot = telegram_call("getMe", {})
    bound = recipient()
    last_report = None
    delivery = None
    if bound:
        rows = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "status,last_error,scheduled_for,telegram_message_id,sent_at,payload", "chat_id": f"eq.{bound['chat_id']}",
            "order": "created_at.desc", "limit": 10,
        })) or []
        for row in rows:
            p = row.get("payload") or {}
            if (p.get("makata_private_report") and p.get("makata_source_chat_id") == MAKATA_CHAT_ID
                    and p.get("makata_report_date")):
                if delivery is None:
                    delivery = {"date": p["makata_report_date"], "status": row.get("status"),
                        "error": row.get("last_error"), "next_attempt_utc": row.get("scheduled_for")}
                if row.get("status") == "sent" and row.get("telegram_message_id") and last_report is None:
                    last_report = {"date": p["makata_report_date"], "message_id": row["telegram_message_id"],
                        "sent_utc": row["sent_at"]}
    return {"ok": True, "team": MAKATA["name"], "chat_id": MAKATA_CHAT_ID,
        "approved": MAKATA_CHAT_ID in approved, "topics": {k: MAKATA.get(k) for k in ("general", "announcements", "receipts", "contact_thread", "active", "songs")},
        "recipient": {"username": RECIPIENT_USERNAME, "connected": bool(bound), "last_report": last_report,
            "delivery": delivery},
        "bot_username": bot["username"], "subscribe_url": f"https://t.me/{bot['username']}?start=makata_reports"}

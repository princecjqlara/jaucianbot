"""Short, repeated tomorrow-poll nudges through the scheduled outbox."""
from __future__ import annotations

import datetime as dt
import html
import os
import urllib.parse

import automation_controls
import member_presence
from cloud_store import daily_poll_answers, enqueue_scheduled_action, request
from daily_automation import AVAILABILITY_GROUPS, MANILA


HOURS = (8, 11, 14, 17, 20, 22)
MENTIONS_PER_MESSAGE = 20
PAGE_SIZE = 1000


def enabled() -> bool:
    return os.environ.get("AVAILABILITY_REMINDERS_ENABLED", "false").strip().lower() == "true"


def _read(table: str, filters: dict) -> list[dict]:
    return request(table + "?" + urllib.parse.urlencode(filters)) or []


def slot(now: dt.datetime) -> tuple[dt.date, int] | None:
    local = now.astimezone(MANILA)
    # Only the current reminder hour can run; an outage must not replay old slots.
    return (local.date() + dt.timedelta(days=1), local.hour) if local.hour in HOURS else None


def poll(chat: int, day: dt.date) -> dict | None:
    rows = _read("daily_polls", {"select": "poll_id,thread_id,telegram_message_id",
        "chat_id": f"eq.{chat}", "work_date": f"eq.{day}", "limit": 1})
    return rows[0] if rows else None


def known_members(chat: int) -> list[dict]:
    """Discover IDs, not presumed membership, from all captured authors/voters.

    Keyset pagination skips remaining posts by the last author, rather than
    rereading their entire history. Silent, never-observed members are unknown.
    """
    people = {uid: {"user_id": uid, "user_name": row.get("user_name") or str(uid)}
              for uid, row in member_presence.records(chat).items() if uid > 0}
    for table, identity, name, extra, order in (
        ("messages", "author_id", "author_name", {"chat_id": f"eq.{chat}"},
         "author_id.asc,sent_utc.desc,message_id.desc"),
        ("daily_poll_answers", "user_id", "user_name", {"daily_polls.chat_id": f"eq.{chat}"},
         "user_id.asc,updated_at.desc,poll_id.asc"),
    ):
        cursor = 0
        while True:
            fields = f"{identity},{name}" + (",daily_polls!inner(work_date,chat_id)" if table == "daily_poll_answers" else "")
            rows = _read(table, {"select": fields, **extra, identity: f"gt.{cursor}",
                "order": order, "limit": PAGE_SIZE})
            for row in rows:
                uid = int(row[identity])
                if uid > cursor:
                    people[uid] = {"user_id": uid, "user_name": row.get(name) or str(uid)}
                    cursor = uid
            if len(rows) < PAGE_SIZE:
                break
    return [people[uid] for uid in sorted(people)]


def queue(now: dt.datetime, allowed: set[int]) -> int:
    if not enabled():
        return 0
    current = slot(now)
    teams = {chat for chat in allowed.intersection(AVAILABILITY_GROUPS)
             if automation_controls.chat_enabled(chat)}
    if not current or not teams:
        return 0
    day, hour = current
    queued = 0
    for chat in sorted(teams):
        base = f"availability-nonvoter:{day}:{hour:02}:{chat}"
        key = base + ":roster"
        saved = _read("scheduled_actions", {"select": "payload", "dedupe_key": f"eq.{key}", "limit": 1})
        if saved and saved[0]["payload"].get("complete"):
            continue
        if not saved:
            current_poll = poll(chat, day)
            if not current_poll or not current_poll.get("telegram_message_id"):
                continue  # Wait for the poll's ordinary scheduled delivery.
            payload = {"availability_roster": known_members(chat), "poll": current_poll}
            created = request("scheduled_actions?on_conflict=dedupe_key&select=payload", {
                "chat_id": chat, "action_type": "message", "status": "cancelled",
                "dedupe_key": key, "scheduled_for": now.isoformat(), "payload": payload,
            }, prefer="resolution=ignore-duplicates,return=representation")
            saved = created or _read("scheduled_actions", {"select": "payload", "dedupe_key": f"eq.{key}", "limit": 1})
        payload = saved[0]["payload"]
        people = payload["availability_roster"]
        for offset in range(0, len(people), MENTIONS_PER_MESSAGE):
            queued += bool(enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
                dedupe_key=f"{base}:{offset // MENTIONS_PER_MESSAGE}", payload={
                    "text": "Please answer tomorrow's availability poll.",
                    "availability_reminder_date": day.isoformat(), "availability_reminder_hour": hour,
                    "availability_reminder_poll": payload["poll"]["poll_id"],
                    "availability_reminder_members": people[offset:offset + MENTIONS_PER_MESSAGE],
                }))
        request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": f"eq.{key}"}),
            {"payload": {**payload, "complete": True}, "updated_at": now.isoformat()},
            method="PATCH", prefer="return=minimal")
    return queued


def prepare_delivery(action: dict, now: dt.datetime) -> bool:
    source = action.get("payload") or {}
    if not source.get("availability_reminder_date"):
        return True
    action["repeat_interval_minutes"] = None
    if not enabled():
        return False
    chat = int(action["chat_id"])
    current = slot(now)
    if (chat not in AVAILABILITY_GROUPS or not current
            or current != (dt.date.fromisoformat(source["availability_reminder_date"]), source["availability_reminder_hour"])):
        return False
    day, _ = current
    current_poll = poll(chat, day)
    if not current_poll or current_poll["poll_id"] != source["availability_reminder_poll"]:
        return False
    answered = {int(row["user_id"]) for row in daily_poll_answers(current_poll["poll_id"])}
    candidates = [p for p in source["availability_reminder_members"] if int(p["user_id"]) not in answered]
    people, _ = member_presence.filter_current(chat, candidates, now,
        budget={"remaining": MENTIONS_PER_MESSAGE}, humans_only=True)
    cached = member_presence.records(chat) if people else {}
    people = [p for p in people if cached.get(int(p["user_id"]), {}).get("status") in {"member", "restricted"}
              and not cached[int(p["user_id"])].get("is_bot")]
    # A vote may arrive while membership API requests are in flight.
    answered.update(int(row["user_id"]) for row in daily_poll_answers(current_poll["poll_id"]))
    mentions, notified = [], []
    for person in people:
        uid = int(person["user_id"])
        if uid in answered:
            continue
        name = cached.get(uid, {}).get("user_name") or person.get("user_name") or str(uid)
        name = " ".join(name.split())[:64]
        mentions.append(f'<a href="tg://user?id={uid}">{html.escape(name)}</a>')
        notified.append(uid)
    if not mentions:
        return False
    source.update(text=f"Active tomorrow ({day:%b %d}, PHT)? Vote Yes or No.\n\n" + ", ".join(mentions),
        availability_reminder_notified_ids=notified,
        parse_mode="HTML", message_thread_id=current_poll["thread_id"], disable_notification=False,
        reply_markup={"inline_keyboard": [[{"text": "Vote here", "url":
            f"https://t.me/c/{str(abs(chat))[3:]}/{current_poll['telegram_message_id']}"}]]})
    import member_warnings
    if member_warnings.enabled() and chat in member_warnings.GROUPS:
        source["text"] += "\n\nPlease answer by midnight. Unanswered personal reminders with 2+ hours to respond appear in daily warnings."
    return True

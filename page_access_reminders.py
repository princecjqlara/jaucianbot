"""Daily page-access poll nudges for verified non-admin nonvoters."""
from __future__ import annotations

import datetime as dt
import html
import os
import urllib.parse

import automation_controls
import member_presence
import page_access
from availability_reminders import known_members, MENTIONS_PER_MESSAGE
from cloud_store import enqueue_scheduled_action, request
from daily_automation import AVAILABILITY_GROUPS, MANILA


def enabled() -> bool:
    return os.environ.get("PAGE_ACCESS_REMINDERS_ENABLED", "false").strip().lower() == "true"


def _read(key: str) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "dedupe_key": f"eq.{key}", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else None


def queue(now: dt.datetime, allowed: set[int]) -> int:
    local = now.astimezone(MANILA)
    if not enabled() or not 8 <= local.hour < 23:
        return 0
    queued = 0
    teams = automation_controls.enabled_chat_ids(allowed).intersection(AVAILABILITY_GROUPS)
    for chat in sorted(teams):
        if not page_access.enabled(chat) or not automation_controls.page_access_polls_enabled(chat):
            continue
        version = page_access.version(chat)
        parts = (len(page_access.page_map(chat)) + 8) // 9
        roster = None
        for part in range(parts):
            base = f"page-access-nonvoter:v1:{local.date()}:{chat}:{version}:part:{part}"
            key = base + ":roster"
            saved = _read(key)
            if saved and saved.get("complete"):
                continue
            poll = page_access.poll_record(chat, part)
            if not poll or not poll.get("message_id"):
                continue
            if saved is None:
                if roster is None:
                    roster = known_members(chat)
                answered = page_access.answered_members(chat, part)
                people = [p for p in roster if int(p["user_id"]) not in answered]
                payload = {"members": people}
                request("scheduled_actions?on_conflict=dedupe_key", {
                    "chat_id": chat, "action_type": "message", "status": "cancelled",
                    "scheduled_for": now.isoformat(), "dedupe_key": key, "payload": payload,
                }, prefer="resolution=ignore-duplicates,return=minimal")
                saved = _read(key)
                if saved is None:
                    raise RuntimeError("Page-access reminder roster was not persisted")
            people = saved["members"]
            for offset in range(0, len(people), MENTIONS_PER_MESSAGE):
                queued += int(enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
                    dedupe_key=f"{base}:{offset // MENTIONS_PER_MESSAGE}", payload={
                        "text": "Please answer the page-access poll.",
                        "page_access_reminder_date": str(local.date()), "page_access_reminder_version": version,
                        "page_access_reminder_part": part, "page_access_reminder_poll": poll["page_access_poll_id"],
                        "page_access_reminder_members": people[offset:offset + MENTIONS_PER_MESSAGE],
                    }))
            request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": f"eq.{key}"}),
                {"payload": {**saved, "complete": True}, "updated_at": now.isoformat()},
                method="PATCH", prefer="return=minimal")
    return queued


def prepare_delivery(action: dict, now: dt.datetime) -> bool:
    source = action.get("payload") or {}
    if not source.get("page_access_reminder_date"):
        return True
    action["repeat_interval_minutes"] = None
    chat = int(action["chat_id"])
    local = now.astimezone(MANILA)
    if (not enabled() or not page_access.enabled(chat) or not automation_controls.page_access_polls_enabled(chat)
            or not automation_controls.chat_enabled(chat) or not 8 <= local.hour < 23
            or source["page_access_reminder_date"] != str(local.date())
            or source.get("page_access_reminder_version") != page_access.version(chat)):
        return False
    part = int(source.get("page_access_reminder_part") or 0)
    poll = page_access.poll_record(chat, part)
    if not poll or poll.get("page_access_poll_id") != source.get("page_access_reminder_poll"):
        return False
    answered = page_access.answered_members(chat, part)
    candidates = [p for p in source["page_access_reminder_members"] if int(p["user_id"]) not in answered]
    people, _ = member_presence.filter_current(chat, candidates, now,
        budget={"remaining": MENTIONS_PER_MESSAGE}, humans_only=True)
    cached = member_presence.records(chat) if people else {}
    answered.update(page_access.answered_members(chat, part))
    mentions = []
    for person in people:
        uid = int(person["user_id"])
        member = cached.get(uid, {})
        if (uid in answered or member.get("status") not in {"member", "restricted"}
                or member.get("is_bot") is not False):
            continue
        name = " ".join((member.get("user_name") or person.get("user_name") or str(uid)).split())[:64]
        mentions.append(f'<a href="tg://user?id={uid}">{html.escape(name)}</a>')
    if not mentions:
        return False
    source.update(text="Please vote in the page-access poll. Choose your pages or No access.\n\n" + ", ".join(mentions),
        parse_mode="HTML", disable_notification=False, message_thread_id=AVAILABILITY_GROUPS[chat]["contact_thread"],
        reply_markup={"inline_keyboard": [[{"text": "Choose pages", "url":
            f"https://t.me/c/{str(abs(chat))[3:]}/{int(poll['message_id'])}"}]]})
    return True

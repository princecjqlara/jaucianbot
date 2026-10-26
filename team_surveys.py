"""Explicit manager-requested page-access/tomorrow reminders via tracked polls."""
from __future__ import annotations

import datetime as dt
import html
import re

import automation_controls
import availability_reminders
import page_access
from cloud_store import daily_poll_counts, enqueue_scheduled_action
from daily_automation import AVAILABILITY_GROUPS, MANILA, queue_daily_polls
from concern_reports import link


def request_refresh(chats, work_date, now, approved, request_id):
    chats = set(chats)
    if not chats or chats.difference(approved.intersection(AVAILABILITY_GROUPS)):
        raise ValueError("Survey groups must be explicitly identified approved teams")
    if chats.difference(automation_controls.enabled_chat_ids(approved)):
        raise ValueError("Survey group automation is paused")
    if work_date != now.astimezone(MANILA).date() + dt.timedelta(days=1):
        raise ValueError("Survey refresh must target tomorrow's PHT work date")
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("A valid request ID is required")
    if any(not page_access.enabled(chat) or not automation_controls.page_access_polls_enabled(chat) for chat in chats):
        raise ValueError("Page-access polls are not enabled for a requested team")
    existing = daily_poll_counts(chats, work_date)
    queued = queue_daily_polls(work_date, now, chats, existing_chats=set(existing))
    for chat in sorted(chats):
        queued += page_access.queue_poll(chat, now)
        queued += int(enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
            dedupe_key=f"staffing-survey-refresh:{work_date}:{chat}:{request_id}", payload={
                "staffing_survey_refresh": True, "staffing_survey_date": str(work_date),
                "text": "Preparing page-access and tomorrow-availability poll links.",
                "message_thread_id": AVAILABILITY_GROUPS[chat]["announcements"], "disable_notification": False}))
    return {"ok": True, "work_date": str(work_date), "chat_ids": sorted(chats),
        "queued_actions": queued, "delivery": "normal scheduler; existing answers preserved"}


def prepare_delivery(action, now):
    p = action["payload"]
    if not p.get("staffing_survey_refresh"):
        return True
    chat = int(action["chat_id"])
    target = dt.date.fromisoformat(p["staffing_survey_date"])
    if target <= now.astimezone(MANILA).date() or chat not in AVAILABILITY_GROUPS:
        p["_staffing_survey_obsolete"] = True
        return False
    daily = availability_reminders.poll(chat, target)
    records = [page_access.poll_record(chat, part) for part in range((len(page_access.page_map(chat))+8)//9)]
    if not daily or not daily.get("telegram_message_id") or not records or any(not r or not r.get("message_id") for r in records):
        return False  # Wait for normal scheduler delivery/registration; never publish untracked polls.
    lines = [f"{html.escape(AVAILABILITY_GROUPS[chat]['name'])} — PLEASE UPDATE YOUR VOTES", "",
        f"<b>Active tomorrow, {target} (PHT)?</b>",
        f'<a href="{link(chat,daily["telegram_message_id"])}">Choose Yes or No in the availability poll</a>.', "",
        "<b>Which pages do you actually have access to?</b>"]
    for i,r in enumerate(records,1):
        label = f"Page-access poll {i}" if len(records)>1 else "Page-access poll"
        lines.append(f'<a href="{link(chat,r["message_id"])}">{label}</a>: choose your pages, or No access.')
    lines += ["", "Existing votes are kept; update them if your access or availability changed.",
        "Tomorrow's vote is separate from being Ready to receive clients now."]
    p.update(text="\n".join(lines), parse_mode="HTML")
    return True

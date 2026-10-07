"""Persistent song delivery deadlines and owner-confirmed completion in topic 7692."""

from __future__ import annotations

import datetime as dt
import html
import math
import re

from cloud_store import (
    complete_song_job, create_song_job, enqueue_scheduled_action, new_client_actions,
    song_jobs, song_job_state, song_notices, song_reply_messages, mark_song_ack_queued,
)
from daily_automation import MANILA, TRABAWHO, TRABAWHO_CHAT_ID
from suno_store import suno_assignment_matches_project


DONE = re.compile(r"\s*(?:SONG\s+SENT|DONE)(?:\s+([A-F0-9]{8}))?\s*[.!]?\s*", re.I)


def timestamp(value) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def ensure_song_jobs(now: dt.datetime) -> None:
    existing = {j["payload"]["song_assignment_id"] for j in song_jobs(TRABAWHO_CHAT_ID)}
    for assignment in new_client_actions({TRABAWHO_CHAT_ID}):
        if assignment["id"] in existing:
            continue
        payload = assignment.get("payload") or {}
        started = payload.get("new_client_acknowledged_at")
        if not started or not payload.get("new_client_token") or not suno_assignment_matches_project(assignment):
            continue
        start = timestamp(started)
        create_song_job(TRABAWHO_CHAT_ID, int(assignment["id"]), {
            "song_assignment_id": int(assignment["id"]), "song_token": payload["new_client_token"],
            "song_assignee_id": payload["new_client_assignee_id"],
            "song_assignee_name": payload.get("new_client_assignee_name") or str(payload["new_client_assignee_id"]),
            "song_contact_name": payload.get("new_client_contact_name") or "Client",
            "song_started_at": start.isoformat(), "song_deadline_at": (start + dt.timedelta(hours=24)).isoformat(),
        }, now)


def due_stage(job: dict, now: dt.datetime) -> str | None:
    payload = job["payload"]
    if payload.get("song_completed_at"):
        return None
    start, deadline = timestamp(payload["song_started_at"]), timestamp(payload["song_deadline_at"])
    if now < start:
        return None
    if now >= deadline:
        return f"overdue-{int((now - deadline).total_seconds()) // 86400}"
    milestones = [(start, "start")]
    for hours in (12, 20, 23):
        milestones.append((start + dt.timedelta(hours=hours), f"hour-{hours}"))
    local = start.astimezone(MANILA)
    evening = dt.datetime.combine(local.date(), dt.time(21), tzinfo=MANILA)
    if start < evening < deadline:
        milestones.append((evening, "same-day"))
    return max((item for item in milestones if item[0] <= now), key=lambda item: item[0])[1]


def notice_text(job: dict, now: dt.datetime) -> str:
    payload = job["payload"]
    deadline = timestamp(payload["song_deadline_at"])
    minutes = math.ceil(abs((deadline - now).total_seconds()) / 60)
    duration = f"{minutes // 60}h {minutes % 60:02d}m"
    timing = f"OVERDUE by {duration}" if now >= deadline else f"Time remaining: {duration}"
    user = f'<a href="tg://user?id={int(payload["song_assignee_id"])}">{html.escape(payload["song_assignee_name"])}</a>'
    return "\n".join([
        "🎵 TRABAWHO SONG DELIVERY", f"{user} — {html.escape(payload['song_contact_name'])}",
        timing, f"24-hour deadline: {deadline.astimezone(MANILA).strftime('%b %d, %Y %I:%M %p')} PHT", "",
        "Please send the song to the client today. If you already sent it, confirm below.",
        f"Reply <code>SONG SENT {payload['song_token']}</code> or reply <code>DONE</code> to this reminder.",
        "Confirm after delivery to the client; uploading a file here does not confirm client delivery.",
    ])


def _matches(message: dict, job: dict, notices: list[dict]) -> bool:
    payload = job["payload"]
    author = message.get("author_id", (message.get("from") or {}).get("id"))
    thread = message.get("thread_id", message.get("message_thread_id"))
    if author != int(payload["song_assignee_id"]) or thread != TRABAWHO["songs"]:
        return False
    match = DONE.fullmatch(message.get("text") or message.get("caption") or "")
    if not match:
        return False
    if match[1]:
        return match[1].upper() == payload["song_token"].upper()
    reply = message.get("reply_to_message_id", (message.get("reply_to_message") or {}).get("message_id"))
    return reply is not None and any(
        notice.get("telegram_message_id") == reply and notice.get("payload", {}).get("song_job_id") == job["id"]
        for notice in notices
    )


def _record_completion(message: dict, job: dict, now: dt.datetime) -> bool:
    payload = job["payload"]
    value = message.get("edited_utc") or message.get("sent_utc")
    sent = timestamp(value) if value else dt.datetime.fromtimestamp(
        message.get("edit_date") or message.get("date") or now.timestamp(), dt.timezone.utc)
    if not timestamp(payload["song_started_at"]) <= sent <= now:
        return False
    if not payload.get("song_completed_at"):
        if not complete_song_job(int(job["id"]), sent, int(message["message_id"])):
            return False
        payload["song_completed_at"] = sent.isoformat()
        payload["song_completion_message_id"] = int(message["message_id"])
    _queue_completion_ack(job, now)
    return True


def _queue_completion_ack(job: dict, now: dt.datetime) -> int:
    payload = job["payload"]
    if payload.get("song_ack_queued_at"):
        return 0
    on_time = timestamp(payload["song_completed_at"]) <= timestamp(payload["song_deadline_at"])
    queued = int(enqueue_scheduled_action(
        chat_id=TRABAWHO_CHAT_ID, action_type="message",
        payload={"text": (
            f"✅ {payload['song_assignee_name']}, song delivery recorded for {payload['song_contact_name']} "
            f"({payload['song_token']}). {'Within 24 hours.' if on_time else 'Recorded after the 24-hour deadline.'} "
            "Reminders for this client have stopped."
        ), "message_thread_id": TRABAWHO["songs"], "disable_notification": False,
            "reply_parameters": {"message_id": payload["song_completion_message_id"], "allow_sending_without_reply": True}},
        scheduled_for=now, dedupe_key=f"trabawho-song-ack:{job['id']}",
    ))
    if mark_song_ack_queued(int(job["id"]), now):
        payload["song_ack_queued_at"] = now.isoformat()
    return queued


def confirm_song_reply(update: dict, allowed: set[int], now: dt.datetime) -> bool:
    message = update.get("message") or update.get("edited_message") or {}
    if TRABAWHO_CHAT_ID not in allowed or (message.get("chat") or {}).get("id") != TRABAWHO_CHAT_ID:
        return False
    if message.get("message_thread_id") != TRABAWHO["songs"] or not DONE.fullmatch(
        message.get("text") or message.get("caption") or ""):
        return False
    ensure_song_jobs(now)
    jobs, notices = song_jobs(TRABAWHO_CHAT_ID), song_notices(TRABAWHO_CHAT_ID)
    matches = [job for job in jobs if _matches(message, job, notices)]
    return len(matches) == 1 and _record_completion(message, matches[0], now)


def queue_song_followups(now: dt.datetime, allowed: set[int]) -> int:
    if TRABAWHO_CHAT_ID not in allowed:
        return 0
    ensure_song_jobs(now)
    jobs = song_jobs(TRABAWHO_CHAT_ID)
    outstanding = [job for job in jobs if not job["payload"].get("song_completed_at")]
    if outstanding:
        notices = song_notices(TRABAWHO_CHAT_ID)
        replies = song_reply_messages(TRABAWHO_CHAT_ID, TRABAWHO["songs"],
            min(timestamp(j["payload"]["song_started_at"]) for j in outstanding), now,
            {int(j["payload"]["song_assignee_id"]) for j in outstanding})
        for message in sorted(replies, key=lambda m: m.get("edited_utc") or m["sent_utc"]):
            matches = [j for j in outstanding if not j["payload"].get("song_completed_at") and _matches(message, j, notices)]
            if len(matches) == 1:
                _record_completion(message, matches[0], now)
    queued = 0
    for job in jobs:
        if job["payload"].get("song_completed_at"):
            queued += _queue_completion_ack(job, now)
            continue
        stage = due_stage(job, now)
        if stage is None:
            continue
        queued += int(enqueue_scheduled_action(
            chat_id=TRABAWHO_CHAT_ID, action_type="message",
            payload={"text": notice_text(job, now), "parse_mode": "HTML",
                "message_thread_id": TRABAWHO["songs"], "disable_notification": False,
                "song_job_id": job["id"], "song_stage": stage},
            scheduled_for=now, dedupe_key=f"trabawho-song-notice:{job['id']}:{stage}",
        ))
    return queued


def is_song_notice(action: dict) -> bool:
    return int(action.get("chat_id", 0)) == TRABAWHO_CHAT_ID and bool(action.get("payload", {}).get("song_job_id"))


def song_notice_delivery_allowed(action: dict, now: dt.datetime) -> bool:
    job = song_job_state(int(action["payload"]["song_job_id"]), TRABAWHO_CHAT_ID)
    if not job or job["payload"].get("song_completed_at"):
        return False
    # Suppress stale milestones after cron downtime, then send only the current stage.
    if action["payload"]["song_stage"] != due_stage(job, now):
        return False
    action["payload"]["text"] = notice_text(job, now)
    return True


def song_report_lines(work_date: dt.date, now: dt.datetime) -> list[str]:
    jobs = song_jobs(TRABAWHO_CHAT_ID)
    completed = [j for j in jobs if j["payload"].get("song_completed_at") and
                 timestamp(j["payload"]["song_completed_at"]).astimezone(MANILA).date() == work_date]
    open_jobs = [j for j in jobs if not j["payload"].get("song_completed_at") and timestamp(j["payload"]["song_started_at"]) <= now]
    overdue = [j for j in open_jobs if timestamp(j["payload"]["song_deadline_at"]) <= now]
    late = sum(timestamp(j["payload"]["song_completed_at"]) > timestamp(j["payload"]["song_deadline_at"]) for j in completed)
    lines = [f"Songs confirmed delivered on {work_date.isoformat()}: {len(completed)} ({late} after 24 hours)",
             f"Still outstanding as of report time: {len(open_jobs)}; overdue: {len(overdue)}"]
    for job in overdue:
        p = job["payload"]
        lines.append(f"Overdue: {p['song_assignee_name']} — {p['song_contact_name']} ({p['song_token']})")
    return lines

"""Persistent song delivery deadlines and owner-confirmed completion in topic 7692."""

from __future__ import annotations

import datetime as dt
import html
import math
import re

from cloud_store import (
    complete_song_job, create_song_job, enqueue_scheduled_action, new_client_actions,
    song_jobs, song_job_state, song_notices, song_reply_messages, mark_song_ack_queued,
    sync_song_owner,
    song_assignment_owner,
)
from daily_automation import MANILA, TRABAWHO, TRABAWHO_CHAT_ID, SUNO_GROUPS
from suno_store import suno_assignment_matches_project


DONE = re.compile(r"\s*(?:SONG\s+SENT|DONE)(?:\s+([A-F0-9]{8}))?\s*[.!]?\s*", re.I)


def timestamp(value) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def ensure_song_jobs(now: dt.datetime, chat_id: int = TRABAWHO_CHAT_ID) -> None:
    existing = {j["payload"]["song_assignment_id"]: j for j in song_jobs(chat_id)}
    for assignment in new_client_actions({chat_id}):
        payload = assignment.get("payload") or {}
        started = payload.get("new_client_acknowledged_at")
        if not started or not payload.get("new_client_token") or not suno_assignment_matches_project(assignment):
            continue
        old = existing.get(assignment["id"])
        if old:
            events = [e for e in payload.get("new_client_handover_events") or [] if e.get("state") == "accepted"]
            if (not old["payload"].get("song_completed_at") and
                    (old["payload"].get("song_assignee_id") != payload["new_client_assignee_id"] or
                     events and old["payload"].get("song_owner_since") != events[-1]["resolved_at"])):
                if not sync_song_owner(int(old["id"]), assignment, now):
                    raise RuntimeError("Song owner changed concurrently; retry synchronization")
            continue
        start = timestamp(started)
        create_song_job(chat_id, int(assignment["id"]), {
            "song_assignment_id": int(assignment["id"]), "song_token": payload["new_client_token"],
            "song_assignee_id": payload["new_client_assignee_id"],
            "song_assignee_name": payload.get("new_client_assignee_name") or str(payload["new_client_assignee_id"]),
            "song_contact_name": payload.get("new_client_contact_name") or "Client",
            "song_started_at": start.isoformat(), "song_deadline_at": (start + dt.timedelta(hours=24)).isoformat(),
            "song_owner_since": ([e for e in payload.get("new_client_handover_events") or [] if e.get("state") == "accepted"] or [{}])[-1].get("resolved_at") or started,
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
        "SUNO SONG DELIVERY", f"{user} — {html.escape(payload['song_contact_name'])}",
        timing, f"24-hour deadline: {deadline.astimezone(MANILA).strftime('%b %d, %Y %I:%M %p')} PHT",
        f"Reply <code>SONG SENT {payload['song_token']}</code> or reply <code>DONE</code> to this reminder.",
        "Confirm after sending to the client; a file upload alone does not confirm delivery.",
    ])


def _matches(message: dict, job: dict, notices: list[dict]) -> bool:
    payload = job["payload"]
    author = message.get("author_id", (message.get("from") or {}).get("id"))
    thread = message.get("thread_id", message.get("message_thread_id"))
    chat_id = int(job.get("chat_id", TRABAWHO_CHAT_ID))
    if author != int(payload["song_assignee_id"]) or thread != SUNO_GROUPS[chat_id]["songs"]:
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
    if payload.get("song_owner_since") and sent < timestamp(payload["song_owner_since"]):
        return False
    if (payload.get("song_owner_since") and
            song_assignment_owner(int(payload["song_assignment_id"]), int(job.get("chat_id", TRABAWHO_CHAT_ID))) != int(payload["song_assignee_id"])):
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
    chat_id = int(job.get("chat_id", TRABAWHO_CHAT_ID))
    queued = int(enqueue_scheduled_action(
        chat_id=chat_id, action_type="message",
        payload={"text": (
            f"Delivered: {payload['song_contact_name']} ({payload['song_token']}). "
            f"{'Within 24 hours.' if on_time else 'After the 24-hour deadline.'} Reminders stopped."
        ), "message_thread_id": SUNO_GROUPS[chat_id]["songs"], "disable_notification": False,
            "reply_parameters": {"message_id": payload["song_completion_message_id"], "allow_sending_without_reply": True}},
        scheduled_for=now, dedupe_key=f"trabawho-song-ack:{job['id']}",
    ))
    if mark_song_ack_queued(int(job["id"]), now):
        payload["song_ack_queued_at"] = now.isoformat()
    return queued


def confirm_song_reply(update: dict, allowed: set[int], now: dt.datetime) -> bool:
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id not in allowed or chat_id not in SUNO_GROUPS:
        return False
    if message.get("message_thread_id") != SUNO_GROUPS[chat_id]["songs"] or not DONE.fullmatch(
        message.get("text") or message.get("caption") or ""):
        return False
    ensure_song_jobs(now) if chat_id == TRABAWHO_CHAT_ID else ensure_song_jobs(now, chat_id)
    jobs, notices = song_jobs(chat_id), song_notices(chat_id)
    matches = [job for job in jobs if _matches(message, job, notices)]
    return len(matches) == 1 and _record_completion(message, matches[0], now)


def queue_song_followups(now: dt.datetime, allowed: set[int]) -> int:
    return sum(_queue_team_song_followups(now, chat) for chat in sorted(allowed.intersection(SUNO_GROUPS)) if SUNO_GROUPS[chat].get("songs"))


def _queue_team_song_followups(now: dt.datetime, chat_id: int) -> int:
    if not SUNO_GROUPS[chat_id].get("songs"):
        return 0
    ensure_song_jobs(now) if chat_id == TRABAWHO_CHAT_ID else ensure_song_jobs(now, chat_id)
    jobs = song_jobs(chat_id)
    outstanding = [job for job in jobs if not job["payload"].get("song_completed_at")]
    if outstanding:
        notices = song_notices(chat_id)
        replies = song_reply_messages(chat_id, SUNO_GROUPS[chat_id]["songs"],
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
            chat_id=chat_id, action_type="message",
            payload={"text": notice_text(job, now), "parse_mode": "HTML",
                "message_thread_id": SUNO_GROUPS[chat_id]["songs"], "disable_notification": False,
                "song_job_id": job["id"], "song_stage": stage, "song_owner_id": job["payload"]["song_assignee_id"]},
            scheduled_for=now, dedupe_key=f"trabawho-song-notice:{job['id']}:owner-{job['payload']['song_assignee_id']}:{stage}",
        ))
    return queued


def is_song_notice(action: dict) -> bool:
    return int(action.get("chat_id", 0)) in SUNO_GROUPS and bool(action.get("payload", {}).get("song_job_id"))


def song_notice_delivery_allowed(action: dict, now: dt.datetime) -> bool:
    chat_id = int(action["chat_id"])
    job = song_job_state(int(action["payload"]["song_job_id"]), chat_id)
    if not job or job["payload"].get("song_completed_at"):
        return False
    if action["payload"].get("song_owner_id") is not None and action["payload"]["song_owner_id"] != job["payload"]["song_assignee_id"]:
        return False
    if (job["payload"].get("song_owner_since") and
            song_assignment_owner(int(job["payload"]["song_assignment_id"]), chat_id) != int(job["payload"]["song_assignee_id"])):
        return False
    # Legacy queued reminders should also name the current owner.
    action["payload"]["text"] = notice_text(job, now)
    # Suppress stale milestones after cron downtime, then send only the current stage.
    if action["payload"]["song_stage"] != due_stage(job, now):
        return False
    action["payload"]["text"] = notice_text(job, now)
    return True


def song_report_lines(work_date: dt.date, now: dt.datetime, chat_id: int = TRABAWHO_CHAT_ID) -> list[str]:
    jobs = song_jobs(chat_id)
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

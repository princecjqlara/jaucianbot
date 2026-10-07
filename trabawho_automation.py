"""Trabawho availability, two-per-member quota, and proposed advertising budget."""

from __future__ import annotations

import datetime as dt
import html
import urllib.parse
from collections import defaultdict

from cloud_store import daily_poll_answers, daily_poll_counts, enqueue_scheduled_action, new_client_actions, request
from daily_automation import MANILA, TRABAWHO, TRABAWHO_CHAT_ID, queue_daily_polls


FRIENDLY_PLAN_VERSION = "friendly-v1"
TRABAWHO_CHECKIN_HOURS = (10, 16, 20)


def plan_totals(active_members: int) -> tuple[int, int]:
    if active_members < 0:
        raise ValueError("Active member count cannot be negative")
    quota = active_members * 2
    return quota, quota * 150


def plan_text(work_date: dt.date, active_members: int, today: dt.date) -> str:
    label = "TODAY" if work_date == today else "TOMORROW"
    date_label = work_date.strftime('%A, %B %d, %Y')
    if active_members:
        teammate = "teammate" if active_members == 1 else "teammates"
        status = f"{active_members} {teammate} marked Active for {label.lower()}."
        next_step = "Client assignments will be shared fairly among the Active team."
    else:
        status = "No teammates are marked Active yet."
        next_step = "Please vote in the poll so we can prepare the day's assignments."
    return "\n".join([
        f"🌟 TRABAWHO TEAM UPDATE — {label}",
        f"📅 {date_label} (PHT)", "",
        "Hi team! 👋",
        status,
        next_step,
        "Please keep an eye on the New Client topic and reply when your client comes in.",
        "Let's have a smooth and successful day together! 💛",
    ])


def _mention(user: dict) -> str:
    user_id = user.get("user_id")
    name = html.escape(user.get("user_name") or str(user_id or "team member"))
    return f'<a href="tg://user?id={int(user_id)}">{name}</a>' if user_id else name


def _assignment_counts(work_date: dt.date) -> tuple[int, dict[int, int]]:
    assigned_keys: set[str] = set()
    by_user: defaultdict[int, int] = defaultdict(int)
    for row in new_client_actions({TRABAWHO_CHAT_ID}):
        payload = row.get("payload") or {}
        if not payload.get("new_client_token") or payload.get("new_client_work_date") != work_date.isoformat():
            continue
        contact_key = payload.get("new_client_contact_identity") or payload.get("new_client_contact_id")
        if contact_key:
            assigned_keys.add(str(contact_key))
        user_id = payload.get("new_client_assignee_id")
        if user_id is not None and payload.get("new_client_acknowledged_at"):
            by_user[int(user_id)] += 1
    return len(assigned_keys), dict(by_user)


def availability_checkin_text(work_date: dt.date, answers: list[dict], assigned_by_user: dict[int, int]) -> str:
    active = [row for row in answers if row.get("active")]
    inactive = [row for row in answers if not row.get("active")]
    waiting = [row for row in active if int(row.get("user_id")) not in assigned_by_user]
    lines = [
        "💛 TRABAWHO AVAILABILITY CHECK-IN",
        f"📅 {work_date.strftime('%A, %B %d, %Y')} (PHT)", "",
        "Hi team! Just a gentle check-in so we can support everyone well today. 👋",
    ]
    if waiting:
        lines += [
            "A quick note for " + ", ".join(_mention(user) for user in waiting) + ": "
            "you marked Active today, and we haven't seen a working response from you yet. "
            "Please check New Client when you're ready—no pressure.",
        ]
    if inactive:
        lines += [
            "Thanks for updating us, " + ", ".join(_mention(user) for user in inactive) + ": "
            "you marked Not Active today. If your plans change, you can update the poll anytime.",
        ]
    lines += [
        "If you haven't voted yet, please choose Active or Not Active so we can plan fairly.",
        "Thank you, team. We appreciate you and hope you have a good day! 🌟",
    ]
    return "\n".join(lines)


def progress_text(work_date: dt.date, count_row: dict | None, assigned: int) -> str:
    date_label = work_date.strftime('%A, %B %d, %Y')
    if not count_row:
        return "\n".join([
            "📣 TRABAWHO TEAM PROGRESS", f"📅 {date_label} (PHT)", "",
            "We're waiting for today's Active poll before setting the client target.",
            "Please vote when you can so the team plan is ready. 💛",
        ])
    active = int(count_row.get("active_workers", 0))
    if active <= 0:
        return "\n".join([
            "📣 TRABAWHO TEAM PROGRESS", f"📅 {date_label} (PHT)", "",
            "No Active responses have been recorded yet, so there is no client target to track.",
            "Please vote in the poll when you can. 💛",
        ])
    quota = active * 2
    remaining = max(quota - assigned, 0)
    if remaining:
        status = f"Client progress: {assigned} of {quota} planned • {remaining} left to go."
        encouragement = "Keep an eye on New Client and let's keep the momentum going together! 🌟"
    else:
        status = f"Client progress: {assigned} of {quota} planned • today's target is complete!"
        encouragement = "Wonderful work, team! Please keep supporting any open clients. 🎉"
    return "\n".join([
        "📣 TRABAWHO TEAM PROGRESS", f"📅 {date_label} (PHT)", "",
        status,
        encouragement,
        "This is a quick team update; the organized details remain in Daily Reports.",
    ])


def queue_trabawho_followups(now: dt.datetime) -> int:
    local = now.astimezone(MANILA)
    if local.hour not in TRABAWHO_CHECKIN_HOURS:
        return 0
    work_date = local.date()
    count_row = daily_poll_counts({TRABAWHO_CHAT_ID}, work_date).get(TRABAWHO_CHAT_ID)
    if not count_row or not count_row.get("poll_id"):
        return 0
    answers = daily_poll_answers(count_row["poll_id"])
    assigned, assigned_by_user = _assignment_counts(work_date)
    checkin_key = f"trabawho-availability-checkin:{work_date.isoformat()}:{local.hour}"
    queued = 0
    if enqueue_scheduled_action(
        chat_id=TRABAWHO_CHAT_ID, action_type="message",
        payload={"text": availability_checkin_text(work_date, answers, assigned_by_user),
                 "parse_mode": "HTML", "message_thread_id": TRABAWHO["general"],
                 "disable_notification": False},
        scheduled_for=now, dedupe_key=checkin_key,
    ):
        queued += 1
    progress_key = f"trabawho-progress:{work_date.isoformat()}:{local.hour}:{int(count_row.get('active_workers', 0))}:{assigned}"
    if enqueue_scheduled_action(
        chat_id=TRABAWHO_CHAT_ID, action_type="message",
        payload={"text": progress_text(work_date, count_row, assigned),
                 "message_thread_id": TRABAWHO["announcements"], "disable_notification": False,
                 "trabawho_progress_date": work_date.isoformat(),
                 "trabawho_active_count": int(count_row.get("active_workers", 0)),
                 "trabawho_assigned_count": assigned},
        scheduled_for=now, dedupe_key=progress_key,
    ):
        queued += 1
    return queued


def _queue_plan(work_date: dt.date, count_row: dict, now: dt.datetime) -> int:
    workers = int(count_row["active_workers"])
    # Remember the last queued count. Unchanged counts produce no repeat posts;
    # concurrent cron and poll callbacks share a minute-scoped database dedupe key.
    latest = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,count:payload->trabawho_active_count,format:payload->>trabawho_plan_format",
        "chat_id": f"eq.{TRABAWHO_CHAT_ID}",
        "payload->>trabawho_work_date": f"eq.{work_date.isoformat()}",
        "order": "id.desc", "limit": 1,
    })) or []
    if latest and latest[0].get("count") == workers and latest[0].get("format") == FRIENDLY_PLAN_VERSION:
        return 0
    minute = int(now.timestamp()) // 60
    return int(enqueue_scheduled_action(
        chat_id=TRABAWHO_CHAT_ID, action_type="message",
        payload={
            "text": plan_text(work_date, workers, now.astimezone(MANILA).date()),
            "message_thread_id": TRABAWHO["announcements"],
            "disable_notification": False,
            "trabawho_work_date": work_date.isoformat(), "trabawho_active_count": workers,
            "trabawho_plan_format": FRIENDLY_PLAN_VERSION,
        },
        scheduled_for=now,
        dedupe_key=f"trabawho-plan:{work_date.isoformat()}:{minute}:{workers}",
    ))


def queue_trabawho_automation(now: dt.datetime, allowed: set[int]) -> int:
    if TRABAWHO_CHAT_ID not in allowed:
        return 0
    today = now.astimezone(MANILA).date()
    queued = 0
    for work_date in (today, today + dt.timedelta(days=1)):
        counts = daily_poll_counts({TRABAWHO_CHAT_ID}, work_date)
        count = counts.get(TRABAWHO_CHAT_ID)
        if count is None:
            queued += queue_daily_polls(work_date, now, {TRABAWHO_CHAT_ID})
        else:
            queued += _queue_plan(work_date, count, now)
    queued += queue_trabawho_followups(now)
    return queued


def trabawho_poll_work_date(poll_id: str, now: dt.datetime, allowed: set[int]) -> dt.date | None:
    if TRABAWHO_CHAT_ID not in allowed:
        return None
    today = now.astimezone(MANILA).date()
    for work_date in (today, today + dt.timedelta(days=1)):
        row = daily_poll_counts({TRABAWHO_CHAT_ID}, work_date).get(TRABAWHO_CHAT_ID)
        if row and row.get("poll_id") == poll_id:
            return work_date
    return None

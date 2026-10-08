"""Trabawho availability, two-per-member quota, and proposed advertising budget."""

from __future__ import annotations

import datetime as dt
import html
import urllib.parse
from collections import defaultdict

from cloud_store import daily_poll_answers, daily_poll_counts, enqueue_scheduled_action, new_client_actions, request
from daily_automation import MANILA, TRABAWHO, TRABAWHO_CHAT_ID, queue_daily_polls
from suno_store import suno_assignment_matches_project


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
    from hourly_availability import enabled
    if enabled(work_date):
        next_step = "Choose every hour you can work in all three availability polls. Clients rotate among members available that hour."
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
    name = html.escape((user.get("user_name") or str(user_id or "team member"))[:64])
    return f'<a href="tg://user?id={int(user_id)}">{name}</a>' if user_id else name


def _mentions(users: list[dict]) -> str:
    mentions = []
    size = 0
    for user in users:
        mention = _mention(user)
        if size + len(mention) + 2 > 1000:
            break
        mentions.append(mention)
        size += len(mention) + 2
    remaining = len(users) - len(mentions)
    return ", ".join(mentions) + (f" and {remaining} other teammates" if remaining else "")


def _assignment_counts(work_date: dt.date) -> tuple[int, dict[int, int]]:
    assigned_keys: set[str] = set()
    by_user: defaultdict[int, int] = defaultdict(int)
    for row in new_client_actions({TRABAWHO_CHAT_ID}):
        payload = row.get("payload") or {}
        if not payload.get("new_client_token") or payload.get("new_client_work_date") != work_date.isoformat():
            continue
        if not suno_assignment_matches_project(row):
            continue
        if row.get("status") == "cancelled" and not payload.get("new_client_acknowledged_at"):
            continue
        if not (row.get("sent_at") or payload.get("_first_delivery_at") or payload.get("new_client_acknowledged_at")):
            continue
        contact_key = payload.get("new_client_contact_identity") or payload.get("new_client_contact_id")
        if contact_key:
            assigned_keys.add(str(contact_key))
        user_id = payload.get("new_client_assignee_id")
        if user_id is not None:
            by_user[int(user_id)] += int(bool(payload.get("new_client_acknowledged_at")))
    return len(assigned_keys), dict(by_user)


def availability_checkin_text(work_date: dt.date, answers: list[dict], assigned_by_user: dict[int, int], *, now: dt.datetime | None = None) -> str:
    active = [row for row in answers if row.get("active")]
    from hourly_availability import current_members, enabled
    if now:
        active = current_members(active, now)
    inactive = [row for row in answers if not row.get("active")]
    waiting = [row for row in active if assigned_by_user.get(int(row.get("user_id"))) == 0]
    lines = [
        "💛 TRABAWHO AVAILABILITY CHECK-IN",
        f"📅 {work_date.strftime('%A, %B %d, %Y')} (PHT)", "",
        "Hi team! Just a gentle check-in so we can support everyone well today. 👋",
    ]
    if waiting:
        lines += [
            "A quick note for " + _mentions(waiting) + ": "
            "you marked Active today, and we haven't seen a working response from you yet. "
            "Please check New Client when you're ready—no pressure.",
        ]
    if inactive:
        lines += [
            "Thanks for updating us, " + _mentions(inactive) + ": "
            "you marked Not Active today. If your plans change, you can update the poll anytime.",
        ]
    lines += [
        "If you haven't voted yet, please choose Active or Not Active so we can plan fairly.",
        "Thank you, team. We appreciate you and hope you have a good day! 🌟",
    ]
    if enabled(work_date):
        lines = [line.replace("choose Active or Not Active", "select the hours you can work in all three polls")
                 .replace("you marked Active today", "you're scheduled this hour")
                 .replace("you marked Not Active today", "you haven't selected any available hours today") for line in lines]
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
        status = f"Clients assigned: {assigned} of {quota} planned • {remaining} left to assign."
        encouragement = "Keep an eye on New Client and let's keep the momentum going together! 🌟"
    else:
        status = f"Clients assigned: {assigned} of {quota} planned • today's assignment plan is filled!"
        encouragement = "Thank you, team! Please keep supporting your clients through delivery. 💛"
    return "\n".join([
        "📣 TRABAWHO TEAM PROGRESS", f"📅 {date_label} (PHT)", "",
        status,
        encouragement,
        "Sales and completed deliveries are tracked separately in Daily Reports.",
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
        payload={"text": availability_checkin_text(work_date, answers, assigned_by_user, now=now),
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
    from hourly_availability import context
    poll = context(poll_id, {TRABAWHO_CHAT_ID})
    if poll:
        work_date = dt.date.fromisoformat(poll["payload"]["work_date"])
        today = now.astimezone(MANILA).date()
        return work_date if work_date in (today, today + dt.timedelta(days=1)) else None
    today = now.astimezone(MANILA).date()
    for work_date in (today, today + dt.timedelta(days=1)):
        row = daily_poll_counts({TRABAWHO_CHAT_ID}, work_date).get(TRABAWHO_CHAT_ID)
        if row and row.get("poll_id") == poll_id:
            return work_date
    return None

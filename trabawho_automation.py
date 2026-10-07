"""Trabawho availability, two-per-member quota, and proposed advertising budget."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from decimal import Decimal

from cloud_store import daily_poll_counts, enqueue_scheduled_action, request
from daily_automation import MANILA, TRABAWHO, TRABAWHO_CHAT_ID, money, queue_daily_polls


def plan_totals(active_members: int) -> tuple[int, int]:
    if active_members < 0:
        raise ValueError("Active member count cannot be negative")
    quota = active_members * 2
    return quota, quota * 150


def plan_text(work_date: dt.date, active_members: int, today: dt.date) -> str:
    quota, ads_budget = plan_totals(active_members)
    label = "TODAY" if work_date == today else "TOMORROW"
    return "\n".join([
        f"📣 TRABAWHO — {label}'S PLAN",
        f"📅 {work_date.strftime('%A, %B %d, %Y')} (PHT)", "",
        f"Active members: {active_members}",
        f"Team quota: {active_members} × 2 = {quota}",
        f"Ads budget: {active_members} × 2 × ₱150 = {money(Decimal(ads_budget))}", "",
        "Completed Suno clients rotate among members who selected Active for this workday.",
    ])


def _queue_plan(work_date: dt.date, count_row: dict, now: dt.datetime) -> int:
    workers = int(count_row["active_workers"])
    # Remember the last queued count. Unchanged counts produce no repeat posts;
    # concurrent cron and poll callbacks share a minute-scoped database dedupe key.
    latest = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,count:payload->trabawho_active_count",
        "chat_id": f"eq.{TRABAWHO_CHAT_ID}",
        "payload->>trabawho_work_date": f"eq.{work_date.isoformat()}",
        "order": "id.desc", "limit": 1,
    })) or []
    if latest and latest[0].get("count") == workers:
        return 0
    minute = int(now.timestamp()) // 60
    return int(enqueue_scheduled_action(
        chat_id=TRABAWHO_CHAT_ID, action_type="message",
        payload={
            "text": plan_text(work_date, workers, now.astimezone(MANILA).date()),
            "message_thread_id": TRABAWHO["announcements"],
            "disable_notification": False,
            "trabawho_work_date": work_date.isoformat(), "trabawho_active_count": workers,
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

"""Daily Trabawho receipt earnings, active-member plan, and song delivery summary."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from decimal import Decimal

from cloud_store import daily_poll_counts, enqueue_scheduled_action, receipt_messages, record_automation_marker, request
from daily_automation import DAILY_REPORTS_CHAT_ID, MANILA, TRABAWHO, TRABAWHO_CHAT_ID, money, split_message
from trabawho_automation import plan_totals
from trabawho_receipts import receipt_report
from trabawho_songs import song_report_lines


def build_trabawho_report(work_date: dt.date, now: dt.datetime) -> str:
    start = dt.datetime.combine(work_date, dt.time.min, tzinfo=MANILA)
    report = receipt_report(receipt_messages(TRABAWHO_CHAT_ID,
        start.astimezone(dt.timezone.utc), (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)), work_date, work_date)
    count = daily_poll_counts({TRABAWHO_CHAT_ID}, work_date).get(TRABAWHO_CHAT_ID)
    lines = ["📊 TRABAWHO DAILY REPORT", f"{work_date.strftime('%A, %B %d, %Y')} (PHT)", ""]
    if count:
        active = int(count["active_workers"])
        quota, budget = plan_totals(active)
        lines += [f"Active members: {active}", f"Team quota: {quota} (Active × 2)",
                  f"Planned ads budget: {money(Decimal(budget))} (Active × 2 × ₱150)"]
    else:
        lines.append("No tracked Active poll for this date; quota and ads budget are unavailable.")
    total = report["totals"]
    lines += ["", "RECEIPTS — RECORDED AMOUNTS",
              f"Gross receipts (including tips/extras): {money(total['gross'])}",
              f"Recorded salary (parenthesized amounts): {money(total['salary'])}",
              f"Labeled tips: {money(total['labeled_tips'])}; worker tip share: {money(total['tip_salary'])}",
              f"Gross less recorded salary, before expenses: {money(total['company_share'])}",
              f"Accepted receipt posts: {total['receipt_posts']} (not a unique-client count)", ""]
    for worker in report["workers"]:
        lines.append(f"{worker['name']}: gross {money(worker['gross'])} | salary {money(worker['salary'])}"
                     f" | {worker['receipt_posts']} receipts | {worker['review_posts']} awaiting review")
    if not total["receipt_posts"]:
        lines.append("No readable receipts recorded for this date; check for missing posts or image-only receipts.")
    review = len(report["review"])
    images = report["coverage"]["attachment_only_posts"]
    unresolved = len(report["unresolved_topic_messages"])
    if review or images or unresolved:
        lines += ["", f"Review needed: {review} posts; {images} image-only posts; {unresolved} unresolved-topic candidates.",
                  "Amounts above include accepted captions only and may be incomplete."]
        for item in report["review"][:10]:
            lines.append(f"Review receipt {item['message_id']}: {item['url']}")
    lines += ["", "SONG DELIVERY", *song_report_lines(work_date, now), "",
              "Salary here means recorded earnings, not proof of payout. The ads budget is planned, not actual spending.",
              "Receipt posts and song confirmations do not establish unique-client quota completion."]
    return "\n".join(lines)


def queue_trabawho_daily_report(now: dt.datetime, allowed: set[int]) -> int:
    if TRABAWHO_CHAT_ID not in allowed:
        return 0
    local = now.astimezone(MANILA)
    if local.time() < dt.time(0, 5):
        return 0
    work_date = local.date() - dt.timedelta(days=1)
    targets = [(TRABAWHO_CHAT_ID, TRABAWHO["announcements"])]
    if DAILY_REPORTS_CHAT_ID in allowed:
        targets.append((DAILY_REPORTS_CHAT_ID, None))
    missing = []
    for chat_id, topic in targets:
        marker = f"trabawho-daily-report-complete:{work_date.isoformat()}:{chat_id}"
        if not request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id", "chat_id": f"eq.{chat_id}", "dedupe_key": f"eq.{marker}", "limit": 1,
        })):
            missing.append((chat_id, topic, marker))
    if not missing:
        return 0
    parts = split_message(build_trabawho_report(work_date, now))
    queued = 0
    for chat_id, topic, marker in missing:
        for number, text in enumerate(parts, 1):
            payload = {"text": text, "disable_notification": False}
            if topic is not None:
                payload["message_thread_id"] = topic
            queued += int(enqueue_scheduled_action(
                chat_id=chat_id, action_type="message", payload=payload, scheduled_for=now,
                dedupe_key=f"trabawho-daily-report:{work_date.isoformat()}:{chat_id}:{number}",
            ))
        # The marker means all parts are durably queued, not that Telegram delivered them.
        record_automation_marker(chat_id, marker, now)
    return queued

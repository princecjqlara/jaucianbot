"""Daily Telegram availability polls, quotas, and sales reports."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict
from decimal import Decimal
from zoneinfo import ZoneInfo

from cloud_store import archive_messages, daily_poll_counts, enqueue_scheduled_action


MANILA = ZoneInfo("Asia/Manila")
DAILY_REPORTS_CHAT_ID = -1004362432984
AUTOMATION_START_DATE = dt.date(2026, 9, 19)

GROUPS = {
    -1004423057797: {
        "name": "Veo Ollie",
        "announcements": 4794,
        "active": 4536,
        "done": 17,
        "close": 16,
        "pages": {"vista": "Vista", "loki": "Loki"},
    },
    -1003962888977: {
        "name": "Veo Jessa",
        "announcements": 5,
        "active": 3050,
        "done": 7,
        "close": 6,
        "pages": {"manawaristudio": "Manawari Studios", "manawaristudios": "Manawari Studios"},
    },
    -1003647732254: {
        "name": "Veo",
        "announcements": 248,
        "active": 25893,
        "done": 1135,
        "close": 1132,
        "pages": {"azshinari": "Azshinari", "samakanamedia": "Sama Ka Na Media"},
    },
    -1004461399292: {
        "name": "Veo Jel",
        "announcements": 2,
        "active": 2917,
        "done": 6,
        "close": 8,
        "pages": {"onset": "Onset Media Agency", "onsetmediaagency": "Onset Media Agency"},
    },
}

PAGE_RE = re.compile(r"(?im)^\s*page(?:\s+name)?\s*:\s*([^\n]+)")
PRICE_RE = re.compile(r"(?im)^\s*(?:price\s*deal|pd)\s*:\s*(?:php|₱)?\s*([\d,]+(?:\.\d+)?)")
CLOSE_RE = re.compile(r"(?im)^\s*close\s*deals?\s*:\s*(\d+)")
DONE_MARKER_RE = re.compile(r"(?im)^\s*(?:paid\b|total\s*(?:payment|pay)\s*:)")


def normalize_page(text: str, configured: dict[str, str]) -> str:
    value = text.split("(", 1)[0].strip(" .:-")
    key = re.sub(r"[^a-z0-9]", "", value.casefold())
    return configured.get(key, value.title() or "Unknown page")


def parse_page(text: str, configured: dict[str, str]) -> str | None:
    match = PAGE_RE.search(text)
    return normalize_page(match.group(1), configured) if match else None


def parse_price(text: str) -> Decimal | None:
    match = PRICE_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except Exception:
        return None


def parse_close_count(text: str) -> int | None:
    match = CLOSE_RE.search(text)
    if match:
        return int(match.group(1))
    return 1 if parse_price(text) is not None else None


def money(value: Decimal) -> str:
    formatted = f"{value:,.2f}"
    if formatted.endswith(".00"):
        formatted = formatted[:-3]
    return "₱" + formatted


def quota_for(count_row: dict | None) -> tuple[int, int, bool]:
    if not count_row:
        return 0, 0, False
    workers = int(count_row.get("active_workers", 0))
    return workers, math.ceil(workers * 0.8), True


def messages_for_day(chat_id: int, work_date: dt.date) -> tuple[list[dict], bool]:
    start_local = dt.datetime.combine(work_date, dt.time.min, tzinfo=MANILA)
    end_local = start_local + dt.timedelta(days=1)
    rows = archive_messages(
        {chat_id},
        since=start_local.astimezone(dt.timezone.utc),
        limit=500,
        group=chat_id,
    )
    selected = []
    for row in rows:
        sent = dt.datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00"))
        if start_local <= sent.astimezone(MANILA) < end_local:
            selected.append(row)
    return selected, len(rows) == 500


def build_group_report(
    chat_id: int,
    work_date: dt.date,
    count_row: dict | None,
    *,
    historical: bool = False,
) -> str:
    config = GROUPS[chat_id]
    rows, capped = messages_for_day(chat_id, work_date)
    page_stats = defaultdict(lambda: {"cd": 0, "dd": 0, "gross": Decimal("0")})
    employee_stats = defaultdict(lambda: {"dd": 0, "gross": Decimal("0")})
    counted_close_reports: set[tuple[str, str]] = set()
    skipped_done = 0
    skipped_close = 0

    for row in rows:
        thread_id = row.get("thread_id")
        text = row.get("text") or ""
        historical_done = (
            historical
            and thread_id is None
            and PAGE_RE.search(text)
            and PRICE_RE.search(text)
            and DONE_MARKER_RE.search(text)
        )
        historical_close = (
            historical
            and thread_id is None
            and PAGE_RE.search(text)
            and PRICE_RE.search(text)
            and not DONE_MARKER_RE.search(text)
        )
        if thread_id == config["done"] or historical_done:
            page = parse_page(text, config["pages"])
            price = parse_price(text)
            if page is None or price is None:
                if text.strip():
                    skipped_done += 1
                continue
            author = (row.get("author_name") or "Unknown employee").strip()
            page_stats[page]["dd"] += 1
            page_stats[page]["gross"] += price
            employee_stats[author]["dd"] += 1
            employee_stats[author]["gross"] += price
        elif thread_id == config["close"] or historical_close:
            page = parse_page(text, config["pages"])
            close_count = parse_close_count(text)
            if page is None or close_count is None:
                if text.strip():
                    skipped_close += 1
                continue
            if CLOSE_RE.search(text):
                author_key = ((row.get("author_name") or "Unknown employee").casefold(), page)
                if author_key in counted_close_reports:
                    continue
                counted_close_reports.add(author_key)
            page_stats[page]["cd"] += close_count

    for canonical in dict.fromkeys(config["pages"].values()):
        page_stats[canonical]

    cd_total = sum(values["cd"] for values in page_stats.values())
    dd_total = sum(values["dd"] for values in page_stats.values())
    gross = sum((values["gross"] for values in page_stats.values()), Decimal("0"))
    workers, quota, has_poll = quota_for(count_row)
    qualified = has_poll and cd_total >= quota and dd_total >= quota
    rate = Decimal("0.40") if qualified else Decimal("0.35")
    salaries = {name: values["gross"] * rate for name, values in employee_stats.items()}
    salary_total = sum(salaries.values(), Decimal("0"))
    profit = gross - salary_total

    date_label = work_date.strftime("%B %d, %Y")
    lines = [
        f"📊 {'HISTORICAL SAMPLE — ' if historical else 'DAILY REPORT — '}{date_label}",
        f"🏢 {config['name']}",
        "",
    ]
    if has_poll:
        lines.extend([
            f"👥 Active workers: {workers}",
            f"🎯 Quota: {quota} DD and {quota} CD (ceil({workers} × 0.8))",
        ])
    else:
        lines.extend(["👥 Active workers: no tracked poll", "🎯 Quota: unavailable for this setup day"])
    lines.extend([
        f"✅ Results: {dd_total} DD | {cd_total} CD",
        f"💼 Commission rate: {int(rate * 100)}%",
        "",
        "PAGE TOTALS",
    ])
    for page, values in sorted(page_stats.items()):
        lines.append(f"• {page}: {values['dd']} DD | {values['cd']} CD | {money(values['gross'])}")
    lines.extend(["", "EMPLOYEE SALES & COMMISSION"])
    ranking = sorted(employee_stats.items(), key=lambda item: (-item[1]["gross"], item[0].casefold()))
    if ranking:
        for index, (name, values) in enumerate(ranking, 1):
            lines.append(
                f"{index}. {name} — {values['dd']} DD | {money(values['gross'])} sales | {money(salaries[name])} pay"
            )
    else:
        lines.append("No parsed done deals.")
    lines.extend([
        "",
        "FINANCIALS",
        f"Gross price deals: {money(gross)}",
        f"Less employee commissions: {money(salary_total)}",
        f"Profit after commissions: {money(profit)}",
    ])
    if skipped_done or skipped_close or capped:
        lines.extend(["", "DATA CHECK"])
        if skipped_done:
            lines.append(f"• {skipped_done} Done Deals message(s) missing a readable page or Price Deal.")
        if skipped_close:
            lines.append(f"• {skipped_close} Close Deals message(s) missing a readable page or count.")
        if capped:
            lines.append("• The 500-message daily retrieval limit was reached; review this group for overflow.")
    if historical:
        lines.extend([
            "",
            "SOURCE NOTE",
            "Reconstructed from the Telegram Desktop export. Exported messages have no topic IDs, so paid/Total Payment entries count as DD and unpaid Price Deal entries count as CD.",
        ])
    return "\n".join(lines)


def historical_active_count(chat_id: int, work_date: dt.date) -> int | None:
    previous_date = work_date - dt.timedelta(days=1)
    rows, _ = messages_for_day(chat_id, previous_date)
    excluded = re.compile(r"(?i)inactive|active\s+tomorrow|how\s+many|answer\s+the\s+poll|\bguys\b|\bilan\b|\bsino\b")
    workers = set()
    for row in rows:
        text = row.get("text") or ""
        author = (row.get("author_name") or "").strip()
        if author and re.search(r"(?i)\bactive\b", text) and "?" not in text and not excluded.search(text):
            workers.add(author.casefold())
    return len(workers) if workers else None


def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit and current:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def queue_daily_polls(work_date: dt.date, now: dt.datetime, allowed: set[int]) -> int:
    queued = 0
    date_label = work_date.strftime("%B %d, %Y")
    for chat_id, config in GROUPS.items():
        if chat_id not in allowed:
            continue
        payload = {
            "question": f"Will you be active tomorrow? — {date_label}",
            "options": ["✅ Active", "❌ Not active"],
            "is_anonymous": False,
            "allows_multiple_answers": False,
            "disable_notification": False,
            "message_thread_id": config["active"],
            "daily_poll_date": work_date.isoformat(),
        }
        if enqueue_scheduled_action(
            chat_id=chat_id,
            action_type="poll",
            payload=payload,
            scheduled_for=now,
            dedupe_key=f"daily-poll:{work_date.isoformat()}:{chat_id}",
        ):
            queued += 1
    return queued


def quota_announcement(config: dict, work_date: dt.date, count_row: dict | None) -> str:
    workers, quota, has_poll = quota_for(count_row)
    date_label = work_date.strftime("%B %d, %Y")
    if not has_poll:
        return (
            f"📣 TOMORROW'S QUOTA — {date_label}\n\n"
            "No tracked availability poll was found. Please contact the manager before applying a quota."
        )
    return "\n".join([
        f"📣 TOMORROW'S QUOTA — {date_label}",
        f"🏢 {config['name']}",
        "",
        f"Active workers: {workers}",
        f"Client quota: ceil({workers} × 0.8) = {quota}",
        f"Done Deals target: {quota}",
        f"Close Deals target: {quota}",
        "",
        "Both DD and CD targets must be reached for 40% commission. Otherwise, commission is 35%.",
    ])


def queue_daily_closeout(report_date: dt.date, now: dt.datetime, allowed: set[int]) -> int:
    if report_date < AUTOMATION_START_DATE or DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    queued = 0
    report_counts = daily_poll_counts(allowed, report_date)
    tomorrow = report_date + dt.timedelta(days=1)
    tomorrow_counts = daily_poll_counts(allowed, tomorrow)
    for chat_id, config in GROUPS.items():
        if chat_id not in allowed:
            continue
        announcement = quota_announcement(config, tomorrow, tomorrow_counts.get(chat_id))
        if enqueue_scheduled_action(
            chat_id=chat_id,
            action_type="message",
            payload={
                "text": announcement,
                "disable_notification": False,
                "message_thread_id": config["announcements"],
            },
            scheduled_for=now,
            dedupe_key=f"daily-quota:{tomorrow.isoformat()}:{chat_id}",
        ):
            queued += 1
        report = build_group_report(chat_id, report_date, report_counts.get(chat_id))
        report_parts = split_message(report)
        for part_number, part in enumerate(report_parts, 1):
            if len(report_parts) > 1:
                part = f"{part}\n\nPart {part_number}"
            if enqueue_scheduled_action(
                chat_id=DAILY_REPORTS_CHAT_ID,
                action_type="message",
                payload={"text": part, "disable_notification": False},
                scheduled_for=now,
                dedupe_key=f"daily-report:{report_date.isoformat()}:{chat_id}:{part_number}",
            ):
                queued += 1
    return queued


def queue_historical_reports(work_dates: list[dt.date], now: dt.datetime, allowed: set[int]) -> int:
    if DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    queued = 0
    for work_date in work_dates:
        for chat_id, config in GROUPS.items():
            if chat_id not in allowed:
                continue
            workers = historical_active_count(chat_id, work_date)
            report = build_group_report(
                chat_id,
                work_date,
                {"active_workers": workers} if workers is not None else None,
                historical=True,
            )
            report_parts = split_message(report)
            for part_number, part in enumerate(report_parts, 1):
                if len(report_parts) > 1:
                    part = f"{part}\n\nPart {part_number}"
                if enqueue_scheduled_action(
                    chat_id=DAILY_REPORTS_CHAT_ID,
                    action_type="message",
                    payload={"text": part, "disable_notification": False},
                    scheduled_for=now,
                    dedupe_key=f"historical-report:{work_date.isoformat()}:{chat_id}:{part_number}",
                ):
                    queued += 1
    return queued


def run_due_daily_automation(now: dt.datetime, allowed: set[int]) -> int:
    local_now = now.astimezone(MANILA)
    queued = 0
    if local_now.hour == 0 and local_now.minute < 15:
        queued += queue_daily_polls(local_now.date() + dt.timedelta(days=1), now, allowed)
        queued += queue_daily_closeout(local_now.date() - dt.timedelta(days=1), now, allowed)
    elif local_now.hour == 23 and local_now.minute == 59:
        queued += queue_daily_closeout(local_now.date(), now, allowed)
    return queued

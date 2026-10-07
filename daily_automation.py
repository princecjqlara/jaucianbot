"""Daily Telegram availability polls, quotas, and sales reports."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from cloud_store import (
    daily_messages, daily_poll_active_users, daily_poll_counts,
    enqueue_scheduled_action, existing_daily_reminder_chats,
    existing_closeout_chats, record_automation_marker,
)
from deal_parser import (
    CLOSE_RE, DONE_MARKER_RE, PAGE_RE, PRICE_RE, author_identity, collect_deals,
    normalize_page, parse_close_count, parse_page, parse_price,
)


MANILA = ZoneInfo("Asia/Manila")
DAILY_REPORTS_CHAT_ID = -1004362432984
AUTOMATION_START_DATE = dt.date(2026, 9, 19)
REMINDER_HOURS = (7, 10, 13, 16, 19, 21)

GROUPS = {
    -1004423057797: {
        "name": "Veo Ollie",
        "announcements": 4794,
        "freebie": 4811,
        "contact_thread": 5758,
        "crm_pages": {"Vista Media Agency": "142b8647-c785-4205-a558-5e0cb26d7151", "Loki Media": "1a4fc336-1530-4064-a2a2-f79b2b005772"},
        "active": 4536,
        "done": 17,
        "close": 16,
        "pages": {
            "vista": "Vista", "vistamedia": "Vista", "vistamediaagency": "Vista",
            "loki": "Loki", "lokimedia": "Loki", "lokimediaagency": "Loki",
        },
    },
    -1003962888977: {
        "name": "Veo Jessa",
        "announcements": 5,
        "freebie": 3240,
        "contact_thread": 3725,
        "crm_pages": {"Manawari Studios": "b33a9b46-6470-48ed-bcfc-4235aa46f241"},
        "active": 3050,
        "done": 7,
        "close": 6,
        "pages": {
            "manawari": "Manawari Studios", "manawaristudio": "Manawari Studios",
            "manawaristudios": "Manawari Studios", "manawaridtudios": "Manawari Studios",
        },
    },
    -1003647732254: {
        "name": "Veo",
        "new_client_timeout_minutes": 30,
        "new_client_ready_rotation": True,
        "announcements": 248,
        "freebie": 26249,
        "contact_thread": 27622,
        "crm_pages": {"Azshinari": "d3f40d05-aa54-498e-bff7-e9c4410b7471", "SAMA KANA MEDIA": "838ba6f7-7d6d-4f5e-a7cd-11374960d0b7"},
        "active": 25893,
        "done": 1135,
        "close": 1132,
        "pages": {
            "ashinari": "Azshinari", "aszhinari": "Azshinari", "aszihinari": "Azshinari",
            "azhinari": "Azshinari", "azsginari": "Azshinari", "azsh": "Azshinari",
            "azshi": "Azshinari", "azshin": "Azshinari", "azshinari": "Azshinari",
            "samakana": "Sama Ka Na Media", "samakanaadverts": "Sama Ka Na Media",
            "samakanamedia": "Sama Ka Na Media", "sknm": "Sama Ka Na Media",
            "smkn": "Sama Ka Na Media",
        },
    },
    -1004461399292: {
        "name": "Veo Jel",
        "announcements": 2,
        "freebie": 3003,
        "contact_thread": 4180,
        "crm_pages": {"Onset Media Agency": "fee0cc66-ce01-4ad9-94ef-0c8948ce4f0b"},
        "active": 2917,
        "done": 6,
        "close": 8,
        "pages": {"onset": "Onset Media Agency", "onsetmediaagency": "Onset Media Agency"},
    },
}

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
    rows = daily_messages(
        chat_id,
        start_local.astimezone(dt.timezone.utc),
        end_local.astimezone(dt.timezone.utc),
    )
    return rows[:10000], len(rows) > 10000


def same_author(user: dict, row: dict) -> bool:
    user_id, author_id = user.get("user_id"), row.get("author_id")
    if user_id is not None and author_id is not None:
        return int(user_id) == int(author_id)
    name = " ".join((user.get("user_name") or "").split()).casefold()
    author = " ".join((row.get("author_name") or "").split()).casefold()
    return bool(name and author and name == author)


def build_group_report(
    chat_id: int,
    work_date: dt.date,
    count_row: dict | None,
    *,
    historical: bool = False,
    active_users: list[dict] | None = None,
) -> str:
    config = GROUPS[chat_id]
    rows, capped = messages_for_day(chat_id, work_date)
    page_stats = defaultdict(lambda: {"cd": 0, "dd": 0, "gross": Decimal("0")})
    employee_stats = defaultdict(lambda: {"dd": 0, "gross": Decimal("0")})
    parsed = collect_deals(rows, config, historical=historical)
    deal_rows = [entry["row"] for entry in parsed["entries"] if entry["count"] > 0]
    uncertain_rows = parsed["uncertain_rows"]
    skipped_done = sum(bool(row.get("thread_id") == config["done"] or (
        historical and row.get("thread_id") is None and DONE_MARKER_RE.search(row.get("text") or "")
    )) for row in uncertain_rows)
    skipped_close = len(uncertain_rows) - skipped_done
    for entry in parsed["entries"]:
        row, page = entry["row"], entry["page"]
        if entry["kind"] == "dd":
            page_stats[page]["dd"] += entry["count"]
            page_stats[page]["gross"] += entry["gross"]
            key = author_identity(row)
            employee_stats[key].update(name=" ".join((row.get("author_name") or "Unknown employee").split()))
            employee_stats[key]["dd"] += entry["count"]
            employee_stats[key]["gross"] += entry["gross"]
        else:
            page_stats[page]["cd"] += entry["count"]
    if historical:
        for row in rows:
            if row.get("thread_id") is not None or PRICE_RE.search(row.get("text") or ""):
                continue
            text = row.get("text") or ""
            # Exported close summaries can duplicate individual CD entries.
            # They establish that an employee had a CD without changing totals.
            summary = CLOSE_RE.search(text)
            if summary and (parse_close_count(text) or 0) > 0:
                deal_rows.append(row)
            elif (PAGE_RE.search(text) or PRICE_RE.search(text) or DONE_MARKER_RE.search(text)) and text.strip():
                uncertain_rows.append(row)

    for canonical in dict.fromkeys(config["pages"].values()):
        page_stats[canonical]

    cd_total = sum(values["cd"] for values in page_stats.values())
    dd_total = sum(values["dd"] for values in page_stats.values())
    gross = sum((values["gross"] for values in page_stats.values()), Decimal("0"))
    workers, quota, has_poll = quota_for(count_row)
    qualified = has_poll and workers > 0 and cd_total >= quota and dd_total >= quota
    # A review should block payroll only when it could actually change the
    # commission tier. Each unreadable Done Deals post can add at most one DD.
    # An unreadable Close Deals post may be a summary, so its possible CD count
    # is deliberately left unbounded. A capped day is likewise indeterminate.
    dd_could_reach_quota = dd_total + skipped_done >= quota
    cd_could_reach_quota = cd_total >= quota or skipped_close > 0
    commission_needs_review = (
        has_poll
        and workers > 0
        and not qualified
        and (capped or (dd_could_reach_quota and cd_could_reach_quota))
    )
    rate = None if commission_needs_review or not has_poll else (
        Decimal("0.40") if qualified else Decimal("0.35")
    )
    salaries = {
        name: (values["gross"] * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for name, values in employee_stats.items()
    } if rate is not None else {}
    salary_total = sum(salaries.values(), Decimal("0")) if rate is not None else None
    profit = gross - salary_total if salary_total is not None else None

    date_label = work_date.strftime("%A, %B %d, %Y")
    dd_status = "✅" if has_poll and workers > 0 and dd_total >= quota else ("❌" if has_poll and workers > 0 else "—")
    cd_status = "✅" if has_poll and workers > 0 and cd_total >= quota else ("❌" if has_poll and workers > 0 else "—")
    lines = [
        f"📊 {'HISTORICAL SAMPLE' if historical else 'DAILY REPORT'}",
        f"📅 {date_label}",
        f"🏢 {config['name'].upper()}",
        "",
        "🎯 QUOTA & RESULT",
    ]
    if has_poll and workers > 0:
        lines.extend([
            f"Active workers: {workers}",
            f"Team target (80%, rounded up): {quota}",
            f"Target: {quota} DD • {quota} CD",
        ])
    elif has_poll:
        lines.extend([
            "Active workers: none recorded",
            "Target: not set because no one marked themselves active.",
        ])
    else:
        lines.extend(["Active workers: no tracked poll", "Target: unavailable until the poll is confirmed."])
    lines.extend([
        f"Actual: {dd_total} DD {dd_status} • {cd_total} CD {cd_status}",
        "Commission: needs a quick data review" if rate is None else f"Commission: {int(rate * 100)}%",
        "",
        "📄 PAGE TOTALS",
    ])
    if skipped_done or skipped_close or capped:
        lines.append("Totals and sales include verified posts only; unresolved entries may change the result.")
    for page, values in sorted(page_stats.items()):
        lines.extend([
            page.upper(),
            f"  {values['dd']} DD • {values['cd']} CD",
            f"  Sales: {money(values['gross'])}",
        ])
    lines.extend(["", "👥 EMPLOYEE RANKING"])
    ranking = sorted(employee_stats.items(), key=lambda item: (-item[1]["gross"], item[1]["name"].casefold()))
    if ranking:
        for index, (key, values) in enumerate(ranking, 1):
            lines.extend([
                f"{index}. {values['name']}",
                f"   {values['dd']} DD • {money(values['gross'])} sales",
                "   Pay: pending data review" if rate is None else f"   Pay: {money(salaries[key])}",
            ])
    else:
        lines.append("No readable Done Deals were recorded.")
    lines.extend(["", "ACTIVE TEAM MEMBERS WITH NO RECORDED CD OR DD"])
    if active_users is None and count_row and count_row.get("poll_id"):
        active_users = daily_poll_active_users(count_row["poll_id"])
    if active_users is None:
        lines.append("Names aren't available because no individual Active responses were recorded for this date.")
    elif capped:
        lines.append("Names aren't listed because the day exceeded the safe message-review limit.")
    elif count_row and len(active_users) != int(count_row["active_workers"]):
        lines.append("Names aren't listed because the Active responses changed while this report was being prepared.")
    else:
        no_deals = []
        uncertain = []
        for user in active_users:
            if any(same_author(user, row) for row in deal_rows):
                continue
            name = user.get("user_name") or str(user.get("user_id") or "Unknown employee")
            if any(same_author(user, row) for row in uncertain_rows):
                uncertain.append(name)
            else:
                no_deals.append(name)
        lines.extend(f"• {name}" for name in sorted(no_deals, key=str.casefold))
        if not no_deals and not uncertain:
            lines.append("Everyone has at least one recorded CD or DD.")
        if uncertain:
            lines.append("Please review a possible deal post for: " + ", ".join(sorted(uncertain, key=str.casefold)))
    lines.extend([
        "",
        "💰 FINANCIALS",
        f"Gross: {money(gross)}",
        "Commissions: pending data review" if salary_total is None else f"Commissions: −{money(salary_total)}",
        "Net profit: pending data review" if profit is None else f"Net profit: {money(profit)}",
    ])
    if not historical:
        from freebie_automation import freebie_report_lines
        from new_client_automation import new_client_report_lines
        try:
            lines.extend(freebie_report_lines(chat_id, work_date))
        except Exception:
            lines.extend(["", "🎁 CONFIRMED FREEBIES SENT", "Freebie assignment data couldn't be loaded for this report."])
        try:
            lines.extend(new_client_report_lines(chat_id, work_date))
        except Exception:
            lines.extend(["", "👤 NEW CLIENTS ACKNOWLEDGED", "New-client assignment data couldn't be loaded for this report."])
    if skipped_done or skipped_close or capped or parsed["duplicate_rows"]:
        lines.extend(["", "DATA CHECK"])
        if skipped_done:
            lines.append(f"• Please review {skipped_done} possible Done Deals post(s); the page or Price Deal wasn't readable.")
        if skipped_close:
            lines.append(f"• Please review {skipped_close} possible Close Deals post(s); the page or count wasn't readable.")
        if parsed["ownership_conflicts"]:
            lines.append("• The same client and amount were posted by different employees; confirm ownership before adding these deals or pay.")
        for row in uncertain_rows:
            lines.append(f"• {config['name']} | UTC {row.get('sent_utc', 'unavailable')} | message #{row.get('message_id', 'unavailable')}")
        if parsed["duplicate_rows"]:
            lines.append(f"• Excluded {len(parsed['duplicate_rows'])} repeated deal post(s) from totals.")
        if capped:
            lines.append("• This group passed the 10,000-message review limit, so please check it manually for any overflow.")
    if historical:
        lines.extend([
            "",
            "SOURCE NOTE",
            "Reconstructed from the Telegram Desktop export. Exported messages have no topic IDs, so paid/Total Payment entries count as DD and unpaid Price Deal entries count as CD.",
        ])
    return "\n".join(lines)


def historical_active_users(chat_id: int, work_date: dt.date) -> list[dict] | None:
    previous_date = work_date - dt.timedelta(days=1)
    rows, capped = messages_for_day(chat_id, previous_date)
    if capped:
        return None
    excluded = re.compile(r"(?i)inactive|active\s+tomorrow|how\s+many|answer\s+the\s+poll|\bguys\b|\bilan\b|\bsino\b")
    workers = {}
    for row in rows:
        text = row.get("text") or ""
        author = (row.get("author_name") or "").strip()
        if (author and re.search(r"(?i)\bactive\b", text) and "?" not in text
                and not re.search(r"(?i)\b(?:not|no[t']?|won't)\s+(?:be\s+)?active\b", text)
                and not excluded.search(text)):
            workers[author.casefold()] = {"user_id": row.get("author_id"), "user_name": author}
    return list(workers.values()) if workers else None


def historical_active_count(chat_id: int, work_date: dt.date) -> int | None:
    users = historical_active_users(chat_id, work_date)
    return len(users) if users is not None else None


def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit and current:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def queue_daily_polls(
    work_date: dt.date,
    now: dt.datetime,
    allowed: set[int],
    *,
    existing_chats: set[int] | None = None,
) -> int:
    queued = 0
    existing_chats = existing_chats or set()
    for chat_id, config in GROUPS.items():
        if chat_id not in allowed or chat_id in existing_chats:
            continue
        day_label = "TODAY" if work_date == now.astimezone(MANILA).date() else "TOMORROW"
        payload = {
            "question": (
                f"Hi team! Will you be active {day_label.lower()}?\n"
                f"{work_date.strftime('%A, %B %d, %Y')} (PHT)"
            ),
            "options": ["✅ Yes, I'll be active", "❌ No, I won't be active"],
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


def quota_announcement(config: dict, work_date: dt.date, count_row: dict | None, *, today: dt.date | None = None) -> str:
    workers, quota, has_poll = quota_for(count_row)
    date_label = work_date.strftime("%A, %B %d, %Y")
    heading = "TODAY'S QUOTA" if today == work_date else "TOMORROW'S QUOTA"
    if not has_poll:
        return (
            f"📣 {heading}\n📅 {date_label} (PHT)\n\n"
            "We couldn't find the availability poll yet, so no quota has been set. "
            "Please check with your manager before using a target for this date."
        )
    if workers == 0:
        return "\n".join([
            f"📣 {heading}",
            f"📅 {date_label} (PHT)",
            f"🏢 {config['name'].upper()}",
            "",
            "No one has marked themselves active yet, so there isn't a team quota for this date.",
            "Once the Active poll has responses, we'll calculate the target automatically.",
        ])
    return "\n".join([
        f"📣 {heading}",
        f"📅 {date_label} (PHT)",
        f"🏢 {config['name'].upper()}",
        "",
        f"We have {workers} active team member{'s' if workers != 1 else ''}.",
        f"Today's target: {quota} DD and {quota} CD (80%, rounded up).",
        "",
        "Let's work toward both targets together. Reaching both earns the 40% commission rate; otherwise, the rate is 35%.",
    ])


def deal_totals_for_day(chat_id: int, work_date: dt.date) -> tuple[int, int, bool, bool]:
    """Count live DD/CD posts with the same topic and summary rules as reports."""
    progress = deal_progress_for_day(chat_id, work_date)
    return (
        progress["dd_total"], progress["cd_total"],
        progress["capped"], progress["uncertain"],
    )


def deal_progress_for_day(chat_id: int, work_date: dt.date) -> dict:
    """Return team totals plus per-worker activity for an in-day reminder."""
    config = GROUPS[chat_id]
    rows, capped = messages_for_day(chat_id, work_date)
    dd_total = cd_total = 0
    deal_rows: list[dict] = []
    uncertain_rows: list[dict] = []
    worker_stats: dict[tuple[str, object], dict] = {}
    parsed = collect_deals(rows, config)
    uncertain_rows = parsed["uncertain_rows"]
    for entry in parsed["entries"]:
        if entry["count"] <= 0:
            continue
        row = entry["row"]
        key = author_identity(row)
        stats = worker_stats.setdefault(key, {
            "user_id": row.get("author_id"),
            "name": " ".join((row.get("author_name") or "Unknown employee").split()),
            "dd": 0, "cd": 0, "gross": Decimal("0"),
        })
        stats[entry["kind"]] += entry["count"]
        stats["gross"] += entry["gross"]
        dd_total += entry["count"] if entry["kind"] == "dd" else 0
        cd_total += entry["count"] if entry["kind"] == "cd" else 0
        deal_rows.append(row)
    return {
        "dd_total": dd_total,
        "cd_total": cd_total,
        "capped": capped,
        "uncertain": bool(uncertain_rows),
        "deal_rows": deal_rows,
        "uncertain_rows": uncertain_rows,
        "workers": list(worker_stats.values()),
    }


def quota_reminder(
    config: dict, work_date: dt.date, hour: int,
    count_row: dict | None, dd_total: int, cd_total: int, capped: bool, uncertain: bool,
    *, progress: dict | None = None, active_users: list[dict] | None = None,
) -> str:
    workers, quota, has_poll = quota_for(count_row)
    encouragement = {
        7: "Good morning, team! Let's start strong—follow up with interested clients and remember to record each deal in the right topic.",
        10: "Nice work so far. Keep the conversations moving, and record each new CD and DD as it comes in.",
        13: "We're making progress! A few thoughtful follow-ups can go a long way—please keep every CD and DD updated.",
        16: "Afternoon check-in: revisit your open conversations and let's keep moving toward both targets together.",
        19: "Good evening, team. Follow up with your warm leads and keep the deal topics up to date—we're nearly there!",
        21: "Final check-in for today. Let's give the remaining conversations our best and record every deal before closeout.",
    }[hour]
    lines = [
        f"📣 DAILY QUOTA CHECK-IN — {hour % 12 or 12}:00 {'AM' if hour < 12 else 'PM'} PHT",
        f"📅 {work_date.strftime('%A, %B %d, %Y')}",
        f"🏢 {config['name'].upper()}",
        "",
    ]
    if not has_poll or workers == 0:
        lines.append(
            "Quota: waiting for someone to mark themselves active."
            if has_poll else "Quota: today's Active poll hasn't been recorded yet."
        )
    else:
        lines.append(f"Active workers: {workers} • Target: {quota} DD and {quota} CD")
    if capped:
        lines.append("Progress needs a manual check because there were more messages than the report can safely review.")
    else:
        lines.append(f"So far: {dd_total} DD • {cd_total} CD" + (" (readable posts only)" if uncertain else ""))
        if uncertain:
            lines.append("A few possible deal posts need a quick review, so the exact remaining gap isn't shown yet.")
        elif has_poll and workers > 0:
            remaining_dd = max(quota - dd_total, 0)
            remaining_cd = max(quota - cd_total, 0)
            lines.append(f"Still needed: {remaining_dd} DD • {remaining_cd} CD")
            if remaining_dd == 0 and remaining_cd == 0:
                encouragement = "Both targets are met—wonderful work, team! Please keep recording any new deals until closeout."

    if progress and not capped:
        activity_workers = sorted(
            progress.get("workers") or [],
            key=lambda item: (-item["gross"], -item["dd"], -item["cd"], item["name"].casefold()),
        )
        if activity_workers and hour >= 13:
            leader = activity_workers[0]
            if leader["gross"] > 0:
                lines.append(
                    f"Current sales leader: {leader['name']} — {leader['dd']} DD, {money(leader['gross'])} sales."
                )
            elif leader["cd"] > 0:
                lines.append(f"Current activity leader: {leader['name']} — {leader['cd']} CD.")

        if (active_users is not None and hour >= 10
                and len(active_users) == workers):
            no_activity: list[str] = []
            awaiting_review: list[str] = []
            for user in active_users:
                if any(same_author(user, row) for row in progress.get("deal_rows") or []):
                    continue
                name = user.get("user_name") or str(user.get("user_id") or "Unknown employee")
                if any(same_author(user, row) for row in progress.get("uncertain_rows") or []):
                    awaiting_review.append(name)
                else:
                    no_activity.append(name)
            if no_activity:
                lines.append(
                    "Active members with no recorded CD or DD yet: "
                    + ", ".join(sorted(no_activity, key=str.casefold))
                    + "."
                )
            if awaiting_review:
                lines.append(
                    "Possible activity awaiting a readable post: "
                    + ", ".join(sorted(awaiting_review, key=str.casefold))
                    + "."
                )

    if has_poll and workers > 0 and not capped and not uncertain:
        remaining_dd = max(quota - dd_total, 0)
        remaining_cd = max(quota - cd_total, 0)
        if remaining_cd and remaining_dd:
            lines.append("Next focus: advance client conversations, then follow through to payment and record both CD and DD.")
        elif remaining_cd:
            lines.append("Next focus: create more qualified client conversations and record each new CD.")
        elif remaining_dd:
            lines.append("Next focus: follow up open CDs for payment and record each completed DD.")
    lines.extend(["", encouragement])
    if has_poll and workers > 0:
        lines.append("Reaching both targets earns the 40% commission rate.")
    return "\n".join(lines)


def queue_daily_reminders(work_date: dt.date, hour: int, now: dt.datetime, allowed: set[int]) -> int:
    if work_date < AUTOMATION_START_DATE or hour not in REMINDER_HOURS:
        return 0
    eligible = allowed.intersection(GROUPS)
    missing = eligible - existing_daily_reminder_chats(work_date, hour, eligible)
    if not missing:
        return 0
    counts = daily_poll_counts(missing, work_date)
    queued = 0
    for chat_id, config in GROUPS.items():
        if chat_id not in missing:
            continue
        count_row = counts.get(chat_id)
        progress = deal_progress_for_day(chat_id, work_date)
        active_users = None
        if count_row and count_row.get("poll_id"):
            active_users = daily_poll_active_users(count_row["poll_id"])
        reminder = quota_reminder(
            config, work_date, hour, count_row,
            progress["dd_total"], progress["cd_total"], progress["capped"], progress["uncertain"],
            progress=progress, active_users=active_users,
        )
        if enqueue_scheduled_action(
            chat_id=chat_id,
            action_type="message",
            payload={
                "text": reminder,
                "disable_notification": False,
                "message_thread_id": config["announcements"],
            },
            scheduled_for=now,
            dedupe_key=f"daily-reminder:{work_date.isoformat()}:{hour}:{chat_id}",
        ):
            queued += 1
    return queued


def queue_daily_closeout(report_date: dt.date, now: dt.datetime, allowed: set[int]) -> int:
    if report_date < AUTOMATION_START_DATE or DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    queued = 0
    eligible = allowed.intersection(GROUPS)
    missing = eligible - existing_closeout_chats(report_date, eligible)
    if not missing:
        return 0
    report_counts = daily_poll_counts(missing, report_date)
    tomorrow = report_date + dt.timedelta(days=1)
    tomorrow_counts = daily_poll_counts(missing, tomorrow)
    for chat_id, config in GROUPS.items():
        if chat_id not in missing:
            continue
        announcement = quota_announcement(config, tomorrow, tomorrow_counts.get(chat_id), today=now.astimezone(MANILA).date())
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
        record_automation_marker(chat_id, f"daily-closeout-complete:{report_date.isoformat()}:{chat_id}", now)
    return queued


def queue_historical_reports(
    work_dates: list[dt.date],
    now: dt.datetime,
    allowed: set[int],
    *,
    version: str = "v1",
) -> int:
    if DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    queued = 0
    for work_date in work_dates:
        for chat_id, config in GROUPS.items():
            if chat_id not in allowed:
                continue
            active_users = historical_active_users(chat_id, work_date)
            report = build_group_report(
                chat_id,
                work_date,
                {"active_workers": len(active_users)} if active_users is not None else None,
                historical=True,
                active_users=active_users,
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
                    dedupe_key=f"historical-report:{version}:{work_date.isoformat()}:{chat_id}:{part_number}",
                ):
                    queued += 1
    return queued


def run_due_daily_automation(now: dt.datetime, allowed: set[int]) -> int:
    local_now = now.astimezone(MANILA)
    queued = 0
    today = local_now.date()
    today_registered = set(daily_poll_counts(allowed, today))
    if today >= AUTOMATION_START_DATE and any(chat_id in allowed and chat_id not in today_registered for chat_id in GROUPS):
        queued += queue_daily_polls(today, now, allowed, existing_chats=today_registered)
    tomorrow = local_now.date() + dt.timedelta(days=1)
    registered = set(daily_poll_counts(allowed, tomorrow))
    if any(chat_id in allowed and chat_id not in registered for chat_id in GROUPS):
        queued += queue_daily_polls(tomorrow, now, allowed, existing_chats=registered)
    if local_now.hour in REMINDER_HOURS:
        queued += queue_daily_reminders(local_now.date(), local_now.hour, now, allowed)
    if local_now.hour == 0:
        queued += queue_daily_closeout(local_now.date() - dt.timedelta(days=1), now, allowed)
    elif local_now.hour == 23 and local_now.minute == 59:
        queued += queue_daily_closeout(local_now.date(), now, allowed)
    return queued

"""One nightly report batch, beginning at 23:59 Philippine time."""
from __future__ import annotations

import datetime as dt

from daily_automation import DAILY_REPORTS_CHAT_ID, MANILA

POLICY = "nightly-2359-pht-v1"
RECOVERY_MINUTES = 60


def report_date(now: dt.datetime) -> dt.date | None:
    local = now.astimezone(MANILA)
    if local.hour == 23 and local.minute == 59:
        return local.date()
    # Finish a delayed/Telegram-throttled batch across midnight, once only.
    if local.hour == 0 and local.minute < RECOVERY_MINUTES - 1:
        return local.date() - dt.timedelta(days=1)
    return None


def payload(day: dt.date) -> dict:
    return {"nightly_report_policy": POLICY, "nightly_publication_date": day.isoformat()}


def prepare_delivery(action: dict, now: dt.datetime) -> bool:
    p = action.get("payload") or {}
    key = action.get("dedupe_key") or ""
    public = int(action["chat_id"]) == DAILY_REPORTS_CHAT_ID and (
        any(p.get(field) for field in ("organized_report_date", "organized_staffing_date", "organized_summary_date"))
        or key.startswith(("organized-daily:", "organized-staffing:")))
    private = bool(p.get("makata_private_report") and not p.get("makata_report_requested"))
    if not public and not private and not p.get("nightly_report_policy"):
        return True
    action["repeat_interval_minutes"] = None
    day = report_date(now)
    return bool(day and p.get("nightly_report_policy") == POLICY
        and p.get("nightly_publication_date") == day.isoformat())


def changes(current: dict, previous: dict | None) -> dict[tuple[str, int], dict]:
    """Compare personal estimates; a peer default cannot prove improvement."""
    old = {(business, int(m["user_id"])): m.get("planning") or {}
        for business, members in (previous or {}).get("businesses", {}).items()
        for m in members if m.get("user_id") is not None}
    result = {}
    for business, members in current.get("businesses", {}).items():
        for member in members:
            uid = int(member["user_id"])
            plan, before = member.get("planning") or {}, old.get((business, uid), {})
            value = plan.get("estimated_capacity_per_day")
            prior = before.get("estimated_capacity_per_day")
            if value is None:
                text = "Provisional; personal history still below 3 output dates."
            elif not previous:
                text = f"Nightly baseline: {value:.2f}/day; comparison starts with the next report."
            elif prior is None:
                text = f"Personal estimate now available: {value:.2f}/day."
            elif abs(value - prior) < 1e-10:
                text = f"Unchanged: {value:.2f}/day."
            else:
                text = f"{'Increased' if value > prior else 'Decreased'}: {prior:.2f} → {value:.2f}/day."
            result[(business, uid)] = {"text": text}
    return result

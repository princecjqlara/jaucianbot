"""Manager-only worker activity analytics from polls and recorded deal topics."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal

from cloud_store import activity_messages, freebie_actions, new_client_actions, poll_answers_for_range
from daily_automation import GROUPS, MANILA
from deal_parser import collect_deals
from freebie_automation import is_freebie_action
from new_client_automation import is_new_client_action


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _identity(user_id, name: str | None) -> tuple[str, object]:
    if user_id is not None:
        return "id", int(user_id)
    return "name", " ".join((name or "Unknown employee").split()).casefold()


def _poll_date(row: dict) -> dt.date | None:
    poll = row.get("daily_polls") or {}
    if isinstance(poll, list):
        poll = poll[0] if poll else {}
    try:
        return dt.date.fromisoformat(poll["work_date"])
    except (KeyError, TypeError, ValueError):
        return None


def _recommendations(worker: dict) -> list[str]:
    suggestions: list[str] = []
    if worker["active_days"] == 0:
        suggestions.append("Not enough tracked Active-poll history for a consistency assessment.")
    elif worker["active_days_without_deals"]:
        suggestions.append(
            f"On {worker['active_days_without_deals']} tracked Active day(s), no readable CD or DD was recorded; "
            "record activity as it happens or ask a manager to correct missing data."
        )
    if worker["cd"] > worker["dd"]:
        suggestions.append("Prioritize follow-up on open CDs and record the DD promptly when payment is completed.")
    elif worker["dd"] and worker["cd"] == 0:
        suggestions.append("Record CDs consistently so the pipeline leading to paid DDs is visible.")
    if worker["unreadable_posts"]:
        suggestions.append(
            f"Use the standard Page and Price Deal fields; {worker['unreadable_posts']} possible post(s) were unreadable."
        )
    if worker["best_hour_pht"] is not None:
        suggestions.append(
            f"Their strongest recorded deal-posting hour is around {worker['best_hour_pht']:02d}:00 PHT; "
            "schedule focused follow-up time shortly before it."
        )
    if not suggestions:
        suggestions.append("Keep the current routine and continue recording every CD, DD, freebie, and new-client confirmation.")
    return suggestions


def worker_activity_report(chat_id: int, days: int, now: dt.datetime) -> dict:
    """Build transparent activity metrics; this is coaching data, not payroll data."""
    config = GROUPS[chat_id]
    local_today = now.astimezone(MANILA).date()
    start_date = local_today - dt.timedelta(days=days - 1)
    start_local = dt.datetime.combine(start_date, dt.time.min, tzinfo=MANILA)
    end_local = dt.datetime.combine(local_today + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA)
    rows = activity_messages(
        chat_id,
        start_local.astimezone(dt.timezone.utc),
        end_local.astimezone(dt.timezone.utc),
        {config["done"], config["close"]},
    )
    answers = poll_answers_for_range(chat_id, start_date, local_today)
    people: dict[tuple[str, object], dict] = {}

    def person(user_id, name: str | None) -> dict:
        key = _identity(user_id, name)
        value = people.setdefault(key, {
            "user_id": int(user_id) if user_id is not None else None,
            "name": " ".join((name or "Unknown employee").split()),
            "active_dates": set(),
            "inactive_dates": set(),
            "deal_dates": set(),
            "uncertain_dates": set(),
            "dd": 0,
            "cd": 0,
            "gross": Decimal("0"),
            "freebies": 0,
            "new_clients": 0,
            "unreadable_posts": 0,
            "hours": Counter(),
            "first_activity_utc": None,
            "last_activity_utc": None,
        })
        if name:
            value["name"] = " ".join(name.split())
        return value

    for row in answers:
        work_date = _poll_date(row)
        if work_date is None:
            continue
        target = person(row.get("user_id"), row.get("user_name"))
        (target["active_dates"] if row.get("active") else target["inactive_dates"]).add(work_date)

    parsed = collect_deals(rows, config)
    for entry in parsed["entries"]:
        if entry["count"] <= 0:
            continue
        row = entry["row"]
        target = person(row.get("author_id"), row.get("author_name"))
        sent = _timestamp(row["sent_utc"])
        local = sent.astimezone(MANILA)
        target[entry["kind"]] += entry["count"]
        target["gross"] += entry["gross"]
        target["deal_dates"].add(local.date())
        target["hours"][local.hour] += 1
        target["first_activity_utc"] = min(filter(None, (target["first_activity_utc"], sent)), default=sent)
        target["last_activity_utc"] = max(filter(None, (target["last_activity_utc"], sent)), default=sent)
    for row in parsed["uncertain_rows"]:
        target = person(row.get("author_id"), row.get("author_name"))
        target["unreadable_posts"] += 1
        target["uncertain_dates"].add(_timestamp(row["sent_utc"]).astimezone(MANILA).date())

    for action in freebie_actions({chat_id}):
        if not is_freebie_action(action):
            continue
        payload = action.get("payload") or {}
        completed_at = payload.get("freebie_completed_at")
        if not completed_at:
            continue
        completed = _timestamp(completed_at)
        if start_local <= completed.astimezone(MANILA) < end_local:
            person(payload.get("freebie_assignee_id"), payload.get("freebie_assignee_name"))["freebies"] += 1

    for action in new_client_actions({chat_id}):
        if not is_new_client_action(action):
            continue
        payload = action.get("payload") or {}
        acknowledged_at = payload.get("new_client_acknowledged_at")
        if not acknowledged_at:
            continue
        acknowledged = _timestamp(acknowledged_at)
        if start_local <= acknowledged.astimezone(MANILA) < end_local:
            person(
                payload.get("new_client_assignee_id"), payload.get("new_client_assignee_name"),
            )["new_clients"] += 1

    workers = []
    for value in people.values():
        active_dates = value.pop("active_dates")
        active_days = len(active_dates)
        inactive_days = len(value.pop("inactive_dates"))
        deal_dates = value.pop("deal_dates")
        hours = value.pop("hours")
        deal_active_days = len(deal_dates.intersection(active_dates)) if active_dates else len(deal_dates)
        uncertain_dates = value.pop("uncertain_dates")
        no_deal_days = len(active_dates - deal_dates - uncertain_dates)
        value.update({
            "active_days": active_days,
            "inactive_days": inactive_days,
            "deal_days": deal_active_days,
            "active_days_without_deals": no_deal_days,
            "active_days_awaiting_review": len((active_dates - deal_dates).intersection(uncertain_dates)),
            "consistency_percent": round(100 * min(deal_active_days, active_days) / active_days, 1) if active_days else None,
            "best_hour_pht": min((hour for hour, count in hours.items() if count == max(hours.values())), default=None),
            "activity_score": value["dd"] * 3 + value["cd"] * 2,
        })
        value["recommendations"] = _recommendations(value)
        workers.append(value)

    workers.sort(key=lambda item: (-item["activity_score"], -item["gross"], item["name"].casefold()))

    def leaders(metric: str, *, minimum=0) -> list[dict]:
        ranked = [item for item in workers if item[metric] is not None and item[metric] > minimum]
        ranked.sort(key=lambda item: (-item[metric], item["name"].casefold()))
        return [{"name": item["name"], "value": item[metric]} for item in ranked[:5]]

    return {
        "team": config["name"],
        "chat_id": chat_id,
        "start_date": start_date,
        "end_date": local_today,
        "score_formula": "Deal activity score = 3 points per DD + 2 points per CD; sales, consistency, freebies, and acknowledged new clients are ranked separately",
        "leaders": {
            "overall_activity": leaders("activity_score"),
            "sales": leaders("gross"),
            "done_deals": leaders("dd"),
            "close_deals": leaders("cd"),
            "confirmed_freebies": leaders("freebies"),
            "acknowledged_new_clients": leaders("new_clients"),
            "consistency": leaders("consistency_percent"),
        },
        "workers": workers,
        "coverage_note": (
            "Metrics use tracked poll responses, readable live deal-topic posts, FREEBIE SENT confirmations, "
            "and new-client WORKING confirmations. "
            "Best hour means deal-posting time, not login time. Missing or unreadable data is shown separately."
        ),
    }

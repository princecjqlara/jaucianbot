"""Read-only recorded output, declared working days, and observed daily peaks."""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from cloud_store import activity_messages, poll_answers_for_range, archive_status, capacity_export_messages
from daily_automation import GROUPS, MANILA
from deal_parser import collect_deals


PLANNING_WINDOW_DAYS = 30
MIN_PLANNING_ACTIVE_DAYS = 7
MIN_REACHABLE_DAYS = 3
PLANNING_BUFFER = 0.8
TYPICAL_ACTIVE_FRACTION = 0.5


def planning(member: dict, end: dt.date) -> dict:
    """Conservative planning policy, separate from the user's descriptive formulas.

    Availability is declared rather than verified work. These are estimates of
    recorded Done Deals, never guarantees, production limits, or assignment caps.
    """
    start = end - dt.timedelta(days=PLANNING_WINDOW_DAYS - 1)
    daily = [d for d in member.get("daily", []) if start.isoformat() <= d["date"] <= end.isoformat()]
    active = [d["done_deals"] for d in daily if d["attendance"] == "Active"]
    output = [d["done_deals"] for d in daily if d["done_deals"] > 0]
    # At least this much output was recorded on three distinct dates, so a
    # single unusually high day cannot become the reachable target.
    reachable = sorted(output, reverse=True)[MIN_REACHABLE_DAYS - 1] if len(output) >= MIN_REACHABLE_DAYS else None
    safe = (min(sum(active) // len(active), reachable)
            if len(active) >= MIN_PLANNING_ACTIVE_DAYS and reachable is not None else None)
    return {"window_start": start.isoformat(), "window_end": end.isoformat(),
            "known_active_days": len(active), "output_days": len(output),
            "active_day_average": round(sum(active) / len(active), 2) if active else None,
            "output_day_average": round(sum(output) / len(output), 2) if output else None,
            "safe_plan_per_day": safe, "reachable_target_per_day": reachable,
            "reachable_support_days": sum(n >= reachable for n in output) if reachable is not None else 0,
            "minimum_active_days": MIN_PLANNING_ACTIVE_DAYS, "minimum_repeat_days": MIN_REACHABLE_DAYS,
            "estimates_only": True}


def reviewed_planning(member: dict, end: dt.date) -> dict:
    """VEO averages keep fractional output; typical targets include zero days.

    The 20% buffer is a planning choice, not a confidence bound or guarantee.
    A target needs at least half of declared Active dates and three successes.
    """
    import report_topics
    if report_topics.enabled():
        import veo_forecast
        return veo_forecast.planning(member, end)
    base = planning(member, end)
    active = [d["done_deals"] for d in member.get("daily", [])
              if base["window_start"] <= d["date"] <= base["window_end"] and d["attendance"] == "Active"]
    enough = len(active) >= MIN_PLANNING_ACTIVE_DAYS and base["output_days"] >= MIN_REACHABLE_DAYS
    mean = sum(active) / len(active) if active else None
    required = max(MIN_REACHABLE_DAYS, math.ceil(len(active) * TYPICAL_ACTIVE_FRACTION))
    target = sorted(active, reverse=True)[required - 1] if enough else None
    support = sum(n >= target for n in active) if target is not None and target > 0 else None
    return {**base, "policy": "veo-reviewed-v1", "observed_stretch_per_day": base["reachable_target_per_day"],
            "planning_average_per_day": mean if enough else None,
            "safe_plan_per_day": PLANNING_BUFFER * mean if enough else None,
            "reachable_target_per_day": target, "reachable_support_days": support,
            "target_active_days": len(active), "positive_active_days": sum(n > 0 for n in active),
            "target_achievement_percent": round(100 * support / len(active), 1) if support is not None else None,
            "buffer_fraction": PLANNING_BUFFER, "minimum_target_active_fraction": TYPICAL_ACTIVE_FRACTION,
            "history_quality": "limited" if len(active) < 14 else "more_history"}


def backtest(members: list[dict], end: dt.date) -> dict:
    """Walk forward: every prediction uses only earlier dates, on the same cohort.

    Correlated staff/day observations are descriptive checks, not independent
    proof of forecast accuracy or capacity. Latest 90 dates limit stale results.
    """
    sums = defaultdict(lambda: {"count": 0, "predicted": 0.0, "actual": 0.0, "absolute_error": 0.0, "hits": 0})
    cutoff = (end - dt.timedelta(days=89)).isoformat()
    for m in members:
        daily = sorted(m.get("daily", []), key=lambda d: d["date"])
        for i, day in enumerate(daily):
            if day["attendance"] != "Active" or not cutoff <= day["date"] <= end.isoformat():
                continue
            date = dt.date.fromisoformat(day["date"])
            old = planning({"daily": daily[:i]}, date - dt.timedelta(days=1))
            if old["safe_plan_per_day"] is None:
                continue
            recent = reviewed_planning({"daily": daily[:i]}, date - dt.timedelta(days=1))
            predictions = {"old_rounded_plan": old["safe_plan_per_day"], "old_three_day_target": old["reachable_target_per_day"],
                           "active_day_mean": recent["planning_average_per_day"], "buffered_mean": recent["safe_plan_per_day"]}
            for label, predicted in predictions.items():
                if predicted is None:
                    continue
                s = sums[label]
                s["count"] += 1
                s["predicted"] += predicted
                s["actual"] += day["done_deals"]
                s["absolute_error"] += abs(predicted - day["done_deals"])
                s["hits"] += int(day["done_deals"] >= predicted)
    return {"window_start": cutoff, "window_end": end.isoformat(), "read_only": True,
        "methods": {label: {"observations": s["count"], "mean_prediction": round(s["predicted"] / s["count"], 3),
            "mean_recorded_output": round(s["actual"] / s["count"], 3),
            "mean_absolute_error": round(s["absolute_error"] / s["count"], 3),
            "target_met_percent": round(100 * s["hits"] / s["count"], 1)} for label, s in sums.items()},
        "limitations": "Past recorded output only. Active votes do not confirm hours worked or available workload. Repeated staff/dates are correlated; the buffer is a planning policy, not independently validated safety or a production guarantee."}


def _date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def summarize(chat_id: int, rows: list[dict], answers: list[dict], start: dt.date, end: dt.date, *, historical: bool = False) -> dict:
    config = GROUPS[chat_id]
    parsed = collect_deals(rows, config, historical=historical)
    entries = [e for e in parsed["entries"] if e["kind"] == "dd" and e["count"] > 0]
    names = defaultdict(set)
    for row in rows + answers:
        uid = row.get("author_id", row.get("user_id"))
        name = " ".join((row.get("author_name") or row.get("user_name") or "").split())
        if uid is not None and name:
            names[name.casefold()].add(int(uid))
    people, evidence = {}, []

    def person(uid, name):
        name = " ".join((name or "Unknown member").split())
        if uid is None and len(names[name.casefold()]) == 1:
            uid = next(iter(names[name.casefold()]))
        key = ("id", int(uid)) if uid is not None else ("name", name.casefold())
        p = people.setdefault(key, {"user_id": int(uid) if uid is not None else None, "name": name,
            "daily": Counter(), "attendance": defaultdict(set), "full_days": set(), "review": 0, "seen_dates": set()})
        if uid is not None:
            p["name"] = name
        return p

    for answer in answers:
        poll = answer.get("daily_polls") or {}
        if isinstance(poll, list):
            poll = poll[0] if poll else {}
        day = _date(poll.get("work_date"))
        if not day or not start <= day <= end or not isinstance(answer.get("active"), bool):
            continue
        p = person(answer.get("user_id"), answer.get("user_name"))
        p["attendance"][day].add(answer["active"])
        p["seen_dates"].add(day)
        # Normal availability votes never verify a completed full working day.
        if answer.get("full_day_verified") is True and answer["active"]:
            p["full_days"].add(day)
    sources = Counter()
    for entry in entries:
        row = entry["row"]
        try:
            stamp = dt.datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, TypeError, KeyError):
            continue
        day = stamp.astimezone(MANILA).date()
        if not start <= day <= end:
            continue
        p = person(row.get("author_id"), row.get("author_name"))
        p["daily"][day] += entry["count"]
        p["seen_dates"].add(day)
        sources[row.get("source") or "unknown"] += 1
        evidence.append({"team": config["name"], "chat_id": chat_id, "user_id": p["user_id"], "member": p["name"],
            "message_id": row.get("message_id"), "sent_utc": stamp.astimezone(dt.timezone.utc).isoformat(),
            "work_date": day.isoformat(), "source": row.get("source") or "unknown", "done_deals": entry["count"],
            "url": f"https://t.me/c/{str(abs(chat_id))[3:]}/{row.get('message_id')}"})
    for row in parsed["uncertain_rows"]:
        p = person(row.get("author_id"), row.get("author_name"))
        p["review"] += 1
        try:
            p["seen_dates"].add(dt.datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00")).astimezone(MANILA).date())
        except (ValueError, TypeError, KeyError):
            pass
    dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    members = []
    for p in people.values():
        attendance, daily = p["attendance"], p["daily"]
        active = {day for day, values in attendance.items() if values == {True}}
        inactive = {day for day, values in attendance.items() if values == {False}}
        conflicting = {day for day, values in attendance.items() if len(values) != 1}
        output_days, total = sum(n > 0 for n in daily.values()), sum(daily.values())
        full_days = p["full_days"].intersection(active)
        top3 = sorted((daily[day] for day in full_days), reverse=True)[:3]
        members.append({"user_id": p["user_id"], "name": p["name"], "done_deals": total,
            "last_activity_date": max(p["seen_dates"]).isoformat() if p["seen_dates"] else None,
            "known_active_days": len(active), "known_inactive_days": len(inactive),
            "unknown_attendance_days": len(dates) - len(active) - len(inactive),
            "done_deals_on_known_active_days": sum(daily[day] for day in active),
            "average_per_known_active_day": round(sum(daily[day] for day in active) / len(active), 2) if active else None,
            "confirmed_output_days": output_days,
            "average_per_output_day": round(total / output_days, 2) if output_days else None,
            "observed_daily_peak": max(daily.values(), default=0) if daily or active else None,
            "attendance_conflicts": len(conflicting),
            "output_on_inactive_days": sum(daily[day] for day in inactive),
            "verified_full_working_days": len(full_days),
            "top_three_full_day_average": round(sum(top3) / 3, 2) if len(top3) == 3 else None,
            "maximum_capacity": None, "posts_needing_review": p["review"],
            "daily": [{"date": day.isoformat(), "done_deals": daily[day],
                       "attendance": "Active" if day in active else "Inactive" if day in inactive else "Unknown"}
                      for day in dates]})
    members.sort(key=lambda p: (-p["done_deals"], p["name"].casefold(), str(p["user_id"])))
    for member in members:
        member["planning"] = reviewed_planning(member, end)
    return {"chat_id": chat_id, "team": config["name"], "start_date": start.isoformat(), "end_date": end.isoformat(),
            "members": members, "evidence": evidence, "source_counts": dict(sources),
            "duplicate_posts_excluded": len(parsed["duplicate_rows"]), "posts_needing_review": len(parsed["uncertain_rows"])}


def report(chat_id: int, days: int, now: dt.datetime) -> dict:
    # Use only completed Philippine calendar days, keeping a partial today out of the averages.
    end = now.astimezone(MANILA).date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    start_utc = dt.datetime.combine(start, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    end_utc = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(activity_messages, chat_id, start_utc, end_utc, {GROUPS[chat_id]["done"]})
        votes = pool.submit(poll_answers_for_range, chat_id, start, end)
        return summarize(chat_id, read.result(), votes.result(), start, end)


def combined(reports: list[dict]) -> dict:
    """Recompute per-member daily output across teams; never average team averages."""
    people = defaultdict(list)
    for group in reports:
        for member in group["members"]:
            key = ("id", member["user_id"]) if member["user_id"] is not None else (group["chat_id"], member["name"].casefold())
            people[key].append((group["team"], member))
    members = []
    for values in people.values():
        days = defaultdict(list)
        for team, member in values:
            for day in member["daily"]:
                days[day["date"]].append(day)
        daily = []
        for date, rows in sorted(days.items()):
            states = [r["attendance"] for r in rows]
            if len(rows) < len(values):
                states.append("Unknown")  # A different team's archive may start later.
            state = "Active" if "Active" in states else "Inactive" if all(s == "Inactive" for s in states) else "Unknown"
            daily.append({"date": date, "done_deals": sum(r["done_deals"] for r in rows), "attendance": state})
        active = [d for d in daily if d["attendance"] == "Active"]
        output = [d for d in daily if d["done_deals"] > 0]
        total = sum(d["done_deals"] for d in daily)
        p = values[0][1]
        members.append({"user_id": p["user_id"], "name": p["name"], "teams": [team for team, _ in values],
            "last_activity_date": max((m.get("last_activity_date") or "" for _, m in values), default="") or None,
            "done_deals": total, "known_active_days": len(active),
            "known_inactive_days": sum(d["attendance"] == "Inactive" for d in daily),
            "unknown_attendance_days": sum(d["attendance"] == "Unknown" for d in daily),
            "done_deals_on_known_active_days": sum(d["done_deals"] for d in active),
            "average_per_known_active_day": round(sum(d["done_deals"] for d in active) / len(active), 2) if active else None,
            "confirmed_output_days": len(output), "average_per_output_day": round(total / len(output), 2) if output else None,
            "observed_daily_peak": max((d["done_deals"] for d in daily), default=0) if output or active else None,
            "posts_needing_review": sum(m["posts_needing_review"] for _, m in values),
            "output_on_inactive_days": sum(d["done_deals"] for d in daily if d["attendance"] == "Inactive"),
            "top_three_full_day_average": None, "maximum_capacity": None, "daily": daily})
    members.sort(key=lambda m: (-m["done_deals"], m["name"].casefold(), str(m["user_id"])))
    end = max((_date(g["end_date"]) for g in reports), default=None)
    for member in members:
        member["planning"] = reviewed_planning(member, end)
    return {"start_date": min(g["start_date"] for g in reports) if reports else None,
            "end_date": max(g["end_date"] for g in reports) if reports else None, "members": members,
            "attendance_rule": "An Active vote in any team marks that date Active. A date is Inactive only when all included team records for that member say Inactive; otherwise it is Unknown. Each date is counted once.",
            "identity_rule": "Telegram user IDs combine people across teams. Name-only records remain separate by team."}


def archive_report(chat_id: int, start: dt.date, end: dt.date) -> dict:
    if start > end:
        return summarize(chat_id, [], [], end, end, historical=True)
    start_utc = dt.datetime.combine(start, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    end_utc = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    with ThreadPoolExecutor(max_workers=3) as pool:
        live = pool.submit(activity_messages, chat_id, start_utc, end_utc, {GROUPS[chat_id]["done"]})
        exports = pool.submit(capacity_export_messages, chat_id, GROUPS[chat_id]["done"], start_utc, end_utc)
        votes = pool.submit(poll_answers_for_range, chat_id, start, end)
        # A live copy supersedes the export of the same Telegram message.
        rows = {r["message_id"]: r for r in exports.result()}
        rows.update({r["message_id"]: r for r in live.result()})
        return summarize(chat_id, list(rows.values()), votes.result(), start, end, historical=True)


def backread(allowed: set[int], now: dt.datetime, *, end: dt.date | None = None) -> dict:
    teams = allowed.intersection(GROUPS)
    end = end or now.astimezone(MANILA).date() - dt.timedelta(days=1)
    starts = {}
    for group in archive_status(teams) if teams else []:
        if int(group["chat_id"]) in teams and group.get("first_message_utc"):
            starts[int(group["chat_id"])] = dt.datetime.fromisoformat(
                group["first_message_utc"].replace("Z", "+00:00")).astimezone(MANILA).date()
    with ThreadPoolExecutor(max_workers=max(1, len(teams))) as pool:
        reports = list(pool.map(lambda chat: archive_report(chat, starts.get(chat, end), end), sorted(teams)))
    return {"ok": True, "read_only": True, "all_available_history": True, "completed_days_only": True,
            "groups": reports, "combined": combined(reports), "definitions": definitions()}


def definitions() -> dict:
    import report_topics
    if report_topics.enabled():
        import veo_forecast
        return {**DEFINITIONS, "planning": veo_forecast.DEFINITION}
    return DEFINITIONS


def compact(snapshot: dict) -> dict:
    """Avoid repeating years of empty daily rows in an HTTP response."""
    return {**snapshot, "groups": [{**g, "members": [{k: v for k, v in m.items() if k != "daily"}
            for m in g["members"]]} for g in snapshot["groups"]],
            "combined": {**snapshot["combined"], "members": [{k: v for k, v in m.items() if k != "daily"}
                         for m in snapshot["combined"]["members"]]}}


DEFINITIONS = {
    "planning": "VEO planning uses the last 30 completed dates. Mean = recorded DD on declared Active dates / those dates, including zero records. Keep decimals. Conservative average = 80% of that mean, a planning buffer rather than a guarantee. Needs seven Active dates and three output dates. Typical daily target = highest whole count achieved on at least half of Active dates and at least three dates. Show its Active-day achievement rate. Sum unrounded member averages before rounding a team total. Missing attendance/workload, incomplete archives and correlated output limit forecasts; this is recorded sales output, not verified production capacity.",
    "delivery_planning": "Planning estimates use the latest 30 completed dates. Safe plan is the rounded-down declared-Active-day average (including zero recorded output), capped at the repeated reachable level, with at least seven Active dates and three output dates. Reachable is the highest whole daily count recorded on at least three distinct dates in that window. These are planning estimates, not a verified maximum or guarantee.",
    "known_active_day_average": "Recorded Done Deals on declared Active days / declared Active days, including Active days with zero recorded Done Deals. Missing days are excluded and reported as Unknown.",
    "output_day_average": "All recorded Done Deals / days with at least one recorded Done Deal. This can overstate output per actual working day.",
    "peak": "Highest recorded daily Done Deals. Observed output, not a verified production limit.",
    "maximum": "Unknown: availability votes do not establish hours worked, available workload, completed full shifts, or maximum production capacity.",
    "top_three": "Average of the top three explicitly verified full working days, only when three such days exist. Ordinary availability votes do not verify a full day.",
    "coverage": "Known Done Deals topics plus export posts with an explicit paid marker, readable page and amount; direct export replies to DONE DEALS also supply topic evidence. Missing archive days and unclassifiable exports are not assumed to have zero output. Counts describe paid-sale posts, not verified creative completion. Staff with no identifiable record are absent. Combined metrics recompute daily totals across teams; do not add team averages or peaks.",
}

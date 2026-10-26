"""Retained 14-day VEO reference component for personal forecast comparisons.

Pure calculations only: this module cannot query, schedule or send to Telegram.
Every forecast and its rolling bias correction use completed dates only.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import defaultdict

import recent_capacity
from forecast_population import EXCLUDED_USER_IDS

POLICY = "veo-calibrated-active-v1"
WINDOW_DAYS = 14
MIN_ACTIVE_DAYS = 3
TRIAL_DEFAULT = 0.20
CALIBRATION_DAYS = 7
MIN_CALIBRATION_DATES = 3
MIN_CALIBRATION_PERSON_DAYS = 20
HISTORY_DAYS = WINDOW_DAYS + CALIBRATION_DAYS
BUFFER_FRACTION = 0.8  # A planning choice, never a prediction interval.


def excluded(member: dict) -> bool:
    uid = member.get("user_id")
    return uid is not None and int(uid) in EXCLUDED_USER_IDS


def planning(member: dict, end: dt.date) -> dict:
    """Personal reference through a completed date; no inferred attendance."""
    descriptive = recent_capacity.planning(member, end)
    active = [d["done_deals"] for d in member.get("daily", [])
              if descriptive["window_start"] <= d["date"] <= end.isoformat()
              and d["attendance"] == "Active"]
    enough = len(active) >= MIN_ACTIVE_DAYS
    mean = sum(active) / len(active) if active else None
    value = mean if enough else None
    required = max(3, math.ceil(len(active) / 2))
    target = sorted(active, reverse=True)[required - 1] if enough else None
    support = sum(n >= target for n in active) if target is not None and target > 0 else None
    return {**descriptive, "policy": POLICY,
        "best_three_stretch_per_day": descriptive["estimated_capacity_per_day"],
        "observed_stretch_per_day": descriptive["estimated_capacity_per_day"],
        "recorded_dd_on_active_dates": sum(active),
        "uncalibrated_forecast_per_day": value,
        "estimated_capacity_per_day": value, "planning_average_per_day": value,
        "safe_plan_per_day": value * BUFFER_FRACTION if value is not None else None,
        "minimum_active_days": MIN_ACTIVE_DAYS, "minimum_repeat_days": 0,
        "reachable_target_per_day": target, "reachable_support_days": support,
        "target_achievement_percent": 100 * support / len(active) if support is not None else None,
        "safe_target_met_active_days": None, "safe_target_active_days": len(active),
        "buffer_fraction": BUFFER_FRACTION, "basis": "personal_active_day_average",
        "provisional": False, "forecast_validation": "trial; future validation pending",
        "maximum_capacity": None}


def _raw(member: dict, end: dt.date) -> float:
    lo = (end - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    active = [d["done_deals"] for d in member.get("daily", [])
              if lo <= d["date"] <= end.isoformat() and d["attendance"] == "Active"]
    return sum(active) / len(active) if len(active) >= MIN_ACTIVE_DAYS else TRIAL_DEFAULT


def calibration(members: list[dict], end: dt.date) -> dict:
    """Replay raw forecasts on prior dates before correcting the next forecast.

    Only matched, declared-Active person-dates enter either side of the ratio.
    Missing attendance is never a zero. Lack of evidence leaves the factor at 1.
    Downward correction addresses observed optimism; it cannot invent an uplift.
    """
    people = {int(m["user_id"]): m for m in members
              if m.get("user_id") is not None and not excluded(m)}
    dates = [d["date"] for m in people.values() for d in m.get("daily", [])
             if d["date"] <= end.isoformat()]
    history_start = dt.date.fromisoformat(min(dates)) if dates else None
    start = end - dt.timedelta(days=CALIBRATION_DAYS - 1)
    totals = defaultdict(lambda: {"predicted": 0.0, "recorded": 0, "active": 0})
    if history_start is not None:
        # Do not replay a 14-day formula from a truncated cache of fewer dates.
        first_score = history_start + dt.timedelta(days=WINDOW_DAYS)
        for member in people.values():
            for day in member.get("daily", []):
                if not max(start, first_score).isoformat() <= day["date"] <= end.isoformat():
                    continue
                if day["attendance"] != "Active":
                    continue
                target = dt.date.fromisoformat(day["date"])
                row = totals[day["date"]]
                row["predicted"] += _raw(member, target - dt.timedelta(days=1))
                row["recorded"] += day["done_deals"]
                row["active"] += 1
    predicted = sum(r["predicted"] for r in totals.values())
    recorded = sum(r["recorded"] for r in totals.values())
    observations = sum(r["active"] for r in totals.values())
    enough = (len(totals) >= MIN_CALIBRATION_DATES
              and observations >= MIN_CALIBRATION_PERSON_DAYS and predicted > 0)
    factor = min(1.0, recorded / predicted) if enough else 1.0
    return {"factor": factor, "available": enough, "adjusted": enough and factor < 1,
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "scored_dates": len(totals), "active_person_days": observations,
        "raw_forecast_total": predicted, "recorded_dd": recorded,
        "minimum_dates": MIN_CALIBRATION_DATES,
        "minimum_active_person_days": MIN_CALIBRATION_PERSON_DAYS,
        "basis": "prior matched Active-date recorded DD / raw forecast; capped at 1",
        "status": "retrospective calibration; future validation pending" if enough else "insufficient calibration history",
        "daily": [{"date": date, **r} for date, r in sorted(totals.items())]}


def baseline(members: list[dict], end: dt.date | None = None) -> dict:
    people = {int(m["user_id"]): m for m in members
              if m.get("user_id") is not None and not excluded(m)}
    if end is None:
        dates = [(m.get("planning") or {}).get("window_end") for m in people.values()]
        dates = [d for d in dates if d]
        end = dt.date.fromisoformat(max(dates)) if dates else None
    plans = [planning(m, end) if "daily" in m and end is not None else m.get("planning") or {}
             for m in people.values()]
    peers = [p["active_day_average"] for p in plans
             if p.get("known_active_days", 0) >= MIN_ACTIVE_DAYS and p.get("active_day_average") is not None]
    correction = calibration(list(people.values()), end) if end is not None else {
        "factor": 1.0, "available": False, "adjusted": False, "status": "insufficient calibration history"}
    return {"policy": POLICY, "business": "veo", "capacity_per_day": TRIAL_DEFAULT,
        "source": "provisional_trial_default", "unit": "Done Deals/day", "provisional": True,
        "minimum_active_days": MIN_ACTIVE_DAYS, "minimum_output_days": 0,
        "peer_reference_per_day": statistics.median(peers) if peers else None,
        "peer_members": len(peers), "history_through": end.isoformat() if end else None,
        "calibration": correction, "excluded_user_ids": sorted(EXCLUDED_USER_IDS),
        "validation": "0.20 is a trial starting rate; not a validated capacity or quota"}


def apply(member: dict, default: dict) -> dict:
    plan = dict(member.get("planning") or {})
    end = default.get("history_through") or plan.get("window_end")
    if "daily" in member and end:
        metadata = {key: plan[key] for key in ("historical_daily_peak", "historical_peak_date", "daily_capacity_change") if key in plan}
        plan = {**planning(member, dt.date.fromisoformat(end)), **metadata}
    if excluded(member):
        plan.update(planning_average_per_day=None, estimated_capacity_per_day=None,
            safe_plan_per_day=None, forecast_excluded=True, estimate_origin="manager_excluded", provisional=False)
        return {**member, "planning": plan}
    # Never accept a cached top-three estimate as an ordinary personal forecast.
    personal = (plan.get("policy") == POLICY and plan.get("known_active_days", 0) >= MIN_ACTIVE_DAYS
                and plan.get("uncalibrated_forecast_per_day") is not None
                and (not end or plan.get("window_end") == end))
    raw = plan["uncalibrated_forecast_per_day"] if personal else default.get("capacity_per_day")
    correction = default.get("calibration") or {"factor": 1.0, "available": False}
    value = raw * correction["factor"] if raw is not None else None
    plan.update(policy=POLICY, uncalibrated_forecast_per_day=raw,
        estimated_capacity_per_day=value if personal else None, planning_average_per_day=value,
        safe_plan_per_day=value * BUFFER_FRACTION if value is not None else None,
        provisional=not personal and value is not None, default_policy=POLICY,
        default_capacity_per_day=default.get("capacity_per_day"),
        default_peer_members=default.get("peer_members", 0), buffer_fraction=BUFFER_FRACTION,
        calibration_factor=correction["factor"], calibration_available=correction.get("available", False),
        estimate_origin="personal_active_day_average" if personal else "trial_default" if value is not None else "unknown",
        forecast_validation="trial; future validation pending", maximum_capacity=None)
    return {**member, "planning": plan}


DEFINITION = (
    "VEO recorded-DD forecast, not verified production capacity. Last 14 completed PHT dates: "
    "DD on declared Active dates / Active dates, including zero-output Active dates. At least 3 Active "
    "dates are required for a personal rate. Otherwise use a labelled 0.20 trial default. Unknown/Inactive "
    "dates do not enter that rate. A rolling correction uses the preceding 7 completed dates' matched "
    "Active-date actual/raw-forecast ratio, capped at 1, after 3 scored dates and 20 Active person-days. "
    "Without enough calibration history the raw estimate is labelled uncalibrated. Sum fractional member "
    "forecasts before rounding; 80% is an optional planning buffer, not a safety guarantee. Best-three "
    "output remains a stretch reference. All verified current human members, including Melo, are eligible. "
    "Shared people make page/team forecasts overlap; page access does not allocate workload. "
    "All forecasts remain provisional pending future outcomes; missing attendance and posts limit accuracy."
)

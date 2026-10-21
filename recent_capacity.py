"""Fourteen-day descriptive output and the requested top-three planning estimate."""
from __future__ import annotations

import datetime as dt
import math

WINDOW_DAYS = 14
POLICY = "top-three-14d-v1"


def planning(member: dict, end: dt.date) -> dict:
    start = end - dt.timedelta(days=WINDOW_DAYS - 1)
    daily = [d for d in member.get("daily", []) if start.isoformat() <= d["date"] <= end.isoformat()]
    active = [d["done_deals"] for d in daily if d["attendance"] == "Active"]
    output = [d for d in daily if d["done_deals"] > 0]
    # A positive post confirms recorded output, not verified attendance or hours.
    # Explicit Inactive/output conflicts remain visible, but cannot set the plan.
    eligible = sorted((d for d in output if d["attendance"] != "Inactive"),
                      key=lambda d: (d["done_deals"], d["date"]), reverse=True)
    top = eligible[:3]
    enough = len(top) == 3
    mean = sum(d["done_deals"] for d in top) / 3 if enough else None
    safe = sum(d["done_deals"] for d in top) * 4 // 15 if enough else None
    confirmed = enough and all(d["attendance"] == "Active" for d in top)
    target = math.floor(mean) if mean is not None else None
    return {"policy": POLICY, "window_start": start.isoformat(), "window_end": end.isoformat(),
        "known_active_days": len(active), "output_days": len(output), "eligible_output_days": len(eligible),
        "active_day_average": sum(active) / len(active) if active else None,
        "output_day_average": sum(d["done_deals"] for d in output) / len(output) if output else None,
        "observed_daily_peak": max((d["done_deals"] for d in output), default=None),
        "top_three_daily_outputs": [d["done_deals"] for d in top],
        "top_three_dates": [d["date"] for d in top], "estimated_capacity_per_day": mean,
        "planning_average_per_day": mean, "safe_plan_per_day": safe,
        "reachable_target_per_day": target,
        "reachable_support_days": sum(d["done_deals"] >= target for d in eligible) if target else None,
        "target_active_days": len(active), "minimum_active_days": 0, "minimum_repeat_days": 3,
        "basis": "declared_active_output_days" if confirmed else "observed_output_days",
        "attendance_confirmed_for_top_three": confirmed, "buffer_fraction": 0.8,
        "inactive_output_conflicts": sum(d["attendance"] == "Inactive" for d in output),
        "safe_target_met_active_days": sum(n >= safe for n in active) if safe and active else None,
        "safe_target_active_days": len(active),
        "missing_attendance_days": sum(d["attendance"] == "Unknown" for d in daily),
        "estimates_only": True, "maximum_capacity": None}


DEFINITION = ("Last 14 completed Philippine dates. Estimated planning capacity is the mean of the top three "
    "positive daily outputs on distinct dates, excluding explicit Inactive/output conflicts. Safe target is "
    "floor(estimate × 0.80). With fewer than three eligible output dates, the estimate/target is unknown. "
    "Output-day average and observed daily peak are descriptive; positive records do not verify attendance or full shifts. "
    "Unknown dates are not Inactive. Active-day averages include known Active dates with zero output. "
    "No assumed-utilization uplift or verified production maximum is inferred.")

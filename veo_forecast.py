"""Choose a VEO forecast from the member's earlier prediction errors.

Pure calculations only. Both alternatives and every selection use completed
dates before the predicted day. No Telegram queries, scheduling or delivery.
"""
from __future__ import annotations

import datetime as dt

import veo_forecast_reference as reference
import veo_forecast_recent as recent

POLICY = "veo-person-choice-v3-all-members-v2"
WINDOW_DAYS = reference.WINDOW_DAYS
MIN_ACTIVE_DAYS = reference.MIN_ACTIVE_DAYS
TRIAL_DEFAULT = reference.TRIAL_DEFAULT
BUFFER_FRACTION = reference.BUFFER_FRACTION
CALIBRATION_DAYS = reference.CALIBRATION_DAYS
MIN_CALIBRATION_DATES = reference.MIN_CALIBRATION_DATES
MIN_CALIBRATION_PERSON_DAYS = reference.MIN_CALIBRATION_PERSON_DAYS
SELECTION_DAYS = 14
MIN_SELECTION_OBSERVATIONS = 3
# The first selection replay needs 14 training dates plus 7 correction dates.
HISTORY_DAYS = reference.HISTORY_DAYS + SELECTION_DAYS
EXCLUDED_USER_IDS = reference.EXCLUDED_USER_IDS
excluded = reference.excluded


def planning(member: dict, end: dt.date) -> dict:
    """Unselected personal reference; cohort context is required for selection."""
    return {**reference.planning(member, end), "policy": POLICY,
        "selected_model": reference.POLICY,
        "selection_status": "reference only; prior-error replay needs the cohort"}


def calibration(members: list[dict], end: dt.date) -> dict:
    """Legacy component diagnostic; final choices can use different corrections."""
    return reference.calibration(members, end)


def _choices(people: list[dict], end: dt.date) -> dict:
    result = {str(m["user_id"]): {"scored_person_days": 0,
        "reference_absolute_error": 0.0, "reference_squared_error": 0.0,
        "recent_absolute_error": 0.0, "recent_squared_error": 0.0,
        "window_start": str(end - dt.timedelta(days=SELECTION_DAYS - 1)), "window_end": str(end),
        "minimum_observations": MIN_SELECTION_OBSERVATIONS}
        for m in people}
    dates = [r["date"] for m in people for r in m.get("daily", []) if r["date"] <= str(end)]
    if not dates:
        return result
    first_replay = dt.date.fromisoformat(min(dates)) + dt.timedelta(days=reference.HISTORY_DAYS)
    start = max(end - dt.timedelta(days=SELECTION_DAYS - 1), first_replay)
    day = start
    while day <= end:
        targets = [(m, next((r for r in m.get("daily", []) if r["date"] == str(day)), None)) for m in people]
        active = [(m, r) for m, r in targets if r and r["attendance"] == "Active"]
        if active:
            prior = day - dt.timedelta(days=1)
            old_default, new_default = reference.baseline(people, prior), recent.baseline(people, prior)
            for member, actual in active:
                old = reference.apply(member, old_default)["planning"]["planning_average_per_day"]
                new = recent.apply(member, new_default)["planning"]["planning_average_per_day"]
                if old is None or new is None:
                    continue
                stats = result[str(member["user_id"])]
                stats["scored_person_days"] += 1
                for label, value in (("reference", old), ("recent", new)):
                    stats[label + "_absolute_error"] += abs(value - actual["done_deals"])
                    stats[label + "_squared_error"] += (value - actual["done_deals"]) ** 2
        day += dt.timedelta(days=1)
    return result


def baseline(members: list[dict], end: dt.date | None = None) -> dict:
    people = list({int(m["user_id"]): m for m in members
        if m.get("user_id") is not None and not excluded(m)}.values())
    old = reference.baseline(people, end)
    if end is None and old.get("history_through"):
        end = dt.date.fromisoformat(old["history_through"])
    new = recent.baseline(people, end)
    choices = _choices(people, end) if end else {}
    for stats in choices.values():
        enough = stats["scored_person_days"] >= MIN_SELECTION_OBSERVATIONS
        better = (stats["recent_absolute_error"] < stats["reference_absolute_error"] - 1e-10
                  and stats["recent_squared_error"] <= stats["reference_squared_error"] + 1e-10)
        stats["selected_model"] = recent.POLICY if enough and better else reference.POLICY
        stats["reason"] = ("recent model improved both earlier personal error checks" if enough and better else
            "reference retained: recent model did not improve both personal errors" if enough else
            "reference retained: fewer than 3 matched earlier personal forecasts")
    return {**old, "policy": POLICY, "selection_source": "person_model_choice",
        "components": {reference.POLICY: old, recent.POLICY: new}, "person_choices": choices,
        "calibration": {"per_person": True, "available": old["calibration"].get("available", False),
            "factor": 1.0, "status": "correction belongs to the selected component; no shared final multiplier"},
        "validation": "personal choices use earlier errors only; retrospective improvement is modest and not universal"}


def apply(member: dict, default: dict) -> dict:
    uid = member.get("user_id")
    decision = default.get("person_choices", {}).get(str(uid), {})
    chosen = decision.get("selected_model", reference.POLICY)
    component = recent if chosen == recent.POLICY else reference
    child = dict(default.get("components", {}).get(chosen) or default)
    # Retain explicit supplied cold-start configuration when using the reference.
    if component is reference and "capacity_per_day" in default:
        child["capacity_per_day"] = default["capacity_per_day"]
    child["policy"] = chosen
    plan = dict(member.get("planning") or {})
    if plan.get("policy") == POLICY and plan.get("selected_model", reference.POLICY) == chosen:
        plan["policy"] = chosen
    result = component.apply({**member, "planning": plan}, child)
    result["planning"] = {**result["planning"], "policy": POLICY, "selected_model": chosen,
        "selection": decision, "selection_status": decision.get("reason", "reference retained: no comparable personal forecast errors"),
        "forecast_validation": "trial; improvement is not universal and future validation is pending"}
    return result


DEFINITION = (
    "VEO forecasts recorded DD, not verified production capacity. Each person's reference uses the "
    "last 14 completed PHT dates' declared-Active output average, including zero-output Active dates, "
    "after 3 Active dates; otherwise the labelled 0.20/day trial default. A recent alternative uses "
    "7 completed dates: (Active-date DD + 3 x 0.20) / (Active dates + 3), blending each person's own data "
    "with a weak prior. Both components correct past optimism from their own prior 7 dates' matched "
    "actual/raw-forecast ratio after 3 scored dates and 20 Active person-days. Select the recent model "
    "only after at least 3 earlier personal forecasts in the prior 14 dates have lower absolute error "
    "and no higher squared error than the reference. Every component replay excludes its own target "
    "and future dates. Otherwise retain the reference. Missing attendance is unknown, never zero. "
    "Sum fractional forecasts before rounding; the 80% buffer is a planning choice, not a guarantee. "
    "Best-three output is a stretch reference. All verified current human members, including Melo, are eligible. "
    "Combined personal DD and team-only DD are different scopes; page access does not allocate output. "
    "Per-person and per-day errors are reported separately. Some members/teams still worsen; no "
    "historical selection proves future accuracy or production limits."
)

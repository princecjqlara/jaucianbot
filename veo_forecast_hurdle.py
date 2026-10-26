"""Experimental personal occurrence/size forecast; pure, no delivery or I/O.

The private development replay improves squared error but worsens absolute error
against v3. This is an explicit CLI trial, not the organized-report default.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import recent_capacity
import veo_forecast_reference as reference

POLICY = "veo-person-hurdle-trial-v4-all-members-v2"
WINDOW_DAYS = 28
CALIBRATION_DAYS = 7
HISTORY_DAYS = WINDOW_DAYS + CALIBRATION_DAYS
MIN_ACTIVE_DAYS = 3
MIN_CALIBRATION_DATES = 3
MIN_CALIBRATION_PERSON_DAYS = 20
TRIAL_DEFAULT = .20
OCCURRENCE_HALF_LIFE_DAYS = 7
SIZE_HALF_LIFE_DAYS = 14
OCCURRENCE_PRIOR_ACTIVE_DAYS = 7
SIZE_PRIOR_POSITIVE_DAYS = 3
PEER_PRIOR_ACTIVE_DAYS = 10
PEER_PRIOR_POSITIVE_DAYS = 5
TRIAL_OCCURRENCE = .16
TRIAL_POSITIVE_COUNT = 1.25  # .16 * 1.25 = .20; trial anchors, not learned facts.
BUFFER_FRACTION = .8
EXCLUDED_USER_IDS = reference.EXCLUDED_USER_IDS
excluded = reference.excluded


def _people(members):
    return list({int(m["user_id"]): m for m in members
                 if m.get("user_id") is not None and not excluded(m)}.values())


def _features(member, end, window=WINDOW_DAYS, half_life=None):
    start = str(end - dt.timedelta(days=window - 1))
    rows = [r for r in member.get("daily", []) if start <= r["date"] <= str(end)
            and r["attendance"] == "Active"]
    weights = [2 ** (-(end - dt.date.fromisoformat(r["date"])).days / half_life)
               if half_life else 1.0 for r in rows]
    return {"active": sum(weights), "positive": sum(w for r, w in zip(rows, weights) if r["done_deals"] > 0),
            "dd": sum(w * r["done_deals"] for r, w in zip(rows, weights)),
            "dates": len(rows), "recorded_dd": sum(r["done_deals"] for r in rows),
            "last_active_date": max((r["date"] for r in rows), default=None)}


def _prior(features):
    probability = (features["positive"] + PEER_PRIOR_ACTIVE_DAYS * TRIAL_OCCURRENCE) / (features["active"] + PEER_PRIOR_ACTIVE_DAYS)
    positive_count = (features["dd"] + PEER_PRIOR_POSITIVE_DAYS * TRIAL_POSITIVE_COUNT) / (features["positive"] + PEER_PRIOR_POSITIVE_DAYS)
    return probability, positive_count


def _raw_plan(member, end, peer_features):
    occurrence = _features(member, end, half_life=OCCURRENCE_HALF_LIFE_DAYS)
    size = _features(member, end, half_life=SIZE_HALF_LIFE_DAYS)
    observed = _features(member, end)
    q0, mu0 = _prior(peer_features)
    probability = (occurrence["positive"] + OCCURRENCE_PRIOR_ACTIVE_DAYS * q0) / (occurrence["active"] + OCCURRENCE_PRIOR_ACTIVE_DAYS)
    positive_count = (size["dd"] + SIZE_PRIOR_POSITIVE_DAYS * mu0) / (size["positive"] + SIZE_PRIOR_POSITIVE_DAYS)
    raw = probability * positive_count
    enough = observed["dates"] >= MIN_ACTIVE_DAYS
    descriptive = recent_capacity.planning(member, end)
    return {**descriptive, "policy": POLICY, "window_start": str(end - dt.timedelta(days=WINDOW_DAYS - 1)),
        "window_end": str(end), "basis": "personal_occurrence_times_positive_count",
        "known_active_days": observed["dates"], "recorded_dd_on_active_dates": observed["recorded_dd"],
        "active_day_average": observed["recorded_dd"] / observed["dates"] if observed["dates"] else None,
        "best_three_stretch_per_day": descriptive["estimated_capacity_per_day"],
        "stretch_window_days": recent_capacity.WINDOW_DAYS,
        "uncalibrated_forecast_per_day": raw,
        "uncalibrated_probability_any_dd": probability,
        "expected_dd_given_positive": positive_count,
        "peer_prior_probability_any_dd": q0, "peer_prior_positive_count": mu0,
        "peer_active_dates": peer_features["active"], "peer_positive_dates": peer_features["positive"],
        "effective_active_dates": occurrence["active"], "effective_positive_dates": size["positive"],
        "personal_weight": occurrence["active"] / (occurrence["active"] + OCCURRENCE_PRIOR_ACTIVE_DAYS),
        "minimum_active_days": MIN_ACTIVE_DAYS, "last_active_date": observed["last_active_date"],
        "minimum_repeat_days": 0, "reachable_target_per_day": None, "reachable_support_days": None,
        "safe_target_met_active_days": None, "safe_target_active_days": observed["dates"],
        "observed_zero_rate": bool(observed["dates"]) and observed["recorded_dd"] == 0,
        "provisional": not enough, "estimated_capacity_per_day": raw if enough else None,
        "planning_average_per_day": raw, "maximum_capacity": None,
        "estimate_origin": "personal_hurdle_count" if enough else "provisional_personal_peer_blend",
        "forecast_validation": "experimental development model; lower squared error but higher absolute error than v3"}


def _raw_plans(people, end):
    peer = {int(m["user_id"]): _features(m, end, CALIBRATION_DAYS) for m in people}
    totals = {k: sum(r[k] for r in peer.values()) for k in ("active", "positive", "dd")}
    return {str(m["user_id"]): _raw_plan(m, end, {k: totals[k] - peer[int(m["user_id"])][k] for k in totals}) for m in people}, totals


def calibration(members, end):
    people = _people(members)
    dates = [r["date"] for m in people for r in m.get("daily", []) if r["date"] <= str(end)]
    start = end - dt.timedelta(days=CALIBRATION_DAYS - 1)
    totals = defaultdict(lambda: {"predicted": 0.0, "recorded": 0, "active": 0})
    if dates:
        first_score = dt.date.fromisoformat(min(dates)) + dt.timedelta(days=WINDOW_DAYS)
        day = max(start, first_score)
        while day <= end:
            targets = [(m, next((r for r in m.get("daily", []) if r["date"] == str(day) and r["attendance"] == "Active"), None)) for m in people]
            active = [(m, r) for m, r in targets if r is not None]
            if active:
                plans, _ = _raw_plans(people, day - dt.timedelta(days=1))
                totals[str(day)] = {"predicted": sum(plans[str(m["user_id"])]["uncalibrated_forecast_per_day"] for m, _ in active),
                                    "recorded": sum(r["done_deals"] for _, r in active), "active": len(active)}
            day += dt.timedelta(days=1)
    predicted = sum(r["predicted"] for r in totals.values())
    recorded = sum(r["recorded"] for r in totals.values())
    n = sum(r["active"] for r in totals.values())
    enough = len(totals) >= MIN_CALIBRATION_DATES and n >= MIN_CALIBRATION_PERSON_DAYS and predicted > 0
    factor = min(1.0, recorded / predicted) if enough else 1.0
    return {"available": enough, "adjusted": enough and factor < 1, "factor": factor,
            "window_start": str(start), "window_end": str(end), "scored_dates": len(totals),
            "active_person_days": n, "raw_forecast_total": predicted, "recorded_dd": recorded,
            "minimum_dates": MIN_CALIBRATION_DATES, "minimum_active_person_days": MIN_CALIBRATION_PERSON_DAYS,
            "status": "development correction; probability calibration unvalidated" if enough else "insufficient calibration history",
            "daily": [{"date": date, **row} for date, row in sorted(totals.items())]}


def _correct(plan, correction):
    result = dict(plan)
    factor = correction["factor"]
    value = plan["uncalibrated_forecast_per_day"] * factor
    result.update(planning_average_per_day=value, estimated_capacity_per_day=None if plan["provisional"] else value,
        safe_plan_per_day=BUFFER_FRACTION * value, buffer_fraction=BUFFER_FRACTION,
        model_probability_any_dd=plan["uncalibrated_probability_any_dd"] * factor,
        probability_validation="model estimate only; not a validated probability of future sales",
        calibration_factor=factor, calibration_available=correction["available"], default_policy=POLICY)
    return result


def baseline(members, end=None):
    people = _people(members)
    if end is None:
        dates = [(m.get("planning") or {}).get("window_end") for m in people]
        end = dt.date.fromisoformat(max(d for d in dates if d)) if any(dates) else None
    if end is None:
        correction = {"factor": 1.0, "available": False, "status": "dated history required"}
        plans, peer = {}, {"active": 0, "positive": 0, "dd": 0}
    else:
        plans, peer = _raw_plans(people, end)
        correction = calibration(people, end)
    q, mu = _prior(peer)
    return {"policy": POLICY, "business": "veo", "source": "experimental_peer_prior",
        "capacity_per_day": q * mu, "trial_anchor_per_day": TRIAL_DEFAULT,
        "provisional": True, "unit": "Done Deals/day", "minimum_active_days": MIN_ACTIVE_DAYS,
        "history_through": str(end) if end else None, "calibration": correction, "peer_features": peer,
        "person_plans": {uid: _correct(p, correction) for uid, p in plans.items()},
        "excluded_user_ids": sorted(EXCLUDED_USER_IDS),
        "validation": "experimental; improves squared errors but worsens absolute error; not the default policy"}


def planning(member, end):
    return apply(member, baseline([member], end))["planning"]


def apply(member, default):
    if excluded(member):
        return {**member, "planning": {**(member.get("planning") or {}), "policy": POLICY,
            "forecast_excluded": True, "planning_average_per_day": None, "estimated_capacity_per_day": None,
            "safe_plan_per_day": None, "provisional": False, "estimate_origin": "manager_excluded"}}
    plan = default.get("person_plans", {}).get(str(member.get("user_id")))
    if plan is None:
        end = default.get("history_through")
        if not end:
            raise ValueError("A completed history-through date is required")
        # A new roster member has no matched personal history in this baseline.
        # Use the supplied cohort prior; never trust a stale compact forecast.
        raw = _raw_plan({"daily": []}, dt.date.fromisoformat(end), default["peer_features"])
        plan = _correct(raw, default["calibration"])
    metadata = {k: v for k, v in (member.get("planning") or {}).items()
                if k in ("historical_daily_peak", "historical_peak_date", "daily_capacity_change")}
    return {**member, "planning": {**plan, **metadata, "default_capacity_per_day": default["capacity_per_day"]}}


DEFINITION = (
    "Experimental personal hurdle recorded-DD forecast. Last 28 completed PHT dates, declared Active only. "
    "Forecast = model chance of positive DD x expected DD conditional on positive output. Occurrence "
    "uses 7-day half-life weights and 7 pseudo-Active dates; positive count uses 14-day half-life weights "
    "and 3 pseudo-positive dates. Starting values use other members' prior 7 completed dates, leaving "
    "this person's data out; trial anchors are 0.16 chance x 1.25 positive DD = 0.20/day, with 10/5 "
    "peer pseudo-dates. New/sparse people receive a labelled cohort blend, not a validated capacity. "
    "A matched raw-forecast correction on 7 earlier dates is capped at 1, requiring 3 scored dates and "
    "20 Active person-days; every replay trains before its own target. Active zeros count; Unknown/Inactive "
    "do not train or score. Melo is eligible on the same terms as other current human members. No target/future observation, hours, demand or page allocation is "
    "inferred. Some squared errors improve but absolute errors worsen in development. Model probability "
    "is unvalidated; neither this expected sales count nor an 80% buffer establishes a production limit. "
    "This trial is selected explicitly in the offline CLI; v3 remains the organized-report default."
)

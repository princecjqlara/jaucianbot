"""Explicit day-specific personal trial, with matched forecast horizons.

Pure calculations. Fit a regularized correction to v5 using lagged features,
then require earlier personal/positive-date evidence before changing a forecast.
No organized-report default, availability or Telegram delivery is changed.
"""
from __future__ import annotations

import copy
import datetime as dt
import math

import veo_forecast_advanced as control

POLICY = "veo-person-day-trial-v6-all-members-v2"
TRAINING_DAYS = 14
TRAINING_HALF_LIFE_DAYS = 14
RIDGE_STRENGTH = 20.0
MIN_TRAINING_PERSON_DAYS = 100
CALIBRATION_DAYS = 7
MIN_CALIBRATION_DATES = 3
MIN_CALIBRATION_PERSON_DAYS = 20
SELECTION_DAYS = 28
MIN_SELECTION_OBSERVATIONS = 5
MIN_SELECTION_POSITIVE_DATES = 2
HISTORY_DAYS = 128  # Recommended cache for 1/2-calendar-day history lags.
WINDOW_DAYS = 28
TRIAL_DEFAULT = .2
MIN_ACTIVE_DAYS = 3
BUFFER_FRACTION = .8
EXCLUDED_USER_IDS = control.EXCLUDED_USER_IDS
excluded = control.excluded
FEATURE_NAMES = ("intercept", "base_forecast", "personal_recent_trend", "personal_positive_frequency",
    "last_active_positive", "last_active_dd_scaled", "gap_since_active", "recency_of_positive",
    "target_weekend", "peer_recent_rate")


def _solve(matrix, rhs):
    """Small positive-definite ridge system, solved with pivoted elimination."""
    n = len(rhs)
    rows = [list(row) + [value] for row, value in zip(matrix, rhs)]
    for j in range(n):
        pivot = max(range(j, n), key=lambda i: abs(rows[i][j]))
        rows[j], rows[pivot] = rows[pivot], rows[j]
        divisor = rows[j][j]
        if abs(divisor) < 1e-12:
            raise ValueError("Singular forecast fit")
        rows[j] = [value / divisor for value in rows[j]]
        for i in range(n):
            if i != j:
                factor = rows[i][j]
                rows[i] = [a - factor * b for a, b in zip(rows[i], rows[j])]
    return [row[-1] for row in rows]


def _fit(samples):
    n = len(FEATURE_NAMES)
    matrix = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n
    for features, outcome, base, weight in samples:
        for j in range(n):
            rhs[j] += features[j] * (outcome - base) * weight
            for k in range(n):
                matrix[j][k] += features[j] * features[k] * weight
    for j in range(n):
        matrix[j][j] += RIDGE_STRENGTH
    return _solve(matrix, rhs)


class _Replay:
    POLICY = POLICY

    def __init__(self, members):
        self.people = copy.deepcopy(list({int(p["user_id"]): p for p in members
            if p.get("user_id") is not None and not excluded(p)}.values()))
        self.observations = {p["user_id"]: sorted((dt.date.fromisoformat(r["date"]), r["done_deals"])
            for r in p.get("daily", []) if r["attendance"] == "Active") for p in self.people}
        self.records = sorted((day, uid, value) for uid, rows in self.observations.items() for day, value in rows)
        self.base = control.for_replay(self.people)
        self.base_cache, self.feature_cache, self.raw_cache, self.candidate_cache = {}, {}, {}, {}

    @staticmethod
    def excluded(member):
        return excluded(member)

    def _base(self, end):
        if end not in self.base_cache:
            default = self.base.baseline(self.people, end)
            self.base_cache[end] = (default, {p["user_id"]: control.apply(p, default)["planning"] for p in self.people})
        return self.base_cache[end]

    def _features(self, uid, target, end):
        key = uid, target, end
        if key in self.feature_cache:
            return self.feature_cache[key]
        rows = [(day, value) for day, value in self.observations[uid] if end - dt.timedelta(days=27) <= day <= end]
        recent = [(day, value) for day, value in rows if day >= end - dt.timedelta(days=6)]
        mean = lambda values: (sum(y for _, y in values) + .6) / (len(values) + 3)
        chance = (sum(y > 0 for _, y in rows) + .48) / (len(rows) + 3)
        pred = self._base(end)[1][uid]["planning_average_per_day"]
        peers = [(day, value) for other, observed in self.observations.items() if other != uid
                 for day, value in observed if end - dt.timedelta(days=6) <= day <= end]
        last = rows[-1] if rows else None
        positive_dates = [day for day, value in rows if value > 0]
        features = (1.0, pred, mean(recent) - mean(rows), chance, float(bool(last and last[1] > 0)),
            min(3, last[1]) / 3 if last else 0.0,
            min(14, (target - last[0]).days) / 14 if last else 1.0,
            math.exp(-(target - max(positive_dates)).days / 7) if positive_dates else 0.0,
            float(target.weekday() >= 5), (sum(value for _, value in peers) + 2) / (len(peers) + 10))
        self.feature_cache[key] = features, pred
        return features, pred

    def _raw(self, target, end):
        key = target, end
        if key in self.raw_cache:
            return self.raw_cache[key]
        horizon = (target - end).days
        samples = []
        for day, uid, actual in self.records:
            if not end - dt.timedelta(days=TRAINING_DAYS - 1) <= day <= end:
                continue
            features, pred = self._features(uid, day, day - dt.timedelta(days=horizon))
            weight = 2 ** (-(end - day).days / TRAINING_HALF_LIFE_DAYS)
            samples.append((features, actual, pred, weight))
        fitted = len(samples) >= MIN_TRAINING_PERSON_DAYS
        coefficients = _fit(samples) if fitted else [0.0] * len(FEATURE_NAMES)
        plans = {}
        for uid in self.observations:
            features, previous = self._features(uid, target, end)
            value = max(0.0, previous + sum(a * b for a, b in zip(coefficients, features))) if fitted else previous
            plans[uid] = {"raw_prediction": value, "training_person_days": len(samples), "fitted": fitted}
        result = {"plans": plans, "training_person_days": len(samples), "fitted": fitted,
            "coefficients": dict(zip(FEATURE_NAMES, coefficients)),
            "training_window_start": str(end - dt.timedelta(days=TRAINING_DAYS - 1)), "training_window_end": str(end)}
        self.raw_cache[key] = result
        return result

    def _candidate(self, target, end):
        key = target, end
        if key in self.candidate_cache:
            return self.candidate_cache[key]
        horizon = (target - end).days
        raw = self._raw(target, end)
        matched = []
        for day, uid, actual in self.records:
            if end - dt.timedelta(days=CALIBRATION_DAYS - 1) <= day <= end:
                prediction = self._raw(day, day - dt.timedelta(days=horizon))["plans"][uid]["raw_prediction"]
                matched.append((day, actual, prediction))
        expected = sum(r[2] for r in matched)
        enough = len(matched) >= MIN_CALIBRATION_PERSON_DAYS and len({r[0] for r in matched}) >= MIN_CALIBRATION_DATES and expected > 0
        factor = min(1.0, sum(r[1] for r in matched) / expected) if enough else 1.0
        result = {**raw, "factor": factor, "calibration_available": enough,
            "plans": {uid: {**p, "prediction": p["raw_prediction"] * factor} for uid, p in raw["plans"].items()}}
        self.candidate_cache[key] = result
        return result

    def baseline_for_date(self, members, end, target):
        if target <= end:
            raise ValueError("Forecast target must be after the history-through date")
        horizon = (target - end).days
        default, fallback = self._base(end)
        required = control.HISTORY_DAYS + TRAINING_DAYS + CALIBRATION_DAYS + SELECTION_DAYS + 4 * horizon
        dates = [r["date"] for p in self.people for r in p.get("daily", []) if r["date"] <= str(end)]
        complete = bool(dates) and (end - dt.date.fromisoformat(min(dates))).days + 1 >= required
        candidate = self._candidate(target, end) if complete else None
        choices, plans = {}, {}
        for uid, observations in self.observations.items():
            stats = {"window_start": str(end - dt.timedelta(days=SELECTION_DAYS - 1)), "window_end": str(end),
                "scored_person_days": 0, "positive_dates": 0, "minimum_observations": MIN_SELECTION_OBSERVATIONS,
                "minimum_positive_dates": MIN_SELECTION_POSITIVE_DATES,
                "reference_absolute_error": 0.0, "reference_squared_error": 0.0,
                "day_absolute_error": 0.0, "day_squared_error": 0.0,
                "reference_positive_absolute_error": 0.0, "day_positive_absolute_error": 0.0}
            if candidate is not None:
                for day, actual in observations:
                    if not end - dt.timedelta(days=SELECTION_DAYS - 1) <= day <= end:
                        continue
                    previous_end = day - dt.timedelta(days=horizon)
                    proposed = self._candidate(day, previous_end)
                    if not proposed["fitted"]:
                        continue
                    before = self._base(previous_end)[1][uid]["planning_average_per_day"]
                    after = proposed["plans"][uid]["prediction"]
                    stats["scored_person_days"] += 1
                    stats["positive_dates"] += int(actual > 0)
                    for label, value in (("reference", before), ("day", after)):
                        error = value - actual
                        stats[label + "_absolute_error"] += abs(error)
                        stats[label + "_squared_error"] += error * error
                        if actual > 0:
                            stats[label + "_positive_absolute_error"] += abs(error)
            eligible = (stats["scored_person_days"] >= MIN_SELECTION_OBSERVATIONS
                        and stats["positive_dates"] >= MIN_SELECTION_POSITIVE_DATES)
            better = (stats["day_absolute_error"] < stats["reference_absolute_error"] - 1e-10
                      and stats["day_squared_error"] <= stats["reference_squared_error"] + 1e-10
                      and stats["day_positive_absolute_error"] <= stats["reference_positive_absolute_error"] + 1e-10)
            selected = candidate is not None and candidate["fitted"] and eligible and better
            stats.update(selected_model=POLICY if selected else control.POLICY,
                reason="day model improved earlier personal and positive-date error checks" if selected else
                "v5 retained: insufficient complete cache" if not complete else
                "v5 retained: day model lacked matched evidence or did not improve every personal error check")
            plan = dict(fallback[uid])
            if selected:
                proposed = candidate["plans"][uid]
                value = proposed["prediction"]
                plan.update(planning_average_per_day=value, estimated_capacity_per_day=value,
                    uncalibrated_forecast_per_day=proposed["raw_prediction"], calibration_factor=candidate["factor"],
                    calibration_available=candidate["calibration_available"], safe_plan_per_day=BUFFER_FRACTION * value,
                    model_probability_any_dd=None, expected_dd_given_positive=None,
                    estimate_origin="guarded_day_specific_regression", provisional=False)
            plan.update(policy=POLICY, component_selected_model=fallback[uid].get("selected_model"),
                selected_model=stats["selected_model"], day_selection=stats, work_date=str(target), history_through=str(end),
                history_lag_calendar_days=horizon, maximum_capacity=None,
                forecast_validation="explicit day-specific trial; inspected development dates, mixed teams, future accuracy unvalidated")
            choices[str(uid)] = stats
            plans[str(uid)] = plan
        return {"policy": POLICY, "business": "veo", "history_through": str(end), "work_date": str(target),
            "history_lag_calendar_days": horizon, "capacity_per_day": TRIAL_DEFAULT,
            "provisional": True, "source": "guarded_day_specific_trial", "control_default": default,
            "person_plans": plans, "person_choices": choices, "cache_complete": complete,
            "required_history_days": required, "fit": {k:v for k,v in candidate.items() if k != "plans"} if candidate else None,
            "excluded_user_ids": sorted(EXCLUDED_USER_IDS),
            "validation": "small development gains; positive outcomes checked separately, no unseen validation"}

    def baseline(self, members, end=None):
        if end is None:
            raise ValueError("A completed history-through date is required")
        return self.baseline_for_date(members, end, end + dt.timedelta(days=1))

    @staticmethod
    def apply(member, default):
        return apply(member, default)


def for_replay(members):
    return _Replay(members)


def baseline_for_date(members, end, target):
    return for_replay(members).baseline_for_date(members, end, target)


def baseline(members, end=None):
    return for_replay(members).baseline(members, end)


def planning(member, end):
    return apply(member, baseline([member], end))["planning"]


def apply(member, default):
    plan = default.get("person_plans", {}).get(str(member.get("user_id")))
    if plan is None:
        fallback = control.apply(member, default["control_default"])
        plan = {**fallback["planning"], "policy": POLICY, "selected_model": control.POLICY,
            "work_date": default["work_date"], "history_through": default["history_through"],
            "history_lag_calendar_days": default["history_lag_calendar_days"],
            "day_selection": {"selected_model": control.POLICY, "scored_person_days": 0,
                "reason": "v5 retained: no matched personal day-model comparisons"}}
    metadata = {key:value for key,value in (member.get("planning") or {}).items()
        if key in ("historical_daily_peak", "historical_peak_date", "daily_capacity_change")}
    return {**member, "planning": {**plan, **metadata}}


DEFINITION = (
    "Explicit private day-specific VEO trial. Fit a ridge correction to v5 on 14 earlier completed "
    "label dates, 14-day training half-life, penalty 20 and at least 100 Active person-days. Inputs are "
    "lagged personal rate/trend, positive frequency, previous Active outcome, gaps/recency, the known "
    "target weekday and other members' earlier rate. Training features use the same history lag as "
    "the forecast. Correct past raw optimism over seven earlier matched dates, needing three dates "
    "and 20 person-days. Only use this person's new estimate after five earlier fitted comparisons, "
    "including two positive dates, have lower absolute error, no higher squared error and no higher "
    "positive-date absolute error than v5. Otherwise retain v5 and its cold-start/provisional labels. "
    "Every label, feature, correction and selection is cut off at the available history date. Target "
    "weekday is known ahead of time; target sales/attendance are never a feature. Active zeros count; "
    "Unknown/Inactive are excluded; Melo is eligible. A complete cache is required; 128 dates supports normal "
    "one/two-day history lags. All dates were inspected in development. Future accuracy and production "
    "capacity remain unvalidated; no client demand, workload allocation or roster is invented."
)

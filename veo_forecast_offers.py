"""Private delivered-offer trial, with personal and recent cohort checks.

Uses only dated delivery events. Final offer outcomes are neither features nor
filters. Source coverage, scope and immutable per-run caches are explicit.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

import veo_forecast_day as control

POLICY = "veo-person-offer-trial-v7-all-members-v2"
HISTORY_DAYS = 192
WINDOW_DAYS = 28
TRIAL_DEFAULT = .2
BUFFER_FRACTION = .8
EXCLUDED_USER_IDS = control.EXCLUDED_USER_IDS
excluded = control.excluded
PHT = dt.timezone(dt.timedelta(hours=8))
FEATURE_NAMES = control.FEATURE_NAMES + ("delivered_offers_1d", "delivered_offers_3d",
                                        "delivered_offers_7d", "offer_history_available")


class OfferHistory:
    """Retain minimal delivery facts; deliberately discard every outcome label."""

    def __init__(self, sources, expected_chat_ids):
        self.groups = {}
        seen, events = set(), []
        for source in sources:
            if source.get("ok") is False:
                raise ValueError("Worker offer source failed")
            data = source.get("activity", source)
            chat = int(data["chat_id"])
            start = dt.date.fromisoformat(data["start_date"])
            # Worker end_date includes its incomplete today; only earlier whole
            # dates count as covered for these completed-day forecasts.
            end = dt.date.fromisoformat(data["end_date"]) - dt.timedelta(days=1)
            coverage = start, end
            if start > end:
                raise ValueError("Worker offer source has no completed dates")
            if chat in self.groups and self.groups[chat] != coverage:
                raise ValueError("Conflicting worker-source coverage for the same group")
            self.groups[chat] = coverage
            for row in data.get("client_performance_evidence", []):
                if row.get("kind") != "offer" or row.get("user_id") is None:
                    continue
                uid = int(row["user_id"])
                if uid in EXCLUDED_USER_IDS:
                    continue
                timestamp = dt.datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("Offer delivery timestamp must include a timezone")
                day = timestamp.astimezone(PHT).date()
                key = chat, row.get("assignment_id"), row.get("message_id"), timestamp.isoformat(), uid
                if key not in seen and start <= day <= end:
                    seen.add(key)
                    events.append((chat, uid, day))
        wanted = frozenset(int(chat) for chat in expected_chat_ids)
        missing = wanted.difference(self.groups)
        if missing:
            raise ValueError("Missing worker offer sources for requested groups: " + ", ".join(map(str, sorted(missing))))
        if not wanted:
            raise ValueError("At least one explicit offer-source group is required")
        self.scope = wanted
        self.start = max(self.groups[chat][0] for chat in wanted)
        self.end = min(self.groups[chat][1] for chat in wanted)
        self.events = tuple(sorted((uid, day) for chat, uid, day in events if chat in wanted))
        self.by_user = defaultdict(tuple)
        for uid in {uid for uid, _ in self.events}:
            self.by_user[uid] = tuple(day for user, day in self.events if user == uid)

    def features(self, uid, end):
        available = self.start <= end - dt.timedelta(days=6) and end <= self.end
        if not available:
            return (0.0, 0.0, 0.0, 0.0)
        days = self.by_user.get(uid, ())
        counts = [sum(end - dt.timedelta(days=n - 1) <= day <= end for day in days) for n in (1, 3, 7)]
        return tuple(math.log1p(count) / 3 for count in counts) + (1.0,)

    def metadata(self):
        return {"chat_ids": sorted(self.scope), "coverage_start": str(self.start), "coverage_end": str(self.end),
            "accepted_delivery_events": len(self.events), "final_offer_outcomes_used": False,
            "limitations": "current archived delivery facts, not an immutable historical as-of snapshot; source gaps may remain"}


def _fit(samples):
    n = len(FEATURE_NAMES)
    matrix = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n
    for features, actual, base, weight in samples:
        for j in range(n):
            rhs[j] += features[j] * (actual - base) * weight
            for k in range(n):
                matrix[j][k] += features[j] * features[k] * weight
    for j in range(n):
        matrix[j][j] += control.RIDGE_STRENGTH
    return control._solve(matrix, rhs)


class _Replay(control._Replay):
    POLICY = POLICY

    def __init__(self, members, offer_history):
        super().__init__(members)
        self.offers = offer_history
        self.day = control.for_replay(self.people)
        # Share only this run's immutable v5 cohort calculations. Offer features
        # and v6/day predictions have distinct caches.
        self.day.base = self.base
        self.day.base_cache = self.base_cache
        self.fallback_cache, self.first_choice_cache = {}, {}

    def _features(self, uid, target, end):
        values, prediction = super()._features(uid, target, end)
        return values + self.offers.features(uid, end), prediction

    def _raw(self, target, end):
        key = target, end
        if key in self.raw_cache:
            return self.raw_cache[key]
        horizon = (target - end).days
        samples = []
        for day, uid, actual in self.records:
            if end - dt.timedelta(days=control.TRAINING_DAYS - 1) <= day <= end:
                features, previous = self._features(uid, day, day - dt.timedelta(days=horizon))
                weight = 2 ** (-(end - day).days / control.TRAINING_HALF_LIFE_DAYS)
                samples.append((features, actual, previous, weight))
        fitted = len(samples) >= control.MIN_TRAINING_PERSON_DAYS
        coefficients = _fit(samples) if fitted else [0.0] * len(FEATURE_NAMES)
        plans = {}
        for uid in self.observations:
            features, previous = self._features(uid, target, end)
            value = max(0.0, previous + sum(a * b for a, b in zip(coefficients, features))) if fitted else previous
            plans[uid] = {"raw_prediction": value, "training_person_days": len(samples), "fitted": fitted}
        result = {"plans": plans, "fitted": fitted, "training_person_days": len(samples),
            "coefficients": dict(zip(FEATURE_NAMES, coefficients)),
            "training_window_start": str(end - dt.timedelta(days=control.TRAINING_DAYS - 1)), "training_window_end": str(end)}
        self.raw_cache[key] = result
        return result

    def _fallback(self, target, end):
        key = target, end
        if key not in self.fallback_cache:
            default = self.day.baseline_for_date(self.people, end, target)
            self.fallback_cache[key] = (default, {p["user_id"]: control.apply(p, default)["planning"] for p in self.people})
        return self.fallback_cache[key]

    def _first_choice(self, target, end):
        key = target, end
        if key in self.first_choice_cache:
            return self.first_choice_cache[key]
        horizon = (target - end).days
        _, fallback = self._fallback(target, end)
        candidate = self._candidate(target, end)
        plans, choices = {}, {}
        for uid, observations in self.observations.items():
            stats = {"scored_person_days": 0, "positive_dates": 0,
                "reference_absolute_error": 0.0, "reference_squared_error": 0.0,
                "offer_absolute_error": 0.0, "offer_squared_error": 0.0,
                "reference_positive_absolute_error": 0.0, "offer_positive_absolute_error": 0.0}
            for day, actual in observations:
                if not end - dt.timedelta(days=control.SELECTION_DAYS - 1) <= day <= end:
                    continue
                previous_end = day - dt.timedelta(days=horizon)
                proposed = self._candidate(day, previous_end)
                if not proposed["fitted"]:
                    continue
                previous = self._fallback(day, previous_end)[1][uid]["planning_average_per_day"]
                value = proposed["plans"][uid]["prediction"]
                stats["scored_person_days"] += 1
                stats["positive_dates"] += int(actual > 0)
                for label, prediction in (("reference", previous), ("offer", value)):
                    error = prediction - actual
                    stats[label + "_absolute_error"] += abs(error)
                    stats[label + "_squared_error"] += error * error
                    if actual > 0:
                        stats[label + "_positive_absolute_error"] += abs(error)
            eligible = (stats["scored_person_days"] >= control.MIN_SELECTION_OBSERVATIONS
                and stats["positive_dates"] >= control.MIN_SELECTION_POSITIVE_DATES)
            better = (stats["offer_absolute_error"] < stats["reference_absolute_error"] - 1e-10
                and stats["offer_squared_error"] <= stats["reference_squared_error"] + 1e-10
                and stats["offer_positive_absolute_error"] <= stats["reference_positive_absolute_error"] + 1e-10)
            use = eligible and better and candidate["fitted"] and bool(self.offers.features(uid, end)[-1])
            previous = fallback[uid]["planning_average_per_day"]
            plans[uid] = {"prediction": candidate["plans"][uid]["prediction"] if use else previous,
                          "reference": previous, "eligible": use}
            choices[uid] = stats
        result = {"plans": plans, "choices": choices, "candidate": candidate}
        self.first_choice_cache[key] = result
        return result

    def baseline_for_date(self, members, end, target):
        if target <= end:
            raise ValueError("Forecast target must be after the history-through date")
        horizon = (target - end).days
        default, fallback = self._fallback(target, end)
        required = control.HISTORY_DAYS + control.SELECTION_DAYS + control.CALIBRATION_DAYS + 4 * horizon
        dates = [r["date"] for p in self.people for r in p.get("daily", []) if r["date"] <= str(end)]
        complete = bool(dates) and (end - dt.date.fromisoformat(min(dates))).days + 1 >= required
        source_available = self.offers.start <= end - dt.timedelta(days=6) and end <= self.offers.end
        first = self._first_choice(target, end) if complete and source_available else None
        recent = []
        if first is not None:
            for day, uid, actual in self.records:
                if end - dt.timedelta(days=6) <= day <= end:
                    past = self._first_choice(day, day - dt.timedelta(days=horizon))["plans"][uid]
                    recent.append((day, uid, actual, past["prediction"], past["reference"]))
        def recent_stats(rows):
            stats = {"person_days": len(rows), "scored_dates": len({r[0] for r in rows}),
                "reference_absolute_error": sum(abs(r[4] - r[2]) for r in rows),
                "reference_squared_error": sum((r[4] - r[2]) ** 2 for r in rows),
                "offer_absolute_error": sum(abs(r[3] - r[2]) for r in rows),
                "offer_squared_error": sum((r[3] - r[2]) ** 2 for r in rows)}
            stats["no_worse"] = (stats["offer_absolute_error"] <= stats["reference_absolute_error"] + 1e-10
                and stats["offer_squared_error"] <= stats["reference_squared_error"] + 1e-10)
            return stats
        cohort = recent_stats(recent)
        cohort_ok = cohort["person_days"] >= 20 and cohort["scored_dates"] >= 3 and cohort["no_worse"]
        choices, plans = {}, {}
        for uid in self.observations:
            own = recent_stats([r for r in recent if r[1] == uid])
            selected = bool(first and first["plans"][uid]["eligible"] and len([r for r in recent if r[1] == uid]) >= 2
                            and own["no_worse"] and cohort_ok)
            stats = {**(first["choices"][uid] if first else {}), "window_start": str(end - dt.timedelta(days=27)),
                "window_end": str(end), "recent_window_start": str(end - dt.timedelta(days=6)),
                "recent_personal": own, "recent_cohort": cohort, "selected_model": POLICY if selected else control.POLICY,
                "reason": "offer model passed personal and recent error checks" if selected else
                "v6 retained: incomplete history cache" if not complete else
                "v6 retained: offer-source coverage unavailable" if not source_available else
                "v6 retained: offer model lacked evidence or failed an earlier error check"}
            plan = dict(fallback[uid])
            if selected:
                candidate = first["candidate"]
                value = first["plans"][uid]["prediction"]
                plan.update(planning_average_per_day=value, estimated_capacity_per_day=value,
                    uncalibrated_forecast_per_day=candidate["plans"][uid]["raw_prediction"],
                    calibration_factor=candidate["factor"], calibration_available=candidate["calibration_available"],
                    safe_plan_per_day=BUFFER_FRACTION * value, model_probability_any_dd=None,
                    expected_dd_given_positive=None, estimate_origin="guarded_delivered_offer_regression", provisional=False)
            plan.update(policy=POLICY, component_selected_model=fallback[uid].get("selected_model"),
                selected_model=stats["selected_model"], offer_selection=stats, work_date=str(target),
                history_through=str(end), history_lag_calendar_days=horizon, maximum_capacity=None,
                forecast_validation="private offer trial; inspected development dates, small mixed gains, future accuracy unvalidated")
            choices[str(uid)], plans[str(uid)] = stats, plan
        return {"policy": POLICY, "business": "veo", "history_through": str(end), "work_date": str(target),
            "history_lag_calendar_days": horizon, "capacity_per_day": TRIAL_DEFAULT, "source": "guarded_offer_trial",
            "control_default": default, "person_plans": plans, "person_choices": choices,
            "offer_sources": self.offers.metadata(), "source_available": source_available,
            "cache_complete": complete, "required_history_days": required,
            "fit": {k:v for k,v in first["candidate"].items() if k != "plans"} if first else None,
            "excluded_user_ids": sorted(EXCLUDED_USER_IDS), "provisional": True,
            "validation": "small development gains; some recent metrics worsen; not unseen validation"}

    @staticmethod
    def apply(member, default):
        return apply(member, default)


class _Engine:
    POLICY = POLICY
    EXCLUDED_USER_IDS = EXCLUDED_USER_IDS
    control = control
    __file__ = __file__

    def __init__(self, sources, expected_chat_ids):
        self.offers = OfferHistory(sources, expected_chat_ids)

    @staticmethod
    def excluded(member):
        return excluded(member)

    def for_replay(self, members):
        return _Replay(members, self.offers)

    def baseline_for_date(self, members, end, target):
        return self.for_replay(members).baseline_for_date(members, end, target)

    def baseline(self, members, end=None):
        return self.for_replay(members).baseline(members, end)

    @staticmethod
    def apply(member, default):
        return apply(member, default)


def with_sources(sources, expected_chat_ids):
    return _Engine(sources, expected_chat_ids)


def apply(member, default):
    plan = default.get("person_plans", {}).get(str(member.get("user_id")))
    if plan is None:
        result = control.apply(member, default["control_default"])
        plan = {**result["planning"], "policy": POLICY, "selected_model": control.POLICY,
            "offer_selection": {"scored_person_days": 0, "selected_model": control.POLICY,
                "reason": "v6 retained: no matched personal offer comparisons"}}
    metadata = {k:v for k,v in (member.get("planning") or {}).items()
                if k in ("historical_daily_peak", "historical_peak_date", "daily_capacity_change")}
    return {**member, "planning": {**plan, **metadata}}


DEFINITION = (
    "Explicit private delivered-offer VEO trial. Add prior 1/3/7-completed-date delivered-offer counts "
    "and a source-coverage flag to v6's causal day features, log1p-scaled by 3. Fit a 14-date ridge "
    "correction to the v5 base, penalty 20, half-life 14 and at least 100 Active person-days. Final "
    "offer acceptance/missed/withdrawn labels never enter features or filtering. Each horizon's "
    "training, correction and selection use only earlier available facts. Compare the candidate "
    "with v6 on this person's prior 28 dates, requiring five fitted comparisons including two positive "
    "dates, lower absolute error, no higher squared error and no higher positive-date absolute error. "
    "Also require no worse absolute/squared errors in recent seven-date personal comparisons (two "
    "observations) and the recent matched cohort (20 person-days, three dates). Otherwise retain v6. "
    "Missing scope sources fail explicitly; incomplete/stale coverage or short caches retain v6. "
    "Active zeros count; Unknown/Inactive are excluded; Melo is eligible. Defaults/quotas/reports are unchanged. "
    "Small development gains are mixed and not independently validated; no production limit or "
    "exclusive page allocation is inferred. Current archived delivery facts are not immutable "
    "as-of snapshots. Future sales/demand/availability are unknown."
)
_Engine.DEFINITION = DEFINITION

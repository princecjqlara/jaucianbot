"""Explicit private personal model: v3 fallback plus guarded hurdle component.

Every choice uses only that person's earlier matched prediction errors. The
development gain is modest and team results remain mixed; no default is changed.
"""
from __future__ import annotations

import copy
import datetime as dt

import veo_forecast as control
import veo_forecast_hurdle as hurdle

POLICY = "veo-person-guarded-hurdle-trial-v5-all-members-v2"
SELECTION_DAYS = 28
MIN_SELECTION_OBSERVATIONS = 3
WINDOW_DAYS = hurdle.WINDOW_DAYS
HISTORY_DAYS = max(control.HISTORY_DAYS, hurdle.HISTORY_DAYS) + SELECTION_DAYS
MIN_ACTIVE_DAYS = control.MIN_ACTIVE_DAYS
TRIAL_DEFAULT = control.TRIAL_DEFAULT
BUFFER_FRACTION = control.BUFFER_FRACTION
EXCLUDED_USER_IDS = control.EXCLUDED_USER_IDS
excluded = control.excluded


class _Replay:
    """One immutable cohort's dated component cache; never global or persistent."""
    POLICY = POLICY

    def __init__(self, members):
        self.people = copy.deepcopy(list({int(m["user_id"]): m for m in members
            if m.get("user_id") is not None and not excluded(m)}.values()))
        self.defaults = {}
        self.plans = {}

    @staticmethod
    def excluded(member):
        return excluded(member)

    def _component(self, engine, end):
        key = engine.POLICY, end
        if key not in self.defaults:
            default = engine.baseline(self.people, end)
            self.defaults[key] = default
            self.plans[key] = {str(m["user_id"]): engine.apply(m, default)["planning"] for m in self.people}
        return self.defaults[key], self.plans[key]

    def baseline(self, members, end=None):
        if end is None:
            dates = [(m.get("planning") or {}).get("window_end") for m in self.people]
            end = dt.date.fromisoformat(max(d for d in dates if d)) if any(dates) else None
        if end is None:
            raise ValueError("A completed history-through date is required")
        old, _ = self._component(control, end)
        new, _ = self._component(hurdle, end)
        dates = [r["date"] for m in self.people for r in m.get("daily", []) if r["date"] <= str(end)]
        start = end - dt.timedelta(days=SELECTION_DAYS - 1)
        first = dt.date.fromisoformat(min(dates)) + dt.timedelta(days=HISTORY_DAYS - SELECTION_DAYS) if dates else end + dt.timedelta(days=1)
        choices = {}
        for member in self.people:
            uid = str(member["user_id"])
            stats = {"window_start": str(start), "window_end": str(end), "scored_person_days": 0,
                "minimum_observations": MIN_SELECTION_OBSERVATIONS,
                "reference_absolute_error": 0.0, "reference_squared_error": 0.0,
                "advanced_absolute_error": 0.0, "advanced_squared_error": 0.0}
            for row in sorted(member.get("daily", []), key=lambda r: r["date"]):
                if not max(start, first).isoformat() <= row["date"] <= str(end) or row["attendance"] != "Active":
                    continue
                prior = dt.date.fromisoformat(row["date"]) - dt.timedelta(days=1)
                for label, engine in (("reference", control), ("advanced", hurdle)):
                    _, plans = self._component(engine, prior)
                    error = plans[uid]["planning_average_per_day"] - row["done_deals"]
                    stats[label + "_absolute_error"] += abs(error)
                    stats[label + "_squared_error"] += error * error
                stats["scored_person_days"] += 1
            enough = stats["scored_person_days"] >= MIN_SELECTION_OBSERVATIONS
            better = (stats["advanced_absolute_error"] < stats["reference_absolute_error"] - 1e-10
                      and stats["advanced_squared_error"] <= stats["reference_squared_error"] + 1e-10)
            stats["selected_model"] = hurdle.POLICY if enough and better else control.POLICY
            stats["reason"] = ("advanced component improved both earlier personal errors" if enough and better else
                "v3 retained: advanced component did not improve both earlier personal errors" if enough else
                "v3 retained: insufficient earlier personal comparisons")
            choices[uid] = stats
        return {**old, "policy": POLICY, "selection_source": "personal_hurdle_guard",
            "components": {control.POLICY: old, hurdle.POLICY: new}, "person_choices": choices,
            "calibration": {"per_person": True, "factor": 1.0,
                "available": old["calibration"].get("available", False),
                "status": "each selected component retains its own correction"},
            "validation": "explicit local trial; modest development improvement, mixed teams, no unseen validation"}

    @staticmethod
    def apply(member, default):
        return apply(member, default)


def for_replay(members):
    return _Replay(members)


def baseline(members, end=None):
    return for_replay(members).baseline(members, end)


def planning(member, end):
    return apply(member, baseline([member], end))["planning"]


def apply(member, default):
    decision = default.get("person_choices", {}).get(str(member.get("user_id")), {})
    chosen = decision.get("selected_model", control.POLICY)
    engine = hurdle if chosen == hurdle.POLICY else control
    child = default.get("components", {}).get(chosen)
    if child is None:
        raise ValueError("Selected component baseline is required")
    plan = dict(member.get("planning") or {})
    if plan.get("policy") == POLICY and plan.get("selected_model") == chosen:
        plan["policy"] = chosen
        plan["selected_model"] = plan.get("component_selected_model")
    result = engine.apply({**member, "planning": plan}, child)
    result["planning"] = {**result["planning"], "policy": POLICY,
        "component_selected_model": result["planning"].get("selected_model"), "selected_model": chosen,
        "advanced_selection": decision,
        "forecast_validation": "guarded personal trial; small retrospective gains, mixed teams, future validation pending"}
    return result


DEFINITION = (
    "Explicit private personal forecasting trial: v3 personal choice is the fallback; a 28-date "
    "occurrence/size hurdle component is used only when that person's own prior 28 completed dates "
    "contain at least 3 matched predictions with lower absolute error and no higher squared error. "
    "Every compared forecast, correction, peer starting point and model choice excludes its own "
    "target/future outcome. Require 63 cached dates for full replay. No other person's improved errors "
    "justify this person's switch. Components keep separate prior-date corrections and provisional "
    "labels. Active zeros count; Unknown/Inactive do not train or score. Melo is eligible. Recorded sales "
    "expectations and trial probabilities are not verified production capacity. Modest development "
    "gains do not prove future accuracy; teams and individual people can worsen. V3 remains the "
    "organized-report default; this trial requires explicit offline selection."
)

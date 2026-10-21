"""Separate, explicitly provisional VEO and Suno planning baselines."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from fractions import Fraction

import recent_capacity

POLICY = "business-median-prior-v1"
SEEDS = {"veo": 4 / 3, "suno": 2.0}
MIN_PEERS = 5


def unique_members(members: list[dict], end: dt.date, *, combine_days: bool = False) -> list[dict]:
    groups = defaultdict(list)
    for member in members:
        if member.get("user_id") is not None:
            groups[int(member["user_id"])].append(member)
    result = []
    for values in groups.values():
        member = dict(values[0])
        if combine_days and len(values) > 1 and all("daily" in m for m in values):
            days = defaultdict(list)
            for m in values:
                for day in m["daily"]:
                    days[day["date"]].append(day)
            member["daily"] = [{"date": day, "done_deals": sum(r["done_deals"] for r in rows),
                "attendance": "Active" if any(r["attendance"] == "Active" for r in rows) else
                              "Inactive" if len(rows) == len(values) and all(r["attendance"] == "Inactive" for r in rows) else "Unknown"}
                for day, rows in sorted(days.items())]
            member["planning"] = recent_capacity.planning(member, end)
        result.append(member)
    return result


def baseline(business: str, members: list[dict]) -> dict:
    values = [(m.get("planning") or {}).get("estimated_capacity_per_day") for m in members]
    values = [v for v in values if v is not None]
    enough = len(values) >= MIN_PEERS
    rate = statistics.median(values) if enough else SEEDS[business]
    return {"policy": POLICY, "business": business, "capacity_per_day": rate,
        "peer_members": len(values), "source": "current_peer_median" if enough else "initial_business_seed",
        "unit": "Done Deals/day" if business == "veo" else "receipt posts/day"}


def apply(member: dict, default: dict) -> dict:
    plan = dict(member.get("planning") or {})
    if plan.get("estimated_capacity_per_day") is not None or (
            "estimated_capacity_per_day" not in plan and plan.get("planning_average_per_day") is not None):
        return {**member, "planning": {**plan, "provisional": False, "estimate_origin": "personal_history"}}
    # Output/attendance/peak evidence stays untouched. A prior is never recorded as actual output.
    top = plan.get("top_three_daily_outputs", [])[:2]
    prior = Fraction(default["capacity_per_day"]).limit_denominator(1_000_000)
    blend = (sum(top) + (3 - len(top)) * prior) / 3
    value = min(prior, blend)  # One unusually high date cannot raise a new member above the default.
    plan.update(planning_average_per_day=float(value), safe_plan_per_day=int(value * 4 / 5),
        provisional=True, default_policy=POLICY, default_capacity_per_day=float(prior),
        default_peer_members=default["peer_members"], personal_output_dates=len(top),
        estimate_origin="blended_default" if top else "business_default")
    return {**member, "planning": plan}


def enrich(capacities: dict, teams: set[int]) -> dict:
    from daily_automation import GROUPS, SUNO_GROUPS
    end = dt.date.fromisoformat(capacities["date"])
    businesses, defaults = {}, {}
    for business, ids in (("veo", set(GROUPS)), ("suno", set(SUNO_GROUPS))):
        members = [m for chat in sorted(teams.intersection(ids)) for m in capacities["members"].get(str(chat), [])]
        people = unique_members(members, end, combine_days=business == "suno")
        default = baseline(business, people)
        defaults[business] = default
        businesses[business] = [apply(m, default) for m in people]
    result = {**capacities, "defaults": defaults, "businesses": businesses, "members": {}}
    lookup = {b: {m["user_id"]: m for m in people} for b, people in businesses.items()}
    for chat, members in capacities["members"].items():
        business = "veo" if int(chat) in GROUPS else "suno"
        result["members"][chat] = [lookup[business][m["user_id"]] for m in members]
    return result

"""Separate, explicitly provisional VEO and Suno planning baselines."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

import recent_capacity
import veo_forecast

POLICY = "suno-established-active-default-v6"
SEEDS = {"veo": veo_forecast.TRIAL_DEFAULT}
MIN_PEERS = 5
MIN_ACTIVE_DAYS = 3
MIN_OUTPUT_DAYS = 3
SEED_REVIEW = {"start_date": "2026-10-08", "end_date": "2026-10-21",
               "peer_members": {"veo": 28}}


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
    if business == "veo":
        return veo_forecast.baseline(members)
    if business == "suno":
        # Established peers' actual Active-day rates include recorded zeros.
        # This supplies a provisional starting plan, not a personal limit.
        people = {int(m["user_id"]): m for m in members if m.get("user_id") is not None}
        plans = [m.get("planning") or {} for m in people.values()]
        observed = [p["active_day_average"] for p in plans
            if p.get("known_active_days", 0) > 0 and p.get("active_day_average") is not None]
        values = [p["active_day_average"] for p in plans
            if p.get("known_active_days", 0) >= MIN_ACTIVE_DAYS
            and p.get("eligible_output_days", 0) >= MIN_OUTPUT_DAYS
            and p.get("active_day_average") is not None
            and p.get("estimated_capacity_per_day") is not None and not p.get("provisional")]
        rate = statistics.median(values) if values else None
        return {"policy": POLICY, "business": business, "capacity_per_day": rate,
            "peer_reference_per_day": statistics.median(observed) if observed else None,
            "reference_peer_members": len(observed), "peer_members": len(values),
            "minimum_active_days": MIN_ACTIVE_DAYS, "minimum_output_days": MIN_OUTPUT_DAYS,
            "source": "established_peer_active_day_median", "unit": "receipt posts/day"}
    raise ValueError(f"Unknown capacity business: {business}")


def peer_label(default: dict) -> str:
    if default.get("source") == "provisional_trial_default":
        return "0.20 trial default; peer median is descriptive only"
    if default.get("source") == "reviewed_peer_fallback":
        review = default.get("reviewed_fallback", SEED_REVIEW)
        return f"{review['peer_members'][default['business']]} reviewed peers"
    return f"{default['peer_members']} current peers"


def apply(member: dict, default: dict) -> dict:
    if default.get("business") == "veo":
        if default.get("live_forecast"):
            import veo_forecast_offers
            import veo_forecast_day
            engine = veo_forecast_offers if default["policy"] == veo_forecast_offers.POLICY else veo_forecast_day
            return engine.apply(member, default)
        return veo_forecast.apply(member, default)
    plan = dict(member.get("planning") or {})
    if plan.get("estimated_capacity_per_day") is not None or (
            "estimated_capacity_per_day" not in plan and plan.get("planning_average_per_day") is not None
            and not plan.get("provisional")):
        return {**member, "planning": {**plan, "provisional": False, "estimate_origin": "personal_history"}}
    # Output/attendance/peak evidence stays untouched. A prior is never recorded as actual output.
    top = plan.get("top_three_daily_outputs", [])[:2]
    if default.get("business") == "suno":
        value = default.get("capacity_per_day")
        plan.update(estimated_capacity_per_day=None, planning_average_per_day=value, safe_plan_per_day=None,
            provisional=value is not None, default_policy=POLICY, default_capacity_per_day=value,
            buffer_fraction=None,
            default_peer_members=default["peer_members"], personal_output_dates=len(top),
            estimate_origin="historical_peer_default" if value is not None else "insufficient_personal_history")
        return {**member, "planning": plan}
    raise ValueError(f"Unknown capacity business: {default.get('business')}")


def enrich(capacities: dict, teams: set[int]) -> dict:
    from daily_automation import GROUPS, SUNO_GROUPS
    end = dt.date.fromisoformat(capacities["date"])
    businesses, defaults = {}, {}
    for business, ids in (("veo", set(GROUPS)), ("suno", set(SUNO_GROUPS))):
        members = [m for chat in sorted(teams.intersection(ids)) for m in capacities["members"].get(str(chat), [])]
        people = unique_members(members, end, combine_days=business == "suno")
        default = veo_forecast.baseline(people, end) if business == "veo" else baseline(business, people)
        defaults[business] = default
        businesses[business] = [apply(m, default) for m in people]
    result = {**capacities, "defaults": defaults, "businesses": businesses, "members": {},
        "team_references": {str(chat): baseline("suno", capacities["members"].get(str(chat), []))
            for chat in teams.intersection(SUNO_GROUPS)}}
    lookup = {b: {m["user_id"]: m for m in people} for b, people in businesses.items()}
    for chat, members in capacities["members"].items():
        business = "veo" if int(chat) in GROUPS else "suno"
        result["members"][chat] = [lookup[business][m["user_id"]] for m in members]
    return result

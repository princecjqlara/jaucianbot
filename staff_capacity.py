"""Separate demonstrated selling-day output from expected sales at recent demand.

Paid-sale posts alone do not identify physical production limits or ad exposure.
Only explicit per-person sufficient-work records identify supplied-work dates.
"""
from __future__ import annotations

import datetime as dt
import statistics

POLICY = "workload-aware-output-v2"
WINDOW_DAYS = 56
MIN_DATES = 3
RECENT_WINDOWS = (14, 28, 56)


def personal_reference(positive: list[dict], end: dt.date) -> dict:
    """Describe a repeatedly demonstrated level, never forecast sales from it."""
    ranked = sorted((d["done_deals"] for d in positive), reverse=True)
    level = ranked[MIN_DATES - 1] if len(ranked) >= MIN_DATES else None
    supported = [d for d in positive if level is not None and d["done_deals"] >= level]
    recent, window = [], None
    for days in RECENT_WINDOWS:
        selected = [d for d in positive if d["date"] >= str(end - dt.timedelta(days=days - 1))]
        if len(selected) >= MIN_DATES:
            recent, window = selected, days
            break
    last = max((d["date"] for d in supported), default=None)
    age = (end - dt.date.fromisoformat(last)).days if last else None
    return {"repeatable_output_per_day": level, "repeatable_support_days": len(supported),
        "repeatable_output_evidence": [{"date": d["date"], "recorded_output": d["done_deals"]}
            for d in sorted(supported, key=lambda d: d["date"])],
        "repeatable_last_met_date": last, "repeatable_age_days": age,
        "reference_status": "unknown" if level is None else "older evidence" if age > 14 else
            "limited evidence" if len(positive) < 5 else "repeated output observed",
        "recent_selling_day_average": statistics.mean(d["done_deals"] for d in recent) if recent else None,
        "recent_selling_days": len(recent), "recent_selling_window_days": window,
        "reference_kind": "highest recorded level met on at least three distinct Active dates",
        "reference_is_sales_forecast": False}


def measure(member: dict, end: dt.date, records=(), source_chats=()) -> dict:
    start = end - dt.timedelta(days=WINDOW_DAYS - 1)
    uid = member.get("user_id")
    contexts = {(int(r["chat_id"]), r["work_date"], r.get("user_id")): r for r in records
        if r.get("actual") is True and r.get("source") == "manager_record"}
    days = {r["date"]: r for r in member.get("daily", []) if str(start) <= r["date"] <= str(end)}
    active = [r for r in days.values() if r.get("attendance") == "Active"]
    positive = [r for r in active if r["done_deals"] > 0]
    supplied, no_work, limited, unknown_zero, ads_paused = [], 0, 0, 0, 0
    for day in active:
        personal = [contexts.get((chat, day["date"], uid), {}) for chat in source_chats]
        effective = [p or contexts.get((chat, day["date"], None), {})
            for chat, p in zip(source_chats, personal)]
        if any(p.get("workload") == "sufficient" for p in personal):
            supplied.append(day)
        elif effective and all(p.get("workload") == "none" for p in effective):
            no_work += 1
        elif effective and all(p.get("workload") in {"none", "limited"} for p in effective):
            limited += 1
        elif day["done_deals"] == 0:
            unknown_zero += 1
        # Ads off does not establish no work: organic/repeat/backlog clients can remain.
        if effective and all(p.get("ads") == "paused" for p in effective):
            ads_paused += 1
    return {"policy": POLICY, "window_start": str(start), "window_end": str(end), "minimum_dates": MIN_DATES,
        **personal_reference(positive, end),
        "active_days": len(active), "selling_days": len(positive),
        "selling_day_median": statistics.median(r["done_deals"] for r in positive) if len(positive) >= MIN_DATES else None,
        "selling_day_mean": statistics.mean(r["done_deals"] for r in positive) if positive else None,
        "supplied_work_days": len(supplied),
        "supplied_work_output_per_day": statistics.mean(r["done_deals"] for r in supplied) if len(supplied) >= MIN_DATES else None,
        "supplied_work_evidence": [{"date": r["date"], "recorded_output": r["done_deals"]} for r in sorted(supplied, key=lambda r: r["date"])],
        "no_work_days": no_work, "limited_work_days": limited, "unknown_work_zero_days": unknown_zero,
        "ads_paused_days": ads_paused, "maximum_capacity": None,
        "basis": "paid-sale output; supplied-work measure and selling-day reference are separate from expected sales",
        "limitations": "Repeated output demonstrates an observed level, not expected daily sales or maximum production. Recent averages condition on selling dates; workload and ad delivery remain separate evidence."}


def enrich(capacities: dict, end: dt.date, records=(), source_status="available") -> dict:
    from daily_automation import GROUPS
    team_names = {c["name"]: chat for chat, c in GROUPS.items()}
    scope = {}
    for chat, people in capacities["members"].items():
        if int(chat) in GROUPS:
            for person in people:
                scope.setdefault(int(person["user_id"]), set()).add(int(chat))
    people = []
    for member in capacities.get("businesses", {}).get("veo", []):
        chats = {team_names[name] for name in member.get("teams", []) if name in team_names}
        chats.update(scope.get(int(member["user_id"]), set()))
        metric = measure(member, end, records, sorted(chats))
        people.append({**member, "workload_capacity": {**metric, "context_source_status": source_status}})
    lookup = {int(p["user_id"]): p for p in people}
    return {**capacities, "businesses": {**capacities.get("businesses", {}), "veo": people},
        "members": {chat: [lookup.get(int(p["user_id"]), p) for p in members] if int(chat) in GROUPS else members
            for chat, members in capacities["members"].items()}}


def aggregate(people: list[dict], *, tracked=True) -> dict:
    unique = {int(p["user_id"]): p.get("workload_capacity", {}) for p in people}
    result = {"active_members": len(unique) if tracked else None, "policy": POLICY}
    for label, field in (("supplied_work", "supplied_work_output_per_day"),
                        ("repeatable_output", "repeatable_output_per_day"),
                        ("recent_selling", "recent_selling_day_average"),
                        ("selling_day", "selling_day_median")):
        known = [p[field] for p in unique.values() if p.get(field) is not None]
        result[label] = {"known_members": len(known), "unknown_members": len(unique) - len(known),
            "known_total": sum(known) if tracked and (known or not unique) else None,
            "average_per_known_member": statistics.mean(known) if known and tracked else None,
            "total": sum(known) if tracked and len(known) == len(unique) else None}
    result["context_unavailable"] = any(p.get("context_source_status") == "unavailable" for p in unique.values())
    result["older_repeatable_references"] = sum(p.get("reference_status") == "older evidence" for p in unique.values())
    return result


def render(summary: dict, unit="DD/day") -> list[str]:
    n = summary.get("active_members")
    if n is None:
        return ["Workload/output reference: availability pending."]
    if n == 0:
        return ["No confirmed Active page members; no workload/output reference to sum."]
    lines = []
    for key, label in (("repeatable_output", "Demonstrated repeatable output"),
                       ("recent_selling", "Recent selling-day average"),
                       ("supplied_work", "With confirmed sufficient work")):
        data = summary[key]
        count = data["known_members"]
        if count:
            partial = " (partial; supported members only)" if count < n else ""
            lines.append(f"{label}: {data['known_total']:.2f} {unit}{partial}; {count}/{n} members; avg {data['average_per_known_member']:.2f}.")
        elif key == "supplied_work":
            lines.append("Capacity with sufficient work: unmeasured; need 3 confirmed supplied-work Active dates per person.")
        elif key == "repeatable_output":
            lines.append("Repeatable output: pending 3 positive Active dates per person.")
    if summary.get("older_repeatable_references"):
        lines.append(f"Older repeatable-output evidence: {summary['older_repeatable_references']} member(s); last support more than 14 days ago.")
    if summary.get("context_unavailable"):
        lines.append("Ads/workload records unavailable; capacity remains unmeasured.")
    return lines

"""Separate demonstrated selling-day output from expected sales at recent demand.

Paid-sale posts alone do not identify physical production limits or ad exposure.
Only explicit per-person sufficient-work records identify supplied-work dates.
"""
from __future__ import annotations

import datetime as dt
import statistics

POLICY = "workload-aware-output-v1"
WINDOW_DAYS = 56
MIN_DATES = 3


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
        "active_days": len(active), "selling_days": len(positive),
        "selling_day_median": statistics.median(r["done_deals"] for r in positive) if len(positive) >= MIN_DATES else None,
        "selling_day_mean": statistics.mean(r["done_deals"] for r in positive) if positive else None,
        "supplied_work_days": len(supplied),
        "supplied_work_output_per_day": statistics.mean(r["done_deals"] for r in supplied) if len(supplied) >= MIN_DATES else None,
        "supplied_work_evidence": [{"date": r["date"], "recorded_output": r["done_deals"]} for r in sorted(supplied, key=lambda r: r["date"])],
        "no_work_days": no_work, "limited_work_days": limited, "unknown_work_zero_days": unknown_zero,
        "ads_paused_days": ads_paused, "maximum_capacity": None,
        "basis": "paid-sale output; supplied-work measure and selling-day reference are separate from expected sales",
        "limitations": "Selling-day median conditions on positive output. It is not expected sales, an ad-adjusted prediction, or a verified production maximum."}


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
    for label, field in (("supplied_work", "supplied_work_output_per_day"), ("selling_day", "selling_day_median")):
        known = [p[field] for p in unique.values() if p.get(field) is not None]
        result[label] = {"known_members": len(known), "unknown_members": len(unique) - len(known),
            "known_total": sum(known) if tracked and (known or not unique) else None,
            "average_per_known_member": statistics.mean(known) if known and tracked else None,
            "total": sum(known) if tracked and len(known) == len(unique) else None}
    result["context_unavailable"] = any(p.get("context_source_status") == "unavailable" for p in unique.values())
    return result


def render(summary: dict, unit="DD/day") -> list[str]:
    n = summary.get("active_members")
    if n is None:
        return ["Workload/output reference: availability pending."]
    if n == 0:
        return ["No confirmed Active page members; no workload/output reference to sum."]
    lines = []
    for key, label in (("supplied_work", "With confirmed sufficient work"), ("selling_day", "Selling-day reference")):
        data = summary[key]
        count = data["known_members"]
        if count:
            partial = " (partial; supported members only)" if count < n else ""
            lines.append(f"{label}: {data['known_total']:.2f} {unit}{partial}; {count}/{n} members; avg {data['average_per_known_member']:.2f}.")
        elif key == "supplied_work":
            lines.append("Capacity with sufficient work: unmeasured; need 3 confirmed supplied-work Active dates per person.")
        else:
            lines.append("Selling-day reference: pending 3 positive Active dates per person.")
    if summary.get("context_unavailable"):
        lines.append("Ads/workload records unavailable; capacity remains unmeasured.")
    return lines

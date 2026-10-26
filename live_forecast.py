"""Daily/cohort cache of the guarded personal forecast for organized reports.

No delivery. Forecasts use completed dates and an explicit upcoming work date;
saved plan dictionaries keep recurring staffing refreshes from refitting models.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.parse
from pathlib import Path

import veo_forecast_offers as engine
import veo_forecast_day as day_engine
from cloud_store import new_client_actions, request
from daily_automation import DAILY_REPORTS_CHAT_ID, GROUPS, MANILA
from member_performance import build_client_performance

VERSION = 1


def cache_key(people, end, target, teams):
    modules = [engine]
    for module in modules:
        for name in ("control", "hurdle", "reference", "recent", "recent_capacity"):
            dependency = getattr(module, name, None)
            if dependency is not None and dependency not in modules:
                modules.append(dependency)
    signature = {"history_through": str(end), "target": str(target), "teams": sorted(teams),
        "members": [{"user_id": int(p["user_id"]), "daily": p.get("daily", [])}
            for p in sorted(people, key=lambda p: int(p["user_id"]))],
        "model_sources": {Path(m.__file__).name: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in modules}}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return f"live-personal-forecast:v{VERSION}:{target}:{digest}"


def offer_sources(teams, end, now):
    """Use the same delivered-offer evidence as worker reports, without sales reads."""
    until = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, MANILA)
    start = end - dt.timedelta(days=29)
    begin = dt.datetime.combine(start, dt.time.min, MANILA)
    history = new_client_actions(teams)
    result = []
    for chat in sorted(teams):
        rows = [r for r in history if int(r["chat_id"]) == chat]
        performance = build_client_performance(rows, [], GROUPS[chat], begin, until, now)
        result.append({"ok": True, "activity": {"chat_id": chat, "start_date": str(start),
            "end_date": str(end + dt.timedelta(days=1)), "client_performance_evidence": performance["evidence"]}})
    return result


def baseline(people, end, target, teams, now):
    if target <= end:
        raise ValueError("Live forecast target must follow completed history")
    if end >= now.astimezone(MANILA).date():
        raise ValueError("Live forecast history must use completed PHT dates")
    people = list({int(p["user_id"]): p for p in people if p.get("user_id") is not None}.values())
    teams = set(teams).intersection(GROUPS)
    key = cache_key(people, end, target, teams)
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "payload",
        "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}", "dedupe_key": f"eq.{key}", "limit": 1})) or []
    if rows:
        return rows[0]["payload"]["default"]
    try:
        source = offer_sources(teams, end, now)
        default = engine.with_sources(source, teams).baseline_for_date(people, end, target)
        source_status = "dated delivered-offer archive; final offer outcomes ignored"
    except Exception as error:
        # Source failure cannot invent zero offers or block otherwise due reports.
        print(f"Live forecast offer source unavailable: {type(error).__name__}")
        default = day_engine.baseline_for_date(people, end, target)
        source_status = "offer evidence unavailable; guarded day-model fallback"
    default.update(business="veo", live_forecast=True, prepared_at_utc=now.isoformat(),
        forecast_source_status=source_status)
    request("scheduled_actions?on_conflict=dedupe_key", {"chat_id": DAILY_REPORTS_CHAT_ID,
        "action_type": "message", "status": "cancelled", "scheduled_for": now.isoformat(),
        "dedupe_key": key, "payload": {"default": default}},
        prefer="resolution=ignore-duplicates,return=minimal")
    return default


def enrich(capacities, teams, end, target, now, *, scope_people=None, complete=True):
    people = capacities.get("businesses", {}).get("veo", [])
    if not people and complete:
        return capacities
    # A five-minute membership refresh must not replace an already prepared
    # same-date personal model with v3. Apply the saved plans only to currently
    # verified humans; stale members never re-enter displayed worker counts.
    key = "live-personal-current:v1:" + cache_key(scope_people if scope_people is not None else people,
        end, target, teams)
    if complete:
        default = baseline(people, end, target, teams, now)
        request("scheduled_actions?on_conflict=dedupe_key", {"chat_id": DAILY_REPORTS_CHAT_ID,
            "action_type": "message", "status": "cancelled", "scheduled_for": now.isoformat(),
            "updated_at": now.isoformat(), "dedupe_key": key, "payload": {"default": default}},
            prefer="resolution=merge-duplicates,return=minimal")
    else:
        rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "payload",
            "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}", "dedupe_key": f"eq.{key}", "limit": 1})) or []
        if not rows:
            # Cold start: do not fit a peer model from a partially checked cohort.
            default = {**capacities.get("defaults", {}).get("veo", {}),
                "forecast_source_status": "membership verification pending; guarded forecast not prepared yet",
                "membership_refresh_pending": True}
            return {**capacities, "defaults": {**capacities.get("defaults", {}), "veo": default}}
        default = {**rows[0]["payload"]["default"], "membership_refresh_pending": True}
    apply = engine.apply if default["policy"] == engine.POLICY else day_engine.apply
    enriched = [apply(p, default) for p in people]
    lookup = {int(p["user_id"]): p for p in enriched}
    result = {**capacities, "defaults": {**capacities.get("defaults", {}), "veo": default},
        "businesses": {**capacities.get("businesses", {}), "veo": enriched},
        "members": dict(capacities["members"])}
    for chat in set(teams).intersection(GROUPS):
        result["members"][str(chat)] = [lookup[int(p["user_id"])] for p in capacities["members"].get(str(chat), [])]
    return result

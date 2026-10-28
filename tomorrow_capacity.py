"""Manager-only tomorrow estimates from actual Active voters and rated history."""
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import math
import urllib.parse

import member_capacity
import member_capacity_reports as reports
import veo_forecast
import capacity_defaults
import staff_capacity
from automation_controls import chat_enabled
from cloud_store import daily_poll_counts, daily_poll_active_users, enqueue_scheduled_action, request, allowed_chat_ids
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID, MANILA

VERSION = 4


def definition(default: dict) -> str:
    if default.get("live_forecast"):
        import veo_forecast_day
        import veo_forecast_offers
        model = veo_forecast_offers if default["policy"] == veo_forecast_offers.POLICY else veo_forecast_day
        return model.DEFINITION.replace("Explicit private", "Guarded").replace(
            "Defaults/quotas/reports are unchanged.", "Reports use this estimate; assignment quotas are unchanged.")
    return veo_forecast.DEFINITION


def historical_members(now: dt.datetime, teams: set[int]) -> list[dict]:
    date = reports.latest_report_date(now)
    import report_topics
    if report_topics.enabled():
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(teams))) as pool:
            groups = list(pool.map(lambda chat: member_capacity.archive_report(chat,
                date - dt.timedelta(days=veo_forecast.HISTORY_DAYS - 1), date), sorted(teams)))
        return member_capacity.combined(groups)["members"]
    scope = hashlib.sha256(",".join(map(str, sorted(teams))).encode()).hexdigest()[:10]
    saved = reports._saved(f"daily-member-capacity:v{reports.VERSION}:{date}:{scope}:snapshot")
    if saved is not None:
        return saved["members"]
    return member_capacity.backread(teams, now, end=date)["combined"]["members"]


def build(work_date: dt.date, historical: list[dict], rosters: dict[int, list[dict] | None],
          *, history_end: dt.date | None = None, default_override: dict | None = None) -> dict:
    history_end = min(history_end or work_date - dt.timedelta(days=1), work_date - dt.timedelta(days=1))
    default = default_override or veo_forecast.baseline(historical, history_end)
    lookup = {int(m["user_id"]): capacity_defaults.apply({**m, "workload_capacity": m.get("workload_capacity")
        or staff_capacity.measure(m, history_end)}, default) for m in historical if m.get("user_id") is not None}
    groups, unique, membership = [], {}, {}

    def totals(members):
        rated = [m for m in members if m["average"] is not None]
        expected = sum(m["average"] for m in rated) if rated or not members else None
        conservative = sum(m["conservative"] for m in rated) if rated or not members else None
        return {"active_members": len(members), "rated_members": len(rated), "unknown_members": len(members) - len(rated),
            "personal_members": sum(not m["provisional"] for m in rated),
            "provisional_members": sum(m["provisional"] for m in rated),
            "average_per_rated_member": expected / len(rated) if rated else None,
            "average_per_all_active": expected / len(members) if members and len(rated) == len(members) else None,
            "recorded_output_total": expected, "conservative_total": conservative,
            "whole_conservative_total": math.floor(conservative) if conservative is not None else None,
            "workload_capacity": staff_capacity.aggregate(members)}

    for chat, roster in sorted(rosters.items()):
        members, seen = [], set()
        for user in roster or []:
            uid = int(user["user_id"])
            if uid in seen:
                continue
            seen.add(uid)
            if uid in veo_forecast.EXCLUDED_USER_IDS:
                continue
            old = lookup.get(uid) or capacity_defaults.apply(user, default)
            plan = (old or {}).get("planning") or {}
            rate = plan.get("planning_average_per_day") if plan.get("policy") == default["policy"] else None
            m = {"user_id": uid, "name": user.get("user_name") or (old or {}).get("name") or str(uid),
                 "average": rate, "conservative": plan.get("safe_plan_per_day") if rate is not None else None,
                 "provisional": plan.get("provisional", False), "basis": plan.get("estimate_origin"),
                 "workload_capacity": old.get("workload_capacity", {})}
            members.append(m)
            unique[uid] = m
            membership.setdefault(uid, set()).add(chat)
        groups.append({"chat_id": chat, "team": GROUPS[chat]["name"], "poll_recorded": roster is not None,
                       "unverified_membership_records": getattr(roster, "unverified", 0),
                       "forecast_scope": "combined_person_dd", "team_sales_forecast": None,
                       "excluded_members": len(seen.intersection(veo_forecast.EXCLUDED_USER_IDS)),
                       "members": members, **totals(members)})
        if roster is None:
            groups[-1]["workload_capacity"] = staff_capacity.aggregate([], tracked=False)
    overall = totals(list(unique.values()))
    overall.update(pending_teams=sum(r is None for r in rosters.values()),
                   shared_members=sum(len(chats) > 1 for chats in membership.values()),
                   excluded_members=len({int(u["user_id"]) for roster in rosters.values() for u in roster or []}
                       .intersection(veo_forecast.EXCLUDED_USER_IDS)))
    overall["unverified_membership_records"] = sum(g["unverified_membership_records"] for g in groups)
    overall["roster_complete"] = overall["pending_teams"] == 0 and not overall["unverified_membership_records"]
    if not overall["roster_complete"]:
        overall["average_per_all_active"] = None
    if not any(roster is not None for roster in rosters.values()):
        overall.update(recorded_output_total=None, conservative_total=None, whole_conservative_total=None)
        overall["workload_capacity"] = staff_capacity.aggregate([], tracked=False)
    return {"ok": True, "read_only": True, "work_date": work_date.isoformat(), "metric": "recorded Done Deals/day",
            "planning_policy": default["policy"], "history_through": history_end.isoformat(),
            "default": default, "groups": groups, "overall": overall, "assumptions": definition(default)}


def snapshot(now: dt.datetime, approved: set[int]) -> dict:
    if DAILY_REPORTS_CHAT_ID not in approved:
        raise PermissionError("DAILY REPORTS must be approved")
    from automation_controls import enabled_chat_ids
    teams = enabled_chat_ids(approved).intersection(GROUPS)
    date = now.astimezone(MANILA).date() + dt.timedelta(days=1)
    import report_topics
    if report_topics.enabled():
        import organized_reports
        history_end = reports.latest_report_date(now)
        raw = organized_reports.load_capacity(history_end, now, approved)
        capacities = organized_reports.current_capacities(raw, organized_reports.sources(approved), now, forecast_date=date)
        rosters = organized_reports._rosters(teams, date, now)
        result = build(date, capacities.get("businesses", {}).get("veo", []), rosters,
            history_end=history_end, default_override=capacities.get("defaults", {}).get("veo"))
        result.update(checked_at=now.isoformat(), membership=capacities.get("membership", {}))
        return result
    counts = daily_poll_counts(teams, date) if teams else {}
    rosters = {chat: daily_poll_active_users(counts[chat]["poll_id"]) if counts.get(chat, {}).get("poll_id") else None for chat in teams}
    import page_access
    rosters = {chat: page_access.scoped_voters(chat, users, date) if users is not None else None
        for chat, users in rosters.items()}
    # No known Active voter means no historical query or invented average.
    historical = historical_members(now, approved.intersection(GROUPS)) if any(rosters.values()) else []
    result = build(date, historical, rosters, history_end=reports.latest_report_date(now))
    result["checked_at"] = now.isoformat()
    result["history_through"] = reports.latest_report_date(now).isoformat()
    return result


def render(data: dict) -> str:
    lines = ["<b>TOMORROW — VEO STAFF OUTPUT & SALES</b>", f"{data['work_date']} (PHT)", "Capacity evidence and recent-demand sales are separate"]
    def describe(t):
        n, rated = t["active_members"], t["rated_members"]
        if not n:
            return ["Active: 0 · No average to calculate."]
        if not rated:
            return [f"Active: {n} · Rated history: 0/{n}", "Average and total: pending history."]
        return [f"Active: {n} · Forecast coverage: {rated}/{n}",
                *staff_capacity.render(t["workload_capacity"]),
                f"Personal: {t['personal_members']} · Trial default: {t['provisional_members']}",
                f"Average per rated member: {t['average_per_rated_member']:.2f}",
                f"Recent-demand sales total: {t['recorded_output_total']:.2f} · Sales buffer (80%): {t['conservative_total']:.2f}"]
    for group in data["groups"]:
        lines += ["", f"<b>{html.escape(group['team'])}</b>"]
        lines += describe(group) if group["poll_recorded"] else ["Pending — tomorrow's availability poll is not tracked yet."]
    if any(g["poll_recorded"] for g in data["groups"]):
        lines += ["", "<b>Overall — tracked voters, each counted once</b>", *describe(data["overall"])]
    if data["overall"].get("unverified_membership_records"):
        lines += ["Membership checks are pending; counts and totals cover verified current humans only."]
    advanced = data.get("default", {}).get("live_forecast")
    description = ["Guarded personal forecast: newer day/offer models must pass earlier personal error checks; otherwise keep the earlier model.",
        "Completed-date output, attendance and available delivered-offer history inform estimates. Sparse personal evidence stays provisional."] if advanced else [
        "Choose per person between the 14-day reference and smoothed 7-day rate, using earlier personal errors only.",
        "The recent rate blends a weak 0.20 prior with personal evidence. Under 3 Active dates is provisional."]
    lines += ["", "Unrated members stay unknown; totals cover rated voters only. Shared members make team totals overlap.",
              "Repeatable output was recorded on 3+ separate Active dates per person; it does not predict daily sales or verify maximum production.",
              "Confirmed no-work/limited-work dates do not lower supplied-work capacity; ads off does not prove no organic or backlog work.",
              *description,
              "Correction uses earlier matched Active dates only. All forecasts are trials pending future validation.",
              "Figures cover each person's combined VEO DD; team-only sales remain a separate forecast scope.",
              "The 80% buffer is a planning choice, not a guarantee or production limit. All current human members are eligible.",
              "Availability votes and incomplete export/live-bot records limit accuracy. Updates as votes change."]
    correction = data.get("default", {}).get("calibration") or {}
    lines.append("Correction follows each person's selected model; some predictions still worsen." if advanced or correction.get("per_person") else
                 f"Recent forecast correction: x{correction['factor']:.2f}." if correction.get("available") else
                 "Recent forecast correction: insufficient history; uncalibrated trial.")
    return "\n".join(lines)


def queue_automation(now: dt.datetime, approved: set[int]) -> int:
    if not reports.enabled() or DAILY_REPORTS_CHAT_ID not in approved or not chat_enabled(DAILY_REPORTS_CHAT_ID):
        return 0
    teams = approved.intersection(GROUPS)
    if not teams:
        return 0
    date = now.astimezone(MANILA).date() + dt.timedelta(days=1)
    scope = hashlib.sha256(",".join(map(str, sorted(teams))).encode()).hexdigest()[:10]
    return int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=now,
        dedupe_key=f"tomorrow-veo-capacity:v{VERSION}:{date}:{scope}", repeat_interval_minutes=5, payload={
            "text": "Updating tomorrow's capacity…", "parse_mode": "HTML", "disable_notification": True,
            "tomorrow_capacity_date": date.isoformat(), "tomorrow_capacity_version": VERSION,
            "tomorrow_capacity_source_ids": sorted(teams)}))


def prepare_delivery(action: dict, now: dt.datetime) -> bool:
    p = action["payload"]
    if not p.get("tomorrow_capacity_date"):
        return True
    approved = allowed_chat_ids()
    date = now.astimezone(MANILA).date() + dt.timedelta(days=1)
    if (not reports.enabled() or int(action["chat_id"]) != DAILY_REPORTS_CHAT_ID
            or DAILY_REPORTS_CHAT_ID not in approved or p["tomorrow_capacity_date"] != date.isoformat()
            or p.get("tomorrow_capacity_version") != VERSION
            or set(p["tomorrow_capacity_source_ids"]) != approved.intersection(GROUPS)):
        action["repeat_interval_minutes"] = None  # Finish this obsolete recurring board permanently.
        return False
    data = snapshot(now, approved)
    text = render(data)
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    if action.get("telegram_message_id") and p.get("_tomorrow_fingerprint") == fingerprint:
        return False  # Scheduled check stays quiet until votes or history change.
    p.update(text=text, _tomorrow_fingerprint=fingerprint, tomorrow_capacity_snapshot=data)
    if action.get("telegram_message_id"):
        p["edit_message_id"] = int(action["telegram_message_id"])
    return True

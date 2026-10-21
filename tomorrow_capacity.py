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
from automation_controls import chat_enabled
from cloud_store import daily_poll_counts, daily_poll_active_users, enqueue_scheduled_action, request, allowed_chat_ids
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID, MANILA

VERSION = 1


def historical_members(now: dt.datetime, teams: set[int]) -> list[dict]:
    date = reports.latest_report_date(now)
    import report_topics
    if report_topics.enabled():
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(teams))) as pool:
            groups = list(pool.map(lambda chat: member_capacity.archive_report(chat, date - dt.timedelta(days=13), date), sorted(teams)))
        return member_capacity.combined(groups)["members"]
    scope = hashlib.sha256(",".join(map(str, sorted(teams))).encode()).hexdigest()[:10]
    saved = reports._saved(f"daily-member-capacity:v{reports.VERSION}:{date}:{scope}:snapshot")
    if saved is not None:
        return saved["members"]
    return member_capacity.backread(teams, now, end=date)["combined"]["members"]


def build(work_date: dt.date, historical: list[dict], rosters: dict[int, list[dict] | None]) -> dict:
    lookup = {int(m["user_id"]): m for m in historical if m.get("user_id") is not None}
    groups, unique, membership = [], {}, {}

    def totals(members):
        rated = [m for m in members if m["average"] is not None]
        expected = sum(m["average"] for m in rated) if rated else None
        conservative = sum(m["conservative"] for m in rated) if rated else None
        return {"active_members": len(members), "rated_members": len(rated), "unknown_members": len(members) - len(rated),
            "average_per_rated_member": expected / len(rated) if rated else None,
            "average_per_all_active": expected / len(members) if members and len(rated) == len(members) else None,
            "recorded_output_total": expected, "conservative_total": conservative,
            "whole_conservative_total": math.floor(conservative) if conservative is not None else None}

    for chat, roster in sorted(rosters.items()):
        members, seen = [], set()
        for user in roster or []:
            uid = int(user["user_id"])
            if uid in seen:
                continue
            seen.add(uid)
            old = lookup.get(uid)
            plan = (old or {}).get("planning") or {}
            rate = plan.get("planning_average_per_day") if plan.get("policy") in {"veo-reviewed-v1", "top-three-14d-v1"} else None
            m = {"user_id": uid, "name": user.get("user_name") or (old or {}).get("name") or str(uid),
                 "average": rate, "conservative": plan.get("safe_plan_per_day") if rate is not None else None}
            members.append(m)
            unique[uid] = m
            membership.setdefault(uid, set()).add(chat)
        groups.append({"chat_id": chat, "team": GROUPS[chat]["name"], "poll_recorded": roster is not None,
                       "members": members, **totals(members)})
    overall = totals(list(unique.values()))
    overall.update(pending_teams=sum(r is None for r in rosters.values()),
                   shared_members=sum(len(chats) > 1 for chats in membership.values()))
    overall["roster_complete"] = overall["pending_teams"] == 0
    if not overall["roster_complete"]:
        overall["average_per_all_active"] = None
    import report_topics
    assumptions = ("14 completed dates: member estimates use their top three eligible output dates. Safe targets are "
        "floor(member estimate × 0.80), summed across rated voters. Unknown histories stay unknown. "
        "Shared members count once overall; team totals overlap. These are best-day estimates, not verified maximums or guarantees."
        if report_topics.enabled() else
        "Average and totals cover rated Active voters only. Missing histories are unknown, not zero. People active in multiple VEO teams count once overall; team totals overlap and must not be added. Conservative totals use a 20% planning buffer, not a guarantee. Attendance, workload and completed production remain unverified.")
    return {"ok": True, "read_only": True, "work_date": work_date.isoformat(), "metric": "recorded Done Deals/day",
            "planning_policy": "top-three-14d-v1" if report_topics.enabled() else "veo-reviewed-v1",
            "groups": groups, "overall": overall, "assumptions": assumptions}


def snapshot(now: dt.datetime, approved: set[int]) -> dict:
    if DAILY_REPORTS_CHAT_ID not in approved:
        raise PermissionError("DAILY REPORTS must be approved")
    from automation_controls import enabled_chat_ids
    teams = enabled_chat_ids(approved).intersection(GROUPS)
    date = now.astimezone(MANILA).date() + dt.timedelta(days=1)
    counts = daily_poll_counts(teams, date) if teams else {}
    rosters = {chat: daily_poll_active_users(counts[chat]["poll_id"]) if counts.get(chat, {}).get("poll_id") else None for chat in teams}
    import page_access
    rosters = {chat: page_access.scoped_voters(chat, users, date) if users is not None else None
        for chat, users in rosters.items()}
    # No known Active voter means no historical query or invented average.
    historical = historical_members(now, approved.intersection(GROUPS)) if any(rosters.values()) else []
    result = build(date, historical, rosters)
    result["checked_at"] = now.isoformat()
    result["history_through"] = reports.latest_report_date(now).isoformat()
    return result


def render(data: dict) -> str:
    lines = ["<b>TOMORROW — VEO CAPACITY</b>", f"{data['work_date']} (PHT)", "Recorded Done Deals/day estimates"]
    def describe(t):
        n, rated = t["active_members"], t["rated_members"]
        if not n:
            return ["Active: 0 · No average to calculate."]
        if not rated:
            return [f"Active: {n} · Rated history: 0/{n}", "Average and total: pending history."]
        return [f"Active: {n} · Rated history: {rated}/{n}",
                f"Average per rated member: {t['average_per_rated_member']:.2f}",
                f"Rated members' total: {t['recorded_output_total']:.2f} · Conservative: {t['conservative_total']:.2f}"]
    for group in data["groups"]:
        lines += ["", f"<b>{html.escape(group['team'])}</b>"]
        lines += describe(group) if group["poll_recorded"] else ["Pending — tomorrow's availability poll is not tracked yet."]
    if any(g["poll_recorded"] for g in data["groups"]):
        lines += ["", "<b>Overall — tracked voters, each counted once</b>", *describe(data["overall"])]
    lines += ["", "Unrated members stay unknown; totals cover rated voters only. Shared members make team totals overlap.",
              "14-day top-three estimate; safe = floor(each estimate × 80%). Estimates, not verified production limits." if data.get("planning_policy") == "top-three-14d-v1" else
              "Conservative = 80% of the unrounded average. Estimates, not verified production limits.",
              "Availability votes and incomplete export/live-bot records limit accuracy. Updates as votes change."]
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

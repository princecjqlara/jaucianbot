"""Daily manager updates from archive evidence, delivered by the scheduled outbox."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import os
import re
import urllib.parse
from collections import defaultdict

import member_capacity
from automation_controls import chat_enabled
from cloud_store import enqueue_scheduled_action, record_automation_marker, request, allowed_chat_ids
from daily_automation import DAILY_REPORTS_CHAT_ID, MANILA

VERSION = 4
RECENT_MEMBER_DAYS = 30


def enabled() -> bool:
    return os.environ.get("MEMBER_CAPACITY_REPORTS_ENABLED", "false").strip().lower() == "true"


def latest_report_date(now: dt.datetime) -> dt.date:
    local = now.astimezone(MANILA)
    return local.date() - dt.timedelta(days=2 if local.hour == 0 and local.minute < 10 else 1)


def render(snapshot: dict, date: dt.date) -> tuple[list[str], list[dict]]:
    """Show conservative and repeat-supported plans with their observed basis."""
    cutoff = (date - dt.timedelta(days=RECENT_MEMBER_DAYS - 1)).isoformat()
    members = [dict(m) for m in snapshot["combined"]["members"] if (m.get("last_activity_date") or "") >= cutoff]
    members.sort(key=lambda m: (m["name"].casefold(), str(m["user_id"])))
    evidence = [e for g in snapshot["groups"] for e in g["evidence"]]
    grouped, unknown = defaultdict(list), []

    def matches(member: dict, candidate: dict) -> bool:
        return (candidate["user_id"] == member["user_id"] if member["user_id"] is not None else
                candidate["user_id"] is None and candidate["name"].casefold() == member["name"].casefold())

    def primary_team(member: dict) -> str:
        candidates = []
        for group in snapshot["groups"]:
            for candidate in group["members"]:
                if not matches(member, candidate):
                    continue
                if member["user_id"] is None and group["team"] not in member["teams"]:
                    continue
                recent = [d for d in candidate.get("daily", []) if d["date"] >= cutoff]
                score = (sum(d["done_deals"] for d in recent),
                         sum(d["attendance"] == "Active" for d in recent),
                         candidate.get("last_activity_date") or "", group["team"])
                candidates.append((score, group["team"]))
        return max(candidates)[1] if candidates else next(iter(member["teams"]), "Other members")

    for m in members:
        if not m["done_deals"] or m.get("average_per_output_day") is None:
            unknown.append(m)
            continue
        peak_dates = {d["date"] for d in m["daily"] if d["done_deals"] == m.get("observed_daily_peak")}
        matching_evidence = [e for e in evidence if
            (e["user_id"] == m["user_id"] if m["user_id"] is not None else
             e["member"].casefold() == m["name"].casefold() and e["team"] in m["teams"])]
        sample = next((e for e in matching_evidence if e["work_date"] in peak_dates), next(iter(matching_evidence), None))
        name = html.escape(" ".join(m["name"].split())[:65])
        if sample:
            name = f'<a href="{html.escape(sample["url"], quote=True)}">{name}</a>'
            m["report_source"] = sample  # Retain full group/date/message evidence for audit.
        m["report_team"] = primary_team(m)
        plan = member_capacity.reviewed_planning(m, date)
        m["planning"] = plan
        safe = plan["safe_plan_per_day"]
        if safe is None:
            safe_text = (f"Need {plan['minimum_active_days']} Active days"
                         if plan["known_active_days"] < plan["minimum_active_days"] else
                         f"Need {plan['minimum_repeat_days']} output days")
        else:
            safe_text = f"{safe:.2f}/day"
        reachable = plan["reachable_target_per_day"]
        reachable_text = (f"{reachable}/day ({plan['reachable_support_days']}/{plan['target_active_days']} Active days)" if reachable else
                          "Below 1/day" if reachable == 0 else "More Active/output days needed")
        average = (f"{plan['active_day_average']:.2f} ({plan['known_active_days']} days)"
                   if plan["active_day_average"] is not None else "No Active-day records")
        block = (f"<b>{name}</b>\n"
                 f"Average recorded: {average}\n"
                 f"Conservative plan: {safe_text}\n"
                 f"Typical daily target: {reachable_text}")
        grouped[m["report_team"]].append(block)

    header = (f"<b>DAILY CAPACITY PLAN</b>\nThrough {date:%B %d, %Y} (PHT)\n"
              "30-day planning estimates in Done Deals/day")
    intro = ("Decimals are average output; results combine each member's VEO teams.\n"
             "Conservative plans are estimates, not daily guarantees.")
    parts = [header + "\n\n" + intro]
    current_section = None

    def add(block: str, section: str | None = None) -> None:
        nonlocal current_section
        section_header = f"<b>{html.escape(section)}</b>\n\n" if section and section != current_section else ""
        candidate = parts[-1] + "\n\n" + section_header + block
        visible = html.unescape(re.sub(r"<[^>]*>", "", candidate))
        if len(visible.encode("utf-16-le")) // 2 > 3500:
            parts.append(header)
            section_header = f"<b>{html.escape(section)}</b>\n\n" if section else ""
        parts[-1] += "\n\n" + section_header + block
        current_section = section

    for team in sorted(grouped, key=str.casefold):
        for block in grouped[team]:
            add(block, team)
    for member in unknown:
        name = html.escape(" ".join(member["name"].split())[:65])
        add(f"• {name}", "Not enough Done Deal records yet")
    add("Average includes zero-output Active dates. Conservative plan = 80% of the average.\n"
        "Typical target needs success on at least half of Active dates and 3 dates.\n"
        "At least 7 Active and 3 output dates are required for a planning estimate.\n"
        "Below 1/day does not mean unable to work. Availability and workload are not verified.\n"
        "Done Deals are paid-sale records; completed projects and maximum capacity remain unverified.\n"
        "Sources: exports and live bot; missing or unreadable posts are excluded. Tap names for sources.\n"
        "Updated daily after 12:10 AM PHT.")
    return parts, members


def previous_message_ids(date: dt.date, scope: str) -> list[int]:
    """Reuse this day's previous-format posts so a correction does not add clutter."""
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "telegram_message_id", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}", "status": "eq.sent",
        "dedupe_key": f"like.daily-member-capacity:v{VERSION - 1}:{date}:{scope}:part:*",
        "order": "id.asc", "limit": 1000,
    })) or []
    return list(dict.fromkeys(int(r["telegram_message_id"]) for r in rows if r.get("telegram_message_id")))


def _saved(key: str) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"eq.{key}", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else None


def queue_automation(now: dt.datetime, approved: set[int]) -> int:
    if not enabled() or DAILY_REPORTS_CHAT_ID not in approved or not chat_enabled(DAILY_REPORTS_CHAT_ID):
        return 0
    local = now.astimezone(MANILA)
    if local.hour == 0 and local.minute < 10:
        return 0
    from daily_automation import GROUPS
    teams = approved.intersection(GROUPS)
    if not teams:
        return 0
    date = latest_report_date(now)
    scope = hashlib.sha256(",".join(map(str, sorted(teams))).encode()).hexdigest()[:10]
    key = f"daily-member-capacity:v{VERSION}:{date}:{scope}"
    if _saved(key + ":complete") is not None:
        return 0
    saved = _saved(key + ":snapshot")
    if saved is None:
        snapshot = member_capacity.backread(teams, now, end=date)
        parts, members = render(snapshot, date)
        saved = {"parts": parts, "work_date": date.isoformat(), "source_chat_ids": sorted(teams),
                 "edit_message_ids": previous_message_ids(date, scope),
                 "members": [{k: v for k, v in m.items() if k != "daily"} for m in members],
                 "source_counts": {g["team"]: g["source_counts"] for g in snapshot["groups"]},
                 "backtest": member_capacity.backtest(snapshot["combined"]["members"], date),
                 "coverage_start": snapshot["combined"].get("start_date"), "verified_maximum": False}
        request("scheduled_actions?on_conflict=dedupe_key&select=id", {
            "chat_id": DAILY_REPORTS_CHAT_ID, "action_type": "message", "status": "cancelled",
            "scheduled_for": now.isoformat(), "dedupe_key": key + ":snapshot", "payload": saved,
        }, prefer="resolution=ignore-duplicates,return=representation")
        saved = _saved(key + ":snapshot")
        if saved is None:
            raise RuntimeError("Capacity snapshot was not saved")
    queued = 0
    edits = saved.get("edit_message_ids") or []
    for number, part in enumerate(saved["parts"], 1):
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message",
            scheduled_for=now, dedupe_key=f"{key}:part:{number}", payload={
                "text": part + (f"\n\nCapacity · part {number}/{len(saved['parts'])}" if len(saved["parts"]) > 1 else ""),
                "parse_mode": "HTML", "disable_notification": number > 1,
                "capacity_report_date": date.isoformat(), "capacity_source_chat_ids": saved["source_chat_ids"],
                "capacity_format_version": VERSION,
                **({"edit_message_id": edits[number - 1]} if number <= len(edits) else {}),
            }))
    for message_id in edits[len(saved["parts"]):]:
        link = f"https://t.me/c/{str(abs(DAILY_REPORTS_CHAT_ID))[3:]}/{edits[0]}"
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message",
            scheduled_for=now, dedupe_key=f"{key}:cleanup:{message_id}", payload={
                "text": f'<a href="{link}">Open the simplified member report</a>.', "parse_mode": "HTML",
                "disable_notification": True, "edit_message_id": message_id,
                "capacity_report_date": date.isoformat(), "capacity_source_chat_ids": saved["source_chat_ids"],
                "capacity_format_version": VERSION,
            }))
    record_automation_marker(DAILY_REPORTS_CHAT_ID, key + ":complete", now)
    return queued


def delivery_allowed(action: dict, now: dt.datetime) -> bool:
    p = action["payload"]
    if not p.get("capacity_report_date"):
        return True
    return (enabled() and int(action["chat_id"]) == DAILY_REPORTS_CHAT_ID
            and int(p.get("capacity_format_version") or 1) == VERSION
            and p["capacity_report_date"] == latest_report_date(now).isoformat()
            and set(p.get("capacity_source_chat_ids") or []).issubset(allowed_chat_ids()))

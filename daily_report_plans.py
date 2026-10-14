"""Manager-only staffing plans from distinct poll voters, separate from spending."""
from __future__ import annotations
import done_deal_tracking
import team_planning

import datetime as dt
import urllib.parse

from cloud_store import daily_poll_counts, enqueue_scheduled_action, request, record_automation_marker
from daily_automation import AVAILABILITY_GROUPS, DAILY_REPORTS_CHAT_ID, MANILA, TRABAWHO_CHAT_ID, money, split_message


def plan_lines(chat_id: int, work_date: dt.date, count: dict | None, *, heading: str = "TOMORROW'S TEAM PLAN") -> list[str]:
    lines = ["📅 " + heading, f"{work_date:%A, %B %d, %Y} (PHT)"]
    if count is None:
        lines.append("Active members: poll not recorded yet; staffing and targets are pending.")
        lines.append("Planned ads budget: pending availability votes.")
    else:
        active = int(count["active_workers"])
        lines.append(f"Active members: {active}")
        quota, budget = team_planning.plan_totals(active, suno=chat_id == TRABAWHO_CHAT_ID)
        if chat_id == TRABAWHO_CHAT_ID:
            lines += [f"Planned client quota: {quota}",
                      f"Planned ads budget: {money(budget)} (Active × 4 × ₱150)"]
        else:
            target = f"{quota} DD • {quota} CD" if done_deal_tracking.enabled() else f"{quota} close deals"
            lines.append(f"Team target: {target}" if active else "Team target: waiting for Active members.")
            lines.append(f"Planned client quota: {quota} (Active × 1.6, rounded up)")
            lines.append(f"Planned ads budget: {money(budget)} (Active × 1.6 × ₱400)")
    lines.append("Availability is a snapshot of votes; each member counts once per team.")
    return lines


def planning_snapshot(now: dt.datetime, allowed: set[int]) -> dict:
    if DAILY_REPORTS_CHAT_ID not in allowed:
        raise PermissionError("Daily Reports group must be approved")
    work_date = now.astimezone(MANILA).date() + dt.timedelta(days=1)
    teams = allowed.intersection(AVAILABILITY_GROUPS)
    counts = daily_poll_counts(teams, work_date) if teams else {}
    lines = ["📊 TOMORROW'S STAFFING & ADS PLAN", f"{work_date:%A, %B %d, %Y} (PHT)", "",
             "Hi team! Here's our current plan based on availability votes. 💛"]
    for chat_id, config in AVAILABILITY_GROUPS.items():
        if chat_id in teams:
            lines += ["", f"👥 {config['name']}", *plan_lines(chat_id, work_date, counts.get(chat_id))[2:-1]]
    lines += ["", "Veo client quotas round up to whole clients. Ad budgets use the exact formula before quota rounding.",
              "", "💰 ACTUAL AD SPENDING",
              "Not reported yet. Planned budgets are not confirmed spending.",
              "Please record verified spend amounts so we can include actual costs.", "",
              "Each member counts once per team across all hourly votes; counts can change with new votes."]
    return {"read_only": True, "checked_at": now.isoformat(), "work_date": work_date.isoformat(), "plan_version": team_planning.PLAN_VERSION,
            "teams": [{"chat_id": chat_id, "team": AVAILABILITY_GROUPS[chat_id]["name"],
                       "active_members": counts[chat_id]["active_workers"] if chat_id in counts else None,
                       "client_quota": team_planning.plan_totals(int(counts[chat_id]["active_workers"]), suno=chat_id == TRABAWHO_CHAT_ID)[0] if chat_id in counts else None,
                       "planned_ads_budget": str(team_planning.plan_totals(int(counts[chat_id]["active_workers"]), suno=chat_id == TRABAWHO_CHAT_ID)[1]) if chat_id in counts else None}
                      for chat_id in sorted(teams)], "report_preview": "\n".join(lines)}


def queue_formula_update(now: dt.datetime, allowed: set[int]) -> int:
    """Publish the formula's current-day and tomorrow plans once per day."""
    if DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    today = now.astimezone(MANILA).date()
    key = f"team-planning-formula:{team_planning.PLAN_VERSION}:{today.isoformat()}"
    if request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}", "dedupe_key": f"eq.{key}", "limit": 1,
    })):
        return 0
    teams = allowed.intersection(AVAILABILITY_GROUPS)
    lines = ["UPDATED QUOTA & PLANNED ADS", "Veo: Active × 1.6 × ₱400. Suno: Active × 4 × ₱150.",
             "Whole-client quotas round up; budgets use the exact formula. These are planned budgets, not actual spending."]
    queued = 0
    from daily_automation import GROUPS, quota_announcement
    for date in (today, today + dt.timedelta(days=1)):
        counts = daily_poll_counts(teams, date)
        lines += ["", f"{date:%A, %B %d, %Y} (PHT)"]
        for chat in sorted(teams):
            lines += ["", AVAILABILITY_GROUPS[chat]["name"], *plan_lines(chat, date, counts.get(chat))[2:-1]]
            if chat in GROUPS:
                queued += int(enqueue_scheduled_action(chat_id=chat, action_type="message",
                    payload={"text": quota_announcement(GROUPS[chat], date, counts.get(chat), today=today),
                             "message_thread_id": GROUPS[chat]["announcements"], "plan_version": team_planning.PLAN_VERSION},
                    scheduled_for=now, dedupe_key=f"team-planning-announcement:{team_planning.PLAN_VERSION}:{date}:{chat}"))
    for number, part in enumerate(split_message("\n".join(lines)), 1):
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message",
                    payload={"text": part, "plan_version": team_planning.PLAN_VERSION}, scheduled_for=now,
                    dedupe_key=f"{key}:part:{number}"))
    record_automation_marker(DAILY_REPORTS_CHAT_ID, key, now)
    return queued

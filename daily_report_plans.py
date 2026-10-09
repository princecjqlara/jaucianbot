"""Manager-only staffing plans from distinct poll voters, separate from spending."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from cloud_store import daily_poll_counts
from daily_automation import AVAILABILITY_GROUPS, DAILY_REPORTS_CHAT_ID, MANILA, TRABAWHO_CHAT_ID, money, quota_for


def plan_lines(chat_id: int, work_date: dt.date, count: dict | None) -> list[str]:
    lines = ["📅 TOMORROW'S TEAM PLAN", f"{work_date:%A, %B %d, %Y} (PHT)"]
    if count is None:
        lines.append("Active members: poll not recorded yet; staffing and targets are pending.")
        if chat_id == TRABAWHO_CHAT_ID:
            lines.append("Planned ads budget: pending availability votes.")
    else:
        active = int(count["active_workers"])
        lines.append(f"Active members: {active}")
        if chat_id == TRABAWHO_CHAT_ID:
            from trabawho_automation import plan_totals
            quota, budget = plan_totals(active)
            lines += [f"Planned client quota: {quota}",
                      f"Planned ads budget: {money(Decimal(budget))} (Active × 2 × ₱150)"]
        else:
            _, quota, _ = quota_for(count)
            lines.append(f"Team target: {quota} DD • {quota} CD" if active else "Team target: waiting for Active members.")
    if chat_id != TRABAWHO_CHAT_ID:
        lines.append("Planned ads budget: no budget rule configured for this team.")
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
    lines += ["", "💰 ACTUAL AD SPENDING",
              "Not reported yet. Planned budgets are not confirmed spending.",
              "Please record verified spend amounts so we can include actual costs.", "",
              "Each member counts once per team across all hourly votes; counts can change with new votes."]
    return {"read_only": True, "checked_at": now.isoformat(), "work_date": work_date.isoformat(),
            "teams": [{"chat_id": chat_id, "team": AVAILABILITY_GROUPS[chat_id]["name"],
                       "active_members": counts[chat_id]["active_workers"] if chat_id in counts else None}
                      for chat_id in sorted(teams)], "report_preview": "\n".join(lines)}

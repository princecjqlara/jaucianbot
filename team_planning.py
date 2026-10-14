"""Shared quota and planned-ad-budget formulas from dated availability votes."""
from decimal import Decimal, ROUND_CEILING
import datetime as dt

PLAN_VERSION = "2026-10-15.2"
VEO_CLIENTS_PER_MEMBER = Decimal("1.6")
VEO_ADS_PER_CLIENT = Decimal("400")
SUNO_CLIENTS_PER_MEMBER = Decimal("4")
SUNO_ADS_PER_CLIENT = Decimal("150")


def plan_totals(active_members: int, *, suno: bool = False) -> tuple[int, Decimal]:
    if isinstance(active_members, bool) or not isinstance(active_members, int) or active_members < 0:
        raise ValueError("Active member count must be a nonnegative integer")
    multiplier, cost = ((SUNO_CLIENTS_PER_MEMBER, SUNO_ADS_PER_CLIENT) if suno else
                        (VEO_CLIENTS_PER_MEMBER, VEO_ADS_PER_CLIENT))
    raw_quota = Decimal(active_members) * multiplier
    quota = int(raw_quota.to_integral_value(rounding=ROUND_CEILING))
    # Budget follows the exact user formula, before rounding the client quota.
    return quota, (raw_quota * cost).quantize(Decimal("0.01"))


def prepare_delivery(action: dict, now: dt.datetime) -> bool:
    """Refresh queued outputs that were prepared using the previous formula."""
    payload = action["payload"]
    if payload.get("plan_version") == PLAN_VERSION:
        return True
    from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID, MANILA, quota_announcement, quota_reminder, deal_progress_for_day, build_group_report, split_message
    from cloud_store import daily_poll_counts, enqueue_scheduled_action
    key = action.get("dedupe_key") or ""
    fields = key.split(":")
    chat = int(action["chat_id"])
    if fields[0] in {"daily-reminder", "daily-quota"} and chat in GROUPS:
        date = dt.date.fromisoformat(fields[1])
        count = daily_poll_counts({chat}, date).get(chat)
        if fields[0] == "daily-quota":
            payload["text"] = quota_announcement(GROUPS[chat], date, count, today=now.astimezone(MANILA).date())
        else:
            progress = deal_progress_for_day(chat, date)
            payload["text"] = quota_reminder(GROUPS[chat], date, int(fields[2]), count,
                progress["dd_total"], progress["cd_total"], progress["capped"], progress["uncertain"])
    elif payload.get("trabawho_progress_date"):
        from trabawho_automation import progress_text
        payload["text"] = progress_text(dt.date.fromisoformat(payload["trabawho_progress_date"]),
            {"active_workers": int(payload["trabawho_active_count"])}, int(payload["trabawho_assigned_count"]))
    elif payload.get("trabawho_work_date"):
        from trabawho_automation import plan_text, FRIENDLY_PLAN_VERSION
        payload["text"] = plan_text(dt.date.fromisoformat(payload["trabawho_work_date"]),
                                   int(payload["trabawho_active_count"]), now.astimezone(MANILA).date())
        payload["trabawho_plan_format"] = FRIENDLY_PLAN_VERSION
    elif fields[0] in {"daily-report", "trabawho-daily-report"}:
        if chat != DAILY_REPORTS_CHAT_ID:
            return False
        date = dt.date.fromisoformat(fields[1])
        if fields[0] == "daily-report":
            team = int(fields[2])
            if team not in GROUPS:
                return False
            tomorrow = now.astimezone(MANILA).date() + dt.timedelta(days=1)
            count = daily_poll_counts({team}, date).get(team)
            next_count = daily_poll_counts({team}, tomorrow).get(team)
            text = build_group_report(team, date, count, plan_date=tomorrow, plan_count=next_count)
        else:
            from trabawho_reports import build_trabawho_report
            text = build_trabawho_report(date, now)
        parts = split_message(text)
        number = int(fields[3])
        if number < 1 or number > len(parts):
            return False
        payload["text"] = parts[number - 1]
        for extra, text in enumerate(parts[number:], number + 1):
            enqueue_scheduled_action(chat_id=chat, action_type="message",
                payload={"text": text, "plan_version": PLAN_VERSION,
                         "done_deal_tracking": payload.get("done_deal_tracking", False)},
                scheduled_for=now, dedupe_key=":".join(fields[:3]) + f":{extra}")
    else:
        return True
    payload["plan_version"] = PLAN_VERSION
    return True

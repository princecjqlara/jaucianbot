"""Three scheduled VEO performance snapshots and tomorrow availability polls.

All posts travel through the existing durable outbox. Multipart reports have
independent acknowledgment checkpoints. Salary and ad budgets stay in the app.
"""
from __future__ import annotations
import datetime as dt
import os
import re
import urllib.parse
from zoneinfo import ZoneInfo
from cloud_store import request, enqueue_scheduled_action, register_daily_poll
from daily_automation import GROUPS

MANILA = ZoneInfo("Asia/Manila")


class ReportScopeChanged(ValueError):
    scope_changed = True


def clean(value, limit=160):
    return re.sub(r"\s+", " ", str(value or "Unknown")).strip()[:limit]


def period(now: dt.datetime) -> tuple[dt.date, str, dt.datetime] | None:
    local = now.astimezone(MANILA)
    if local.hour == 0 and local.minute < 10:
        return None
    if local.hour >= 16:
        return local.date(), "16", local.replace(hour=16, minute=0, second=0, microsecond=0)
    if local.hour >= 8:
        return local.date(), "08", local.replace(hour=8, minute=0, second=0, microsecond=0)
    return local.date() - dt.timedelta(days=1), "24", local.replace(hour=0, minute=10, second=0, microsecond=0)


def split_lines(lines: list[str], limit=3500) -> list[str]:
    parts, current = [], ""
    size = lambda text: len(text.encode("utf-16-le")) // 2
    for line in lines:
        line = str(line)
        while size(line) > limit:
            if current:
                parts.append(current)
                current = ""
            cut = min(len(line), limit)
            while size(line[:cut]) > limit:
                cut -= 1
            parts.append(line[:cut])
            line = line[cut:]
        if size(current) + size(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        parts.append(current)
    return parts


def lines(data: dict, team: dict, slot: str) -> list[str]:
    label = {"08": "08:00 update", "16": "16:00 update", "24": "00:10 previous-day closeout"}[slot]
    capacity = team.get("capacity") or {}
    quota = capacity.get("quota") or {}
    waiting = overdue = 0
    for worker in team["workers"]:
        waiting += sum(c.get("waiting_payment") is True for c in worker.get("clients", []))
        overdue += sum(c.get("overdue") is True for c in worker.get("clients", []))
    result = [f"VEO · {clean(team['name'])} · {data['work_date']} PHT · {label}",
        "Verified VEO ledger. Telegram receipts never create another sale.",
        f"CLOSED: {capacity.get('closed', 0)}/{quota.get('closed', 0)} · DONE: {capacity.get('done', 0)}/{quota.get('done', 0)}",
        f"Clients waiting payment: {waiting} · Staff projects overdue: {overdue}",
        f"Team capacity: {capacity.get('capacity', 0)} · average per worker: {capacity.get('average_worker_capacity', 'unavailable')}",
        "Financial milestones use the work date; open-client status is current as of " + clean(data.get("generated_at")),
        "Finished work waiting only for the client's balance is not staff overdue."]
    for worker in team["workers"]:
        salary = worker["salary"]
        result += ["", f"{clean(worker.get('name'))}: Closed {salary['closed']}/{salary['target']} · Done {salary['done']}/{salary['target']}",
                   "Assigned Pages: " + (", ".join(clean(p.get("name"), 80) for p in worker.get("pages", [])) or "Unavailable")]
        if "operating_quota" in salary:
            result.append(f"Operating quota: {salary['operating_quota']} CD and DD · proven capacity: {salary.get('production_capacity', 'unknown')}. No zero-quota salary; actual verified sales are required.")
        holds = worker.get("receipt_holds") or []
        if holds:
            result.append(f"SALARY ON HOLD: {len(holds)} receipt(s). Complete Converted lead + saved order + verified full payment, then use Recheck in VEO Receipts.")
            reasons = {
                "converted_lead_required": "set lead to Converted",
                "create_and_link_order": "create/link the order",
                "verify_full_payment": "verify the full payment / mark paid",
                "seller_or_order_attribution_changed": "supervisor attribution review",
            }
            for held in holds[:20]:
                steps = ", ".join(reasons.get(key, clean(key)) for key in held.get("reasons", []))
                result.append(f"• {clean(held.get('contact_name'), 100)}: {steps}")
            if len(holds) > 20:
                result.append("More held receipts are listed in the role-protected VEO Receipts view.")
        clients = worker.get("clients", [])
        if not clients:
            result.append("No open or work-date clients recorded.")
        for client in clients:
            state = "finished · client balance pending" if client.get("work_status") == "ready_waiting_payment" and client.get("waiting_payment") else "delivered" if client.get("work_status") == "delivered" else "work in progress"
            if client.get("waiting_payment") and client.get("work_status") != "ready_waiting_payment":
                state += " · balance pending"
            if client.get("overdue"):
                state += f" · OVERDUE {float(client.get('overdue_hours') or 0):.1f}h"
                if not str(client.get("work_reason") or "").strip():
                    state += " · WARNING: add a truthful delay explanation"
            result.append(f"• {clean(client.get('name'), 100)} / {clean(client.get('page_name'), 80)}: {state}")
    tomorrow = capacity.get("tomorrow") or {}
    result += ["", f"Active tomorrow ({tomorrow.get('work_date', 'PHT')}): {tomorrow.get('active', 0)} · Closed and Done team quota: {tomorrow.get('capacity', 0)} each · awaiting declarations: {tomorrow.get('unknown', 0)}"]
    for plan in team.get("page_quotas", []):
        result.append(f"Page quota — {clean(plan['page_name'])}: {plan['quota']} Closed and {plan['quota']} Done (same allocated targets as VEO ad planning)")
    result.append("Salary and ad-budget amounts are available only in the role-protected VEO workspace.")
    return result


def activation(tenant: str, chat: int, now: dt.datetime) -> dt.datetime:
    key = f"workspace-report-activation:{tenant}"
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "payload", "dedupe_key": "eq." + key, "limit": 1})) or []
    if not rows:
        request("scheduled_actions?on_conflict=dedupe_key&select=payload", {"chat_id": chat, "action_type": "message", "status": "cancelled", "dedupe_key": key,
            "scheduled_for": now.isoformat(), "payload": {"enabled_at": now.isoformat()}}, prefer="resolution=ignore-duplicates,return=representation")
        rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "payload", "dedupe_key": "eq." + key, "limit": 1})) or []
    if not rows:
        raise RuntimeError("Report activation persistence failed")
    return dt.datetime.fromisoformat(rows[0]["payload"]["enabled_at"].replace("Z", "+00:00"))


def manifest(data: dict, team: dict, day: dt.date, slot: str, now: dt.datetime) -> dict:
    key = f"workspace-report-manifest:{data['tenant_id']}:{team['id']}:{day}:{slot}"
    path = "scheduled_actions?" + urllib.parse.urlencode({"select": "payload", "dedupe_key": "eq." + key, "limit": 1})
    rows = request(path) or []
    if not rows:
        saved = {"parts": split_lines(lines(data, team, slot)), "signature": team["access_signature"], "created_at": now.isoformat()}
        request("scheduled_actions?on_conflict=dedupe_key&select=payload", {"chat_id": team["chat_id"], "action_type": "message", "status": "cancelled", "dedupe_key": key,
            "scheduled_for": now.isoformat(), "payload": saved}, prefer="resolution=ignore-duplicates,return=representation")
        rows = request(path) or []
    if not rows:
        raise RuntimeError("Report snapshot persistence failed")
    return rows[0]["payload"]


def queue(now: dt.datetime, allowed: set[int]) -> int:
    from veo_workspace import reporting
    current = period(now)
    if not current:
        return 0
    day, slot, scheduled = current
    launch = os.environ.get("VEO_WORKSPACE_LAUNCH_AT", "")
    if not launch or day < dt.datetime.fromisoformat(launch.replace("Z", "+00:00")).astimezone(MANILA).date():
        return 0
    data = reporting(str(day))
    if data.get("report_version") != 2:
        return 0  # Compatible rollout: wait for the new authoritative endpoint.
    teams = [t for t in data["teams"] if t.get("chat_id") in allowed.intersection(GROUPS)]
    if not teams:
        return 0
    last_sync = (data.get("telegram_sync") or {}).get("last_sync_at")
    if any(w.get("telegram_user_id") for t in teams for w in t.get("workers", [])) and (not last_sync or now - dt.datetime.fromisoformat(last_sync.replace("Z", "+00:00")) > dt.timedelta(minutes=5)):
        from veo_workforce import snapshot, push_snapshot
        observation = snapshot(data["tenant_id"], offset=int((data.get("telegram_sync") or {}).get("cursor") or 0), limit=8)
        push_snapshot(observation)
        data = reporting(str(day))
        teams = [t for t in data["teams"] if t.get("chat_id") in allowed.intersection(GROUPS)]
        if not teams:
            return 0
    active_at = activation(data["tenant_id"], teams[0]["chat_id"], now)
    queued = 0
    chat_parts = {}
    if scheduled >= active_at:
        for team in teams:
            if not team.get("workers") and not (team.get("capacity", {}).get("closed") or team.get("capacity", {}).get("done")):
                continue
            if not team.get("access_signature"):
                raise ValueError("Current report access signature required")
            snapshot = manifest(data, team, day, slot, now)
            texts = snapshot["parts"]
            for index, text in enumerate(texts):
                position = chat_parts.get(team["chat_id"], 0)
                chat_parts[team["chat_id"]] = position + 1
                queued += int(enqueue_scheduled_action(chat_id=team["chat_id"], action_type="message", scheduled_for=now + dt.timedelta(minutes=position // 12),
                    dedupe_key=f"veo-shift:{data['tenant_id']}:{team['id']}:{day}:{slot}:{index}", payload={"veo_report_date": str(day), "veo_team_id": team["id"],
                        "veo_report_slot": slot, "veo_report_part": index, "veo_report_parts": len(texts), "veo_report_text": text,
                        "veo_report_signature": snapshot["signature"], "text": "Verified VEO performance snapshot"}))
    # The normal scheduler creates a single non-anonymous poll per mapped group.
    local = now.astimezone(MANILA)
    if local.hour >= 16:
        tomorrow = str(local.date() + dt.timedelta(days=1))
        for chat in sorted({t["chat_id"] for t in teams if t.get("workers")}):
            found = request("daily_polls?" + urllib.parse.urlencode({"select": "poll_id", "chat_id": f"eq.{chat}", "work_date": "eq." + tomorrow, "limit": 1})) or []
            if not found:
                queued += int(enqueue_scheduled_action(chat_id=chat, action_type="poll", scheduled_for=now,
                    dedupe_key=f"veo-active:{data['tenant_id']}:{chat}:{tomorrow}", payload={"veo_availability_date": tomorrow, "veo_tenant_id": data["tenant_id"], "text": "VEO tomorrow availability"}))
    return queued


def send(action: dict) -> dict:
    from veo_workspace import reporting
    from telegram_sender import telegram_call
    p = action["payload"]
    data = reporting(p["veo_report_date"])
    team = next((t for t in data["teams"] if t["id"] == p["veo_team_id"]), None)
    if not team or team.get("chat_id") != int(action["chat_id"]) or team.get("access_signature") != p["veo_report_signature"]:
        raise ReportScopeChanged("Current team or Page permissions changed")
    text = p["veo_report_text"]
    if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > 3500:
        raise ValueError("Invalid report part")
    payload = {"chat_id": int(action["chat_id"]), "text": f"{text}\nPart {int(p['veo_report_part'])+1}/{int(p['veo_report_parts'])}"}
    if team.get("thread_id"):
        payload["message_thread_id"] = team["thread_id"]
    return telegram_call("sendMessage", payload)


def send_availability(action: dict) -> dict:
    from veo_directory import workspace_directory, teams
    from telegram_sender import telegram_call
    p, chat = action["payload"], int(action["chat_id"])
    data = workspace_directory(p["veo_tenant_id"])
    tomorrow = str(dt.datetime.now(MANILA).date() + dt.timedelta(days=1))
    if p["veo_availability_date"] != tomorrow or chat not in teams() or not any(t.get("telegram_chat_id") == chat for t in data["teams"]):
        raise ReportScopeChanged("Availability poll date or mapping changed")
    return telegram_call("sendPoll", {"chat_id": chat, "message_thread_id": GROUPS[chat]["active"],
        "question": f"Active tomorrow — {tomorrow} (Philippine time)?", "options": ["Active", "Not active"], "is_anonymous": False, "allows_multiple_answers": False})


def register_availability(action: dict) -> None:
    p, chat = action["payload"], int(action["chat_id"])
    if not p.get("_veo_poll_id") or not p.get("_veo_message_id"):
        raise RuntimeError("Poll acknowledgment is incomplete")
    if not register_daily_poll(poll_id=p["_veo_poll_id"], chat_id=chat, thread_id=GROUPS[chat]["active"],
            work_date=dt.date.fromisoformat(p["veo_availability_date"]), telegram_message_id=int(p["_veo_message_id"])):
        raise RuntimeError("Availability poll registration unavailable")

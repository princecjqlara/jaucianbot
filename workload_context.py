"""Manager-recorded daily ads and supplied work; cancelled metadata, never delivery."""
from __future__ import annotations

import datetime as dt
import urllib.parse

from cloud_store import request
from daily_automation import GROUPS, MANILA

PREFIX = "staff-workload:v1:"
ADS = {"running", "paused", "unknown"}
WORK = {"sufficient", "limited", "none", "unknown"}


def read(chats: set[int], start: dt.date, end: dt.date) -> list[dict]:
    if not chats:
        return []
    rows = []
    while True:
        params = [("select", "chat_id,payload,updated_at"),
            ("chat_id", "in.(" + ",".join(map(str, sorted(chats))) + ")"),
            ("dedupe_key", f"like.{PREFIX}*"), ("status", "eq.cancelled"),
            ("payload->>work_date", f"gte.{start}"), ("payload->>work_date", f"lte.{end}"),
            ("order", "id.asc"), ("offset", len(rows)), ("limit", 1000)]
        batch = request("scheduled_actions?" + urllib.parse.urlencode(params)) or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
    return [{**r["payload"], "chat_id": int(r["chat_id"]), "updated_at": r["updated_at"]} for r in rows]


def save(data: dict, approved: set[int], now: dt.datetime) -> dict:
    chat, uid = data.get("chat_id"), data.get("user_id")
    if isinstance(chat, bool) or not isinstance(chat, int) or chat not in approved.intersection(GROUPS):
        raise ValueError("An approved VEO group is required")
    if uid is not None and (isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0):
        raise ValueError("Invalid member ID")
    day = dt.date.fromisoformat(data["work_date"])
    today = now.astimezone(MANILA).date()
    if not today - dt.timedelta(days=365) <= day <= today + dt.timedelta(days=1):
        raise ValueError("Work date is out of range")
    ads, work = data.get("ads", "unknown"), data.get("workload", "unknown")
    if ads not in ADS or work not in WORK:
        raise ValueError("Invalid ads or workload status")
    # A page/team having work cannot prove every person received enough of it.
    if work == "sufficient" and uid is None:
        raise ValueError("Sufficient workload requires the member who received it")
    note = data.get("note")
    if not isinstance(note, str) or not 3 <= len(note.strip()) <= 500:
        raise ValueError("A short evidence note is required")
    message_id = data.get("source_message_id")
    if message_id is not None and (isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0):
        raise ValueError("Invalid source message ID")
    payload = {"work_date": str(day), "user_id": uid, "ads": ads, "workload": work,
        "note": note.strip(), "source_message_id": message_id,
        "recorded_at_utc": now.isoformat(), "source": "manager_record"}
    # Past/current actuals only; a future plan is not training evidence.
    payload["actual"] = day <= today
    key = f"{PREFIX}{chat}:{day}:{uid or 'team'}"
    rows = request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": chat, "action_type": "message", "status": "cancelled", "dedupe_key": key,
        "scheduled_for": dt.datetime.combine(day, dt.time.min, MANILA).isoformat(),
        "updated_at": now.isoformat(), "payload": payload,
    }, prefer="resolution=merge-duplicates,return=representation")
    if not rows:
        raise RuntimeError("Workload record was not saved")
    return {"ok": True, "chat_id": chat, **payload, "delivery": "none; reporting context only"}

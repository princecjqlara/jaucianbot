"""Hourly availability stored in durable, non-delivering schedule records."""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from zoneinfo import ZoneInfo

from cloud_store import enqueue_scheduled_action, request

MANILA = ZoneInfo("Asia/Manila")


def enabled(work_date: dt.date) -> bool:
    start = os.environ.get("HOURLY_AVAILABILITY_START_DATE", "").strip()
    return bool(start and work_date >= dt.date.fromisoformat(start))


def read_records(prefix: str, allowed: set[int], work_date: dt.date | None = None) -> list[dict]:
    if not allowed:
        return []
    result = []
    while True:
        filters = {
            "select": "id,chat_id,payload,updated_at,sent_at,telegram_message_id,status",
            "chat_id": "in.(" + ",".join(map(str, sorted(allowed))) + ")",
            "dedupe_key": f"like.{prefix}*", "order": "id.asc", "limit": 1000, "offset": len(result),
        }
        if work_date:
            filters["payload->>work_date"] = f"eq.{work_date.isoformat()}"
        rows = request("scheduled_actions?" + urllib.parse.urlencode(filters)) or []
        result.extend(rows)
        if len(rows) < 1000:
            return result


def poll_marker(chat_id: int, work_date: str, part: int) -> str:
    return f"hourly-poll:{chat_id}:{work_date}:{part}"


def registered(chat_id: int, date: str, part: int) -> bool:
    return bool(request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "dedupe_key": f"eq.{poll_marker(chat_id, date, part)}", "limit": 1,
    })))


def register_poll(action: dict, message: dict, now: dt.datetime) -> bool:
    payload = action["payload"]
    marker = poll_marker(int(action["chat_id"]), payload["hourly_poll_date"], int(payload["hourly_poll_part"]))
    rows = request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": int(action["chat_id"]), "action_type": "message", "status": "cancelled",
        "dedupe_key": marker, "scheduled_for": now.isoformat(),
        "payload": {"hourly_poll_id": message["poll"]["id"], "work_date": payload["hourly_poll_date"],
                    "part": payload["hourly_poll_part"], "hours": payload["hourly_poll_hours"],
                    "message_id": message["message_id"]},
    }, prefer="resolution=ignore-duplicates,return=representation")
    return bool(rows) or registered(int(action["chat_id"]), payload["hourly_poll_date"], payload["hourly_poll_part"])


def context(poll_id: str, allowed: set[int]) -> dict | None:
    if not os.environ.get("HOURLY_AVAILABILITY_START_DATE") or not allowed:
        return None
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "chat_id,payload", "dedupe_key": "like.hourly-poll:*",
        "chat_id": "in.(" + ",".join(map(str, sorted(allowed))) + ")",
        "payload->>hourly_poll_id": f"eq.{poll_id}", "limit": 1,
    })) or []
    return rows[0] if rows else None


def save_answer(update: dict, allowed: set[int]) -> bool | None:
    answer = update.get("poll_answer") or {}
    poll = context(answer.get("poll_id", ""), allowed)
    if not poll:
        return None
    user = answer.get("user") or {}
    if not user.get("id"):
        return False
    options = answer.get("option_ids") or []
    if any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i <= 8 for i in options):
        return False
    meta = poll["payload"]
    hours = [] if 8 in options else [meta["hours"][i] for i in options]
    now = dt.datetime.now(dt.timezone.utc)
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or user.get("username") or str(user["id"])
    payload = {"work_date": meta["work_date"], "user_id": int(user["id"]), "user_name": name,
               "part": meta["part"], "hours": hours, "update_id": int(update.get("update_id", 0))}
    key = f"hourly-answer:{poll['chat_id']}:{meta['work_date']}:{user['id']}:{meta['part']}"
    if request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": poll["chat_id"], "action_type": "message", "status": "cancelled",
        "dedupe_key": key, "scheduled_for": now.isoformat(), "payload": payload,
    }, prefer="resolution=ignore-duplicates,return=representation"):
        return True
    for _ in range(4):
        current = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id,payload,updated_at", "dedupe_key": f"eq.{key}", "limit": 1,
        })) or []
        if not current:
            raise RuntimeError("Hourly response record disappeared")
        row = current[0]
        if int(row["payload"].get("update_id", -1)) >= payload["update_id"]:
            return True
        changed = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id", "id": f"eq.{row['id']}", "updated_at": f"eq.{row['updated_at']}",
        }), {"payload": payload, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation")
        if changed:
            return True
    raise RuntimeError("Hourly response update conflicted; webhook should retry")


def answers(chat_id: int, work_date: dt.date) -> list[dict]:
    people = {}
    for row in read_records(f"hourly-answer:{chat_id}:{work_date.isoformat()}:", {chat_id}):
        payload = row["payload"]
        user_id = int(payload["user_id"])
        person = people.setdefault(user_id, {"user_id": user_id, "user_name": payload["user_name"],
                                             "active": False, "available_hours": [], "updated_at": row["updated_at"]})
        person["available_hours"] = sorted(set(person["available_hours"]).union(payload.get("hours") or []))
        person["active"] = bool(person["available_hours"])
        if row["updated_at"] >= person["updated_at"]:
            person.update(user_name=payload["user_name"], updated_at=row["updated_at"])
    return sorted(people.values(), key=lambda user: (user["user_name"].casefold(), user["user_id"]))


def counts(allowed: set[int], work_date: dt.date) -> dict[int, dict]:
    if not enabled(work_date):
        return {}
    polls = read_records("hourly-poll:", allowed, work_date)
    result = {}
    for chat_id in {int(row["chat_id"]) for row in polls}:
        users = answers(chat_id, work_date)
        result[chat_id] = {"chat_id": chat_id, "poll_id": f"hourly:{chat_id}:{work_date.isoformat()}",
                           "active_workers": sum(user["active"] for user in users), "total_responses": len(users)}
    return result


def current_members(users: list[dict], now: dt.datetime) -> list[dict]:
    hour = now.astimezone(MANILA).hour
    return [user for user in users if "available_hours" not in user or hour in user["available_hours"]]


def hour_label(hour: int) -> str:
    start = dt.datetime(2000, 1, 1, hour)
    end = start + dt.timedelta(hours=1)
    return f"{start.strftime('%I %p').lstrip('0')} – {end.strftime('%I %p').lstrip('0')}"


def queue_polls(work_date: dt.date, now: dt.datetime, configs: dict[int, dict], allowed: set[int]) -> int:
    if not enabled(work_date):
        return 0
    queued = 0
    for chat_id, config in configs.items():
        if chat_id not in allowed:
            continue
        for part in range(3):
            hours = list(range(part * 8, part * 8 + 8))
            queued += int(enqueue_scheduled_action(
                chat_id=chat_id, action_type="poll", scheduled_for=now,
                dedupe_key=f"hourly-poll-send:{chat_id}:{work_date.isoformat()}:{part}",
                payload={"question": f"Choose ALL hours you can work • {work_date:%a, %b %d, %Y} (PHT)\n"
                                     f"Part {part + 1}/3: {hour_label(hours[0]).split(' – ')[0]} to {hour_label(hours[-1]).split(' – ')[1]}",
                         "options": [hour_label(hour) for hour in hours] + ["Not available in these hours"],
                         "is_anonymous": False, "allows_multiple_answers": True, "disable_notification": False,
                         "message_thread_id": config["active"], "hourly_poll_date": work_date.isoformat(),
                         "hourly_poll_part": part, "hourly_poll_hours": hours},
            ))
    return queued

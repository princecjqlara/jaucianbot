"""Supabase Data API access for Vercel and local archive migration."""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class SupabaseError(RuntimeError):
    pass


def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_CHAT_IDS", "")
    try:
        return {int(part.strip()) for part in raw.split(",") if part.strip()}
    except ValueError as error:
        raise RuntimeError("ALLOWED_CHAT_IDS must contain comma-separated numeric chat IDs") from error


def credentials() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url.startswith("https://") or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    return url, key


def request(
    path: str,
    payload: object | None = None,
    *,
    prefer: str | None = None,
    method: str | None = None,
):
    url, key = credentials()
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if prefer:
        headers["Prefer"] = prefer
    target = url + "/rest/v1/" + path
    http_request = urllib.request.Request(
        target,
        data=body,
        headers=headers,
        method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(http_request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
            code = detail.get("code", "unknown") if isinstance(detail, dict) else "unknown"
        except (json.JSONDecodeError, UnicodeError):
            code = "unknown"
        raise SupabaseError(f"Supabase Data API HTTP {error.code} ({code})") from error
    except urllib.error.URLError as error:
        raise SupabaseError(f"Supabase network error ({type(error.reason).__name__})") from error


def save_update(update: dict, allowed: set[int]) -> bool:
    result = request("rpc/insights_ingest_update", {"p_update": update, "p_allowed_ids": sorted(allowed)})
    return bool(result)


def save_poll_answer(update: dict, allowed: set[int]) -> bool:
    result = request(
        "rpc/insights_ingest_poll_answer",
        {"p_update": update, "p_allowed_ids": sorted(allowed)},
    )
    return bool(result)


def archive_status(allowed: set[int]) -> list[dict]:
    return request("rpc/insights_status", {"p_allowed_ids": sorted(allowed)}) or []


def known_chats(allowed: set[int]) -> list[dict]:
    rows = request(
        "chats?select=chat_id,title,chat_type,membership,last_seen_utc&order=last_seen_utc.desc"
    ) or []
    for row in rows:
        row["approved"] = row.get("chat_id") in allowed
    return rows


def archive_messages(
    allowed: set[int], *, since: dt.datetime, limit: int,
    group: int | None = None, query: str | None = None,
) -> list[dict]:
    return request("rpc/insights_messages", {
        "p_allowed_ids": sorted(allowed),
        "p_since": since.isoformat(),
        "p_limit": limit,
        "p_group": group,
        "p_query": query or None,
    }) or []


def daily_messages(chat_id: int, start_utc: dt.datetime, end_utc: dt.datetime) -> list[dict]:
    """Read one local workday, including author IDs for poll/deal matching."""
    filters = urllib.parse.urlencode([
        ("select", "message_id,sent_utc,author_id,author_name,text,content_type,thread_id,source"),
        ("chat_id", f"eq.{chat_id}"),
        ("sent_utc", f"gte.{start_utc.isoformat()}"),
        ("sent_utc", f"lt.{end_utc.isoformat()}"),
        ("order", "sent_utc.desc,message_id.desc"),
        ("limit", 501),
    ])
    return request("messages?" + filters) or []


def upsert_chats(chats: list[dict]) -> None:
    if chats:
        request("chats?on_conflict=chat_id", chats, prefer="resolution=merge-duplicates,return=minimal")


def import_messages(messages: list[dict]) -> None:
    if messages:
        request("messages?on_conflict=chat_id,message_id", messages, prefer="resolution=ignore-duplicates,return=minimal")


def create_scheduled_action(
    *,
    chat_id: int,
    action_type: str,
    payload: dict,
    scheduled_for: dt.datetime,
    repeat_interval_minutes: int | None,
) -> dict:
    rows = request(
        "scheduled_actions",
        {
            "chat_id": chat_id,
            "action_type": action_type,
            "payload": payload,
            "scheduled_for": scheduled_for.isoformat(),
            "repeat_interval_minutes": repeat_interval_minutes,
        },
        prefer="return=representation",
    ) or []
    if not rows:
        raise SupabaseError("Supabase did not return the created schedule")
    return rows[0]


def enqueue_scheduled_action(
    *,
    chat_id: int,
    action_type: str,
    payload: dict,
    scheduled_for: dt.datetime,
    dedupe_key: str,
    repeat_interval_minutes: int | None = None,
) -> bool:
    rows = request(
        "scheduled_actions?on_conflict=dedupe_key",
        {
            "chat_id": chat_id,
            "action_type": action_type,
            "payload": payload,
            "scheduled_for": scheduled_for.isoformat(),
            "dedupe_key": dedupe_key,
            "repeat_interval_minutes": repeat_interval_minutes,
        },
        prefer="resolution=ignore-duplicates,return=representation",
    ) or []
    return bool(rows)


def existing_daily_reminder_chats(work_date: dt.date, hour: int, allowed: set[int]) -> set[int]:
    if not allowed:
        return set()
    filters = urllib.parse.urlencode({
        "select": "chat_id",
        "chat_id": "in.(" + ",".join(str(value) for value in sorted(allowed)) + ")",
        "dedupe_key": f'like."daily-reminder:{work_date.isoformat()}:{hour}:*"',
    })
    rows = request("scheduled_actions?" + filters) or []
    return {int(row["chat_id"]) for row in rows}


def list_scheduled_actions(
    allowed: set[int], *, status: str | None = None, limit: int = 100
) -> list[dict]:
    if not allowed:
        return []
    filters = [
        "select=id,chat_id,action_type,payload,scheduled_for,repeat_interval_minutes,status,attempts,telegram_message_id,last_error,created_at,updated_at,sent_at",
        "chat_id=in.(" + ",".join(str(value) for value in sorted(allowed)) + ")",
        "order=scheduled_for.asc,id.asc",
        "limit=" + str(limit),
    ]
    if status:
        filters.append("status=eq." + urllib.parse.quote(status, safe=""))
    return request("scheduled_actions?" + "&".join(filters)) or []


def cancel_scheduled_action(allowed: set[int], schedule_id: int) -> dict | None:
    if not allowed:
        return None
    filters = [
        "id=eq." + str(schedule_id),
        "chat_id=in.(" + ",".join(str(value) for value in sorted(allowed)) + ")",
        "status=in.(pending,failed)",
    ]
    rows = request(
        "scheduled_actions?" + "&".join(filters),
        {"status": "cancelled", "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        method="PATCH",
        prefer="return=representation",
    ) or []
    return rows[0] if rows else None


def claim_scheduled_actions(allowed: set[int], *, limit: int = 10) -> list[dict]:
    if not allowed:
        return []
    return request(
        "rpc/insights_claim_scheduled_actions",
        {"p_allowed_ids": sorted(allowed), "p_limit": limit},
    ) or []


def finish_scheduled_action(
    schedule_id: int,
    *,
    success: bool,
    telegram_message_id: int | None = None,
    error: str | None = None,
) -> bool:
    result = request(
        "rpc/insights_finish_scheduled_action",
        {
            "p_id": schedule_id,
            "p_success": success,
            "p_telegram_message_id": telegram_message_id,
            "p_error": error,
        },
    )
    return bool(result)


def register_daily_poll(
    *, poll_id: str, chat_id: int, thread_id: int,
    work_date: dt.date, telegram_message_id: int,
) -> bool:
    result = request("rpc/insights_register_daily_poll", {
        "p_poll_id": poll_id,
        "p_chat_id": chat_id,
        "p_thread_id": thread_id,
        "p_work_date": work_date.isoformat(),
        "p_telegram_message_id": telegram_message_id,
    })
    return bool(result)


def daily_poll_counts(allowed: set[int], work_date: dt.date) -> dict[int, dict]:
    rows = request("rpc/insights_daily_poll_counts", {
        "p_work_date": work_date.isoformat(),
        "p_allowed_ids": sorted(allowed),
    }) or []
    return {int(row["chat_id"]): row for row in rows}


def daily_poll_active_users(poll_id: str) -> list[dict]:
    filters = urllib.parse.urlencode({
        "select": "user_id,user_name",
        "poll_id": f"eq.{poll_id}",
        "active": "eq.true",
        "order": "user_name.asc,user_id.asc",
    })
    return request("daily_poll_answers?" + filters) or []


def freebie_actions(allowed: set[int]) -> list[dict]:
    """Read assignment history from the existing durable scheduler table."""
    if not allowed:
        return []
    result: list[dict] = []
    offset = 0
    while True:
        filters = urllib.parse.urlencode({
            "select": "id,chat_id,payload,status,scheduled_for,sent_at,telegram_message_id",
            "chat_id": "in.(" + ",".join(str(chat_id) for chat_id in sorted(allowed)) + ")",
            "dedupe_key": "like.freebie:*",
            "order": "id.asc",
            "limit": 1000,
            "offset": offset,
        })
        rows = request("scheduled_actions?" + filters) or []
        result.extend(rows)
        if len(rows) < 1000:
            return result
        offset += 1000


def update_freebie_action(action_id: int, payload: dict, *, status: str) -> dict | None:
    """Atomically confirm one still-open assignment from a matching Telegram reply."""
    filters = urllib.parse.urlencode({
        "id": f"eq.{action_id}",
        "status": "in.(pending,processing,failed)",
        "payload->>freebie_completed_at": "is.null",
    })
    rows = request(
        "scheduled_actions?" + filters,
        {"payload": payload, "status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        method="PATCH",
        prefer="return=representation",
    ) or []
    return rows[0] if rows else None


def freebie_action_state(action_id: int) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,payload", "id": f"eq.{action_id}", "limit": 1,
    })) or []
    return rows[0] if rows else None

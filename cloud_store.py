"""Supabase Data API access for Vercel and local archive migration."""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request


READ_PAGE_SIZE = 100


class SupabaseError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None,
                 api_code: str | None = None, restriction: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.api_code = api_code
        self.restriction = restriction


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
    if os.environ.get("ARCHIVE_TRANSPORT", "http").strip() == "postgres":
        from postgres_archive import request as postgres_request
        return postgres_request(path, payload, prefer=prefer, method=method)
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
        restriction = None
        try:
            detail = json.loads(error.read().decode("utf-8"))
            code = detail.get("code", "unknown") if isinstance(detail, dict) else "unknown"
            if error.code == 402 and isinstance(detail, dict):
                message = str(detail.get("message", ""))
                restriction = next((reason for reason in (
                    "exceed_egress_quota", "overdue_payment",
                ) if reason in message), None)
        except (json.JSONDecodeError, UnicodeError):
            code = "unknown"
        description = f"Supabase Data API HTTP {error.code} ({code})"
        if restriction:
            description += f": {restriction}"
        raise SupabaseError(description, http_status=error.code,
                            api_code=code, restriction=restriction) from error
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
    result: list[dict] = []
    while len(result) <= 10000:
        filters = urllib.parse.urlencode([
            ("select", "message_id,sent_utc,author_id,author_name,text,content_type,thread_id,source"),
            ("chat_id", f"eq.{chat_id}"),
            ("sent_utc", f"gte.{start_utc.isoformat()}"),
            ("sent_utc", f"lt.{end_utc.isoformat()}"),
            ("order", "sent_utc.desc,message_id.desc"),
            ("limit", READ_PAGE_SIZE),
            ("offset", len(result)),
        ])
        rows = request("messages?" + filters) or []
        result.extend(rows)
        if len(rows) < READ_PAGE_SIZE:
            break
    return result[:10001]


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
        "scheduled_actions?on_conflict=dedupe_key&select=id",
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


def record_automation_marker(chat_id: int, dedupe_key: str, now: dt.datetime) -> bool:
    """Atomically record a checkpoint that can never be delivered to Telegram."""
    return bool(request(
        "scheduled_actions?on_conflict=dedupe_key&select=id",
        {"chat_id": chat_id, "action_type": "message", "payload": {},
         "status": "cancelled", "scheduled_for": now.isoformat(), "dedupe_key": dedupe_key},
        prefer="resolution=ignore-duplicates,return=representation",
    ))


def claim_automation_slot(
    allowed: set[int], now: dt.datetime, *, workflow: str = "", interval_seconds: int = 300,
) -> bool:
    """Limit a workflow's periodic scans across instances using one durable checkpoint."""
    if not allowed:
        return False
    if interval_seconds <= 0:
        raise ValueError("Automation interval must be positive")
    slot = dt.datetime.fromtimestamp(int(now.timestamp()) // interval_seconds * interval_seconds, dt.timezone.utc).isoformat()
    scope = ",".join(str(chat_id) for chat_id in sorted(allowed))
    dedupe_key = f"automation-clock:{workflow + ':' if workflow else ''}{scope}"
    payload = {"automation_slot": slot}
    created = request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": min(allowed), "action_type": "message", "payload": payload,
        "status": "cancelled", "scheduled_for": now.isoformat(), "dedupe_key": dedupe_key,
    }, prefer="resolution=ignore-duplicates,return=representation")
    if created:
        return True
    # One durable row per team scope; the database predicate chooses one winner
    # even when dispatchers attempt the same next slot concurrently.
    return bool(request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "dedupe_key": f"eq.{dedupe_key}", "status": "eq.cancelled",
        "payload->>automation_slot": f"lt.{slot}",
    }), {"payload": payload, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation"))


def existing_closeout_chats(work_date: dt.date, allowed: set[int]) -> set[int]:
    if not allowed:
        return set()
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "chat_id", "chat_id": "in.(" + ",".join(str(c) for c in sorted(allowed)) + ")",
        "dedupe_key": f'like."daily-closeout-complete:{work_date.isoformat()}:*"',
    })) or []
    return {int(row["chat_id"]) for row in rows}


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
    claim: dict | None = None,
) -> bool:
    if claim is not None:
        now = dt.datetime.now(dt.timezone.utc)
        payload = dict(claim.get("payload") or {})
        failures = 0 if success else int(payload.get("_delivery_failures", 0)) + 1
        payload["_delivery_failures"] = failures
        repeat = claim.get("repeat_interval_minutes")
        changes = {
            "payload": payload, "attempts": failures, "updated_at": now.isoformat(),
            "status": ("pending" if repeat else "sent") if success else ("failed" if failures >= 5 else "pending"),
            "last_error": None if success else (error or "delivery failed")[:500],
        }
        if success and repeat:
            changes["scheduled_for"] = (now + dt.timedelta(minutes=repeat)).isoformat()
        elif not success and failures < 5:
            changes["scheduled_for"] = (now + dt.timedelta(minutes=5)).isoformat()
        if success and telegram_message_id is not None:
            if not payload.get("_first_delivery_at"):
                payload["_first_delivery_at"] = now.isoformat()
            changes.update(telegram_message_id=telegram_message_id, sent_at=now.isoformat())
        return _update_claim(claim, changes)
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


def _update_claim(claim: dict, changes: dict) -> bool:
    filters = {"select": "id", "id": f"eq.{claim['id']}", "status": "eq.processing"}
    if claim.get("updated_at"):
        filters["updated_at"] = "eq." + claim["updated_at"]
    return bool(request("scheduled_actions?" + urllib.parse.urlencode(filters), changes,
                        method="PATCH", prefer="return=representation"))


def defer_scheduled_action(claim: dict) -> bool:
    """Reschedule a suppressed reminder without recording a delivery."""
    now = dt.datetime.now(dt.timezone.utc)
    return _update_claim(claim, {
        "status": "pending", "updated_at": now.isoformat(),
        "scheduled_for": (now + dt.timedelta(minutes=claim.get("repeat_interval_minutes") or 15)).isoformat(),
        "attempts": max(int(claim.get("attempts") or 1) - 1, 0),
    })


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
        "select": "user_id,user_name,updated_at",
        "poll_id": f"eq.{poll_id}",
        "active": "eq.true",
        "order": "user_name.asc,user_id.asc",
    })
    return request("daily_poll_answers?" + filters) or []


def activity_messages(
    chat_id: int,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    thread_ids: set[int],
) -> list[dict]:
    """Read all deal-topic messages in a bounded period, with pagination."""
    result: list[dict] = []
    offset = 0
    while True:
        filters = urllib.parse.urlencode([
            ("select", "message_id,sent_utc,author_id,author_name,text,content_type,thread_id,source"),
            ("chat_id", f"eq.{chat_id}"),
            ("sent_utc", f"gte.{start_utc.isoformat()}"),
            ("sent_utc", f"lt.{end_utc.isoformat()}"),
            ("thread_id", "in.(" + ",".join(str(value) for value in sorted(thread_ids)) + ")"),
            ("order", "sent_utc.asc,message_id.asc"),
            ("limit", READ_PAGE_SIZE),
            ("offset", offset),
        ])
        rows = request("messages?" + filters) or []
        result.extend(rows)
        if len(rows) < READ_PAGE_SIZE:
            return result
        offset += len(rows)


def poll_answers_for_range(chat_id: int, start_date: dt.date, end_date: dt.date) -> list[dict]:
    """Read every poll answer for a team over an inclusive date range."""
    result: list[dict] = []
    while True:
        filters = urllib.parse.urlencode([
            ("select", "user_id,user_name,active,daily_polls!inner(work_date,chat_id)"),
            ("daily_polls.chat_id", f"eq.{chat_id}"),
            ("daily_polls.work_date", f"gte.{start_date.isoformat()}"),
            ("daily_polls.work_date", f"lte.{end_date.isoformat()}"),
            ("order", "poll_id.asc,user_id.asc"),
            ("limit", READ_PAGE_SIZE),
            ("offset", len(result)),
        ])
        rows = request("daily_poll_answers?" + filters) or []
        result.extend(rows)
        if len(rows) < READ_PAGE_SIZE:
            return result


FREEBIE_HISTORY_KEYS = (
    "freebie_token", "freebie_assignee_id", "freebie_assignee_name", "freebie_contact_id",
    "freebie_contact_name", "freebie_page", "freebie_thread_id", "freebie_assigned_at",
    "freebie_completed_at", "freebie_completion_message_id", "freebie_cancelled_at",
    "freebie_cancelled_reason",
)
NEW_CLIENT_HISTORY_KEYS = (
    "new_client_token", "new_client_assignee_id", "new_client_assignee_name",
    "new_client_contact_id", "new_client_contact_identity", "new_client_contact_page_id",
    "new_client_contact_psid", "new_client_contact_name", "new_client_page",
    "new_client_thread_id", "new_client_work_date", "new_client_round",
    "new_client_assigned_at", "new_client_assignment_attempt", "new_client_acknowledged_at",
    "new_client_ack_message_id", "new_client_cancelled_at", "new_client_cancelled_reason",
    "_first_delivery_at",
)


def _assignment_history(allowed: set[int], namespace: str, keys: tuple[str, ...]) -> list[dict]:
    """Keep every assignment's identity/ownership while excluding message bodies and details."""
    if not allowed:
        return []
    result: list[dict] = []
    offset = 0
    while True:
        filters = urllib.parse.urlencode({
            "select": "id,chat_id,status,scheduled_for,sent_at,telegram_message_id," + ",".join(
                f"p{index}:payload->{key}" for index, key in enumerate(keys)
            ),
            "chat_id": "in.(" + ",".join(str(chat_id) for chat_id in sorted(allowed)) + ")",
            "dedupe_key": f"like.{namespace}:*",
            "order": "id.asc",
            "limit": READ_PAGE_SIZE,
            "offset": offset,
        })
        rows = request("scheduled_actions?" + filters) or []
        for row in rows:
            payload = {}
            for index, key in enumerate(keys):
                value = row.pop(f"p{index}", None)
                if value is not None:
                    payload[key] = value
            row["payload"] = payload
        result.extend(rows)
        if len(rows) < READ_PAGE_SIZE:
            return result
        offset += len(rows)


def freebie_actions(allowed: set[int]) -> list[dict]:
    return _assignment_history(allowed, "freebie", FREEBIE_HISTORY_KEYS)


def _update_assignment(action_id: int, payload: dict, status: str,
                       statuses: str, confirmation_key: str) -> dict | None:
    """Merge a compact history update into the full stored payload under a revision guard."""
    filters = {"id": f"eq.{action_id}", "status": f"in.({statuses})",
               f"payload->>{confirmation_key}": "is.null"}
    current = request("scheduled_actions?" + urllib.parse.urlencode({
        **filters, "select": "payload,updated_at", "limit": 1,
    })) or []
    if not current:
        return None
    merged = {**(current[0].get("payload") or {}), **payload}
    if current[0].get("updated_at"):
        filters["updated_at"] = "eq." + current[0]["updated_at"]
    rows = request(
        "scheduled_actions?" + urllib.parse.urlencode({**filters, "select": "id"}),
        {"payload": merged, "status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        method="PATCH", prefer="return=representation",
    ) or []
    return rows[0] if rows else None


def update_freebie_action(action_id: int, payload: dict, *, status: str) -> dict | None:
    """Atomically confirm one still-open assignment from a matching Telegram reply."""
    return _update_assignment(action_id, payload, status, "pending,processing,failed", "freebie_completed_at")


def freebie_action_state(action_id: int) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,confirmed:payload->freebie_completed_at", "id": f"eq.{action_id}", "limit": 1,
    })) or []
    if not rows:
        return None
    row = rows[0]
    row["payload"] = {"freebie_completed_at": row.pop("confirmed", None)}
    return row


def new_client_actions(allowed: set[int]) -> list[dict]:
    """Read durable new-client round-robin assignments."""
    return _assignment_history(allowed, "new-client", NEW_CLIENT_HISTORY_KEYS)


def new_client_reply_messages(
    chat_id: int, thread_id: int, since: dt.datetime, before: dt.datetime,
    *, author_ids: set[int] | None = None,
) -> list[dict]:
    """Read archived WORKING replies that can confirm a new-client assignment."""
    filters = [
        ("select", "message_id,sent_utc,author_id,text,thread_id,reply_to_message_id"),
        ("chat_id", f"eq.{chat_id}"),
        ("thread_id", f"eq.{thread_id}"),
        ("sent_utc", f"gte.{since.isoformat()}"),
        ("sent_utc", f"lte.{before.isoformat()}"),
        ("text", "ilike.*working*"),
        ("order", "sent_utc.asc,message_id.asc"),
        ("limit", READ_PAGE_SIZE),
    ]
    if author_ids is not None:
        if not author_ids:
            return []
        filters.append(("author_id", "in.(" + ",".join(str(i) for i in sorted(author_ids)) + ")"))
    result = []
    while True:
        rows = request("messages?" + urllib.parse.urlencode(filters + [("offset", len(result))])) or []
        result.extend(rows)
        if len(rows) < READ_PAGE_SIZE:
            return result


def update_new_client_action(
    action_id: int, payload: dict, *, status: str, include_cancelled: bool = False,
) -> dict | None:
    """Atomically update one unacknowledged new-client assignment."""
    statuses = "pending,processing,failed,cancelled" if include_cancelled else "pending,processing,failed"
    return _update_assignment(action_id, payload, status, statuses, "new_client_acknowledged_at")


def new_client_action_state(action_id: int) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,confirmed:payload->new_client_acknowledged_at", "id": f"eq.{action_id}", "limit": 1,
    })) or []
    if not rows:
        return None
    row = rows[0]
    row["payload"] = {"new_client_acknowledged_at": row.pop("confirmed", None)}
    return row

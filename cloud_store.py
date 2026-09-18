"""Supabase Data API access for Vercel and local archive migration."""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
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


def request(path: str, payload: object | None = None, *, prefer: str | None = None):
    url, key = credentials()
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if prefer:
        headers["Prefer"] = prefer
    target = url + "/rest/v1/" + path
    http_request = urllib.request.Request(target, data=body, headers=headers, method="POST" if body is not None else "GET")
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


def archive_status(allowed: set[int]) -> list[dict]:
    return request("rpc/insights_status", {"p_allowed_ids": sorted(allowed)}) or []


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


def upsert_chats(chats: list[dict]) -> None:
    if chats:
        request("chats?on_conflict=chat_id", chats, prefer="resolution=merge-duplicates,return=minimal")


def import_messages(messages: list[dict]) -> None:
    if messages:
        request("messages?on_conflict=chat_id,message_id", messages, prefer="resolution=ignore-duplicates,return=minimal")

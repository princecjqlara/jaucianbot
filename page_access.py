"""Self-reported team page access; all polls use the scheduled outbox."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import urllib.parse
import time

from cloud_store import enqueue_scheduled_action, request
from daily_automation import AVAILABILITY_GROUPS as GROUPS, TRABAWHO_CHAT_ID

_suno_pages_cache = None


def page_map(chat_id: int) -> dict[str, str]:
    if GROUPS[chat_id].get("client_source") != "suno":
        return GROUPS[chat_id]["crm_pages"]
    global _suno_pages_cache
    if _suno_pages_cache is None or time.monotonic() >= _suno_pages_cache[0]:
        from suno_store import suno_pages
        pages = suno_pages()
        _suno_pages_cache = (time.monotonic() + 30, pages)
    from suno_store import page_belongs_to_team
    return {name: pid for name, pid in _suno_pages_cache[1].items() if page_belongs_to_team(chat_id, name)}


def enabled(chat_id: int) -> bool:
    return chat_id in GROUPS and os.environ.get("NEW_CLIENT_READY_ENABLED", "").strip().lower() == "true"


def version(chat_id: int) -> str:
    from daily_automation import GROUPS as VEO_GROUPS
    scope = "veo-access-survey-v1:" if chat_id in VEO_GROUPS else ""
    return hashlib.sha256((scope + json.dumps(sorted(page_map(chat_id).items()))).encode()).hexdigest()[:12]


def poll_key(chat_id: int, part: int = 0) -> str:
    return f"page-access-poll:{chat_id}:{version(chat_id)}" + (f":part:{part}" if part else "")


def poll_record(chat_id: int, part: int = 0) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "dedupe_key": f"eq.{poll_key(chat_id, part)}", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else None


def queue_poll(chat_id: int, now: dt.datetime) -> int:
    from automation_controls import page_access_polls_enabled
    if not enabled(chat_id) or not page_access_polls_enabled(chat_id):
        return 0
    pages = list(page_map(chat_id).items())
    if not pages:
        return 0
    queued = 0
    total = (len(pages) + 8) // 9
    for part, offset in enumerate(range(0, len(pages), 9)):
        choices = pages[offset:offset + 9]
        queued += int(enqueue_scheduled_action(chat_id=chat_id, action_type="poll", scheduled_for=now,
        dedupe_key=f"page-access-send:{chat_id}:{version(chat_id)}" + (f":part:{part}" if part else ""), payload={
            "question": "Which pages do you have access to?" + (f" ({part + 1}/{total})" if total > 1 else ""),
            "options": [name for name, _ in choices] + (["None of these pages"] if total > 1 else ["No access"]),
            "is_anonymous": False, "allows_multiple_answers": True,
            "message_thread_id": GROUPS[chat_id]["contact_thread"],
            "page_access_version": version(chat_id), "page_access_ids": [pid for _, pid in choices],
            "page_access_part": part,
        }))
    return queued


def queue_veo_surveys(now: dt.datetime, allowed: set[int]) -> int:
    from daily_automation import GROUPS as VEO_GROUPS
    from automation_controls import enabled_chat_ids
    return sum(queue_poll(chat, now) for chat in sorted(enabled_chat_ids(allowed).intersection(VEO_GROUPS)))


def register_poll(action: dict, message: dict, now: dt.datetime) -> bool:
    chat_id, p = int(action["chat_id"]), action["payload"]
    rows = request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": chat_id, "action_type": "message", "status": "cancelled",
        "scheduled_for": now.isoformat(), "dedupe_key": poll_key(chat_id, int(p.get("page_access_part") or 0)),
        "payload": {"page_access_poll_id": message["poll"]["id"],
                    "page_access_version": p["page_access_version"], "page_ids": p["page_access_ids"],
                    "page_access_part": int(p.get("page_access_part") or 0),
                    "message_id": message["message_id"]},
    }, prefer="resolution=ignore-duplicates,return=representation")
    return bool(rows) or bool(poll_record(chat_id, int(p.get("page_access_part") or 0)))


def context(poll_id: str, allowed: set[int]) -> dict | None:
    chats = {chat for chat in allowed if enabled(chat)}
    if not poll_id or not chats:
        return None
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "chat_id,payload", "dedupe_key": "like.page-access-poll:*",
        "chat_id": "in.(" + ",".join(map(str, sorted(chats))) + ")",
        "payload->>page_access_poll_id": f"eq.{poll_id}", "limit": 1,
    })) or []
    return rows[0] if rows else None


def save_answer(update: dict, allowed: set[int]) -> bool | None:
    answer = update.get("poll_answer") or {}
    poll = context(answer.get("poll_id", ""), allowed)
    if not poll:
        return None
    chat_id, meta = int(poll["chat_id"]), poll["payload"]
    if meta.get("page_access_version") != version(chat_id):
        return True  # An obsolete poll cannot grant access to the current pages.
    user, selected = answer.get("user") or {}, answer.get("option_ids") or []
    if not user.get("id") or user.get("is_bot") or any(
            isinstance(i, bool) or not isinstance(i, int) or not 0 <= i <= len(meta["page_ids"]) for i in selected):
        return False
    pages = [] if len(meta["page_ids"]) in selected else [meta["page_ids"][i] for i in selected]
    now = dt.datetime.now(dt.timezone.utc)
    p = {"user_id": int(user["id"]), "user_name": " ".join(filter(None, (
        user.get("first_name"), user.get("last_name")))) or user.get("username") or str(user["id"]),
        "page_ids": pages, "page_access_version": version(chat_id), "update_id": int(update.get("update_id") or 0)}
    part = int(meta.get("page_access_part") or 0)
    p["page_access_part"] = part
    key = f"page-access-member:{chat_id}:{user['id']}" + (f":part:{part}" if part else "")
    if request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": chat_id, "action_type": "message", "status": "cancelled", "dedupe_key": key,
        "scheduled_for": now.isoformat(), "payload": p,
    }, prefer="resolution=ignore-duplicates,return=representation"):
        return True
    for _ in range(4):
        rows = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id,payload,updated_at", "dedupe_key": f"eq.{key}", "limit": 1,
        })) or []
        if not rows:
            raise RuntimeError("Page-access record missing")
        row = rows[0]
        if int(row["payload"].get("update_id", -1)) >= p["update_id"]:
            return True
        if request("scheduled_actions?" + urllib.parse.urlencode({
            "id": f"eq.{row['id']}", "updated_at": f"eq.{row['updated_at']}", "select": "id",
        }), {"payload": p, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation"):
            return True
    raise RuntimeError("Page-access update conflicted; retry")


def members(chat_id: int) -> dict[int, set[str]]:
    if not enabled(chat_id):
        return {}
    rows = []
    while True:
        batch = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "payload", "chat_id": f"eq.{chat_id}",
            "dedupe_key": f"like.page-access-member:{chat_id}:*",
            "payload->>page_access_version": f"eq.{version(chat_id)}",
            "order": "id.asc", "offset": len(rows), "limit": 1000,
        })) or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
    valid = set(page_map(chat_id).values())
    result = {}
    for row in rows:
        p = row["payload"]
        result.setdefault(int(p["user_id"]), set()).update(set(p.get("page_ids") or []).intersection(valid))
    return result


def can_receive(chat_id: int, user_id: int, *, page_id: str | None = None, page: str | None = None,
                access: dict[int, set[str]] | None = None) -> bool:
    if not enabled(chat_id):
        return True
    pid = page_id or page_map(chat_id).get(page)
    return bool(pid and pid in (members(chat_id) if access is None else access).get(user_id, set()))


def scoped_voters(chat_id: int, users: list[dict], work_date: dt.date) -> list[dict]:
    from automation_controls import new_clients_enabled
    if not new_clients_enabled(chat_id):
        return users
    config = GROUPS.get(chat_id, {})
    if not config.get("client_page_scope") or not enabled(chat_id) or work_date.isoformat() < config.get("page_scope_start", ""):
        return users
    access = members(chat_id)
    valid = set(page_map(chat_id).values())
    return [user for user in users if valid.intersection(access.get(int(user["user_id"]), set()))]

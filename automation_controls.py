"""Restrict bot activity without revoking archive access or changing ownership."""

from __future__ import annotations

import os


NEW_CLIENTS_OFF = "New-client features are off for this VEO team. Availability and reports are on."


def new_clients_enabled(chat_id: int) -> bool:
    from daily_automation import GROUPS
    return chat_id not in GROUPS or os.environ.get("VEO_NEW_CLIENTS_ENABLED", "true").strip().lower() == "true"


def page_access_polls_enabled(chat_id: int) -> bool:
    from daily_automation import GROUPS
    return new_clients_enabled(chat_id) or (chat_id in GROUPS and
        os.environ.get("VEO_PAGE_ACCESS_POLLS_ENABLED", "").strip().lower() == "true")


def new_client_delivery_allowed(action: dict) -> bool:
    if new_clients_enabled(int(action["chat_id"])):
        return True
    payload = action.get("payload") or {}
    if (page_access_polls_enabled(int(action["chat_id"]))
            and not any(k.startswith(("new_client_", "client_")) for k in payload)):
        key = str(action.get("dedupe_key") or "")
        if (action.get("action_type") == "poll" and key.startswith("page-access-send:")
                and payload.get("page_access_version") and isinstance(payload.get("page_access_ids"), list)):
            return True
        if (action.get("action_type") == "message" and key.startswith("page-access-nonvoter:")
                and os.environ.get("PAGE_ACCESS_REMINDERS_ENABLED", "").strip().lower() == "true"
                and payload.get("page_access_reminder_poll")
                and isinstance(payload.get("page_access_reminder_members"), list)):
            return True
    return not (any(k.startswith(("new_client_", "client_directory", "client_queue_", "client_handoff_",
                                 "client_transfer_", "page_access_")) for k in payload)
        or str(action.get("dedupe_key") or "").startswith(("new-client:", "new-client-", "ready-", "client-", "page-access-")))


def queue_mode_notices(now, approved: set[int]) -> int:
    from cloud_store import enqueue_scheduled_action
    from daily_automation import GROUPS
    return sum(int(enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
        dedupe_key=f"bot-mode:veo-new-clients-off:v1:{chat}", payload={"text": NEW_CLIENTS_OFF,
            "message_thread_id": GROUPS[chat]["announcements"]}))
        for chat in sorted(enabled_chat_ids(approved).intersection(GROUPS)) if not new_clients_enabled(chat))


def enabled_chat_ids(approved: set[int]) -> set[int]:
    """An absent setting preserves existing behavior; an empty setting pauses all."""
    raw = os.environ.get("AUTOMATION_CHAT_IDS")
    if raw is None:
        return set(approved)
    try:
        configured = {int(part.strip()) for part in raw.split(",") if part.strip()}
    except ValueError:
        raise RuntimeError("AUTOMATION_CHAT_IDS must contain numeric chat IDs") from None
    active = approved.intersection(configured)
    if any(chat > 0 for chat in approved):
        from makata_automation import enabled, private_enabled
        if enabled():
            active.update(chat for chat in approved if chat > 0 and private_enabled(chat))
    return active


def chat_enabled(chat_id: int) -> bool:
    if int(chat_id) in enabled_chat_ids({int(chat_id)}):
        return True
    if int(chat_id) > 0:
        from makata_automation import enabled, private_enabled
        return enabled() and private_enabled(int(chat_id))
    return False


def status(approved: set[int]) -> dict:
    from daily_automation import AVAILABILITY_GROUPS
    active = enabled_chat_ids(approved)
    client_teams = active.intersection(AVAILABILITY_GROUPS)
    return {"active_chat_ids": sorted(active), "paused_chat_ids": sorted(approved - active),
            "archive_collection_enabled": True,
            "page_access_poll_enabled_chat_ids": sorted(c for c in client_teams if page_access_polls_enabled(c)),
            "new_client_enabled_chat_ids": sorted(c for c in client_teams if new_clients_enabled(c)),
            "new_client_disabled_chat_ids": sorted(c for c in client_teams if not new_clients_enabled(c))}

"""Withdraw CRM receipts through the existing durable scheduler, never inline."""
import datetime as dt
import urllib.parse
import uuid

import veo_workspace as workspace
from cloud_store import request, enqueue_scheduled_action


def require_deleted(submission):
    uuid.UUID(submission)
    try:
        workspace.authoritative(submission)
    except workspace.ReceiptDeleted:
        return
    raise ValueError("Receipt has not been deleted in VEO")


def receive(data, start_response, respond):
    submission = data.get("submission_id", "")
    require_deleted(submission)
    # Fetch by source identity; never accept chat or message IDs from the caller.
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,chat_id,payload,status", "payload->>veo_submission_id": "eq." + submission,
        "payload->>veo_receipt_delete": "is.null", "limit": 100})) or []
    for source in rows:
        key = f"veo-receipt-delete:{submission}:{source['id']}"
        existing = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id", "dedupe_key": "eq." + key, "limit": 1})) or []
        if not existing:
            enqueue_scheduled_action(chat_id=int(source["chat_id"]), action_type="message",
                scheduled_for=dt.datetime.now(dt.timezone.utc), dedupe_key=key,
                payload={"veo_receipt_delete": True, "veo_submission_id": submission,
                         "veo_delete_source_action": source["id"], "text": "Remove withdrawn VEO receipt"})
            existing = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id", "dedupe_key": "eq." + key, "limit": 1})) or []
        if not existing:
            raise RuntimeError("Receipt removal could not be queued")
        request("scheduled_actions?" + urllib.parse.urlencode({"id": "eq." + str(source["id"]), "status": "in.(pending,failed)"}),
                {"status": "cancelled", "last_error": "Receipt deleted in VEO"}, method="PATCH")
    return respond(start_response, 200, {"ok": True, "deletion_queued": True})


def deliver(action):
    from cloud_store import _update_claim
    from telegram_sender import telegram_call, TelegramError
    payload = dict(action["payload"])
    require_deleted(payload["veo_submission_id"])
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,chat_id,status,payload,telegram_message_id",
        "id": "eq." + str(int(payload["veo_delete_source_action"])), "limit": 1})) or []
    if not rows:
        raise RuntimeError("Original receipt delivery record unavailable")
    source = rows[0]
    if int(source["chat_id"]) != int(action["chat_id"]) or (source.get("payload") or {}).get("veo_submission_id") != payload["veo_submission_id"]:
        raise ValueError("Receipt removal attribution changed")
    if source["status"] in {"processing", "pending", "failed"}:
        raise RuntimeError("Waiting for original receipt delivery to settle")
    original = source.get("payload") or {}
    ids = {source.get("telegram_message_id"), original.get("_veo_message_id"), original.get("_veo_details_message_id"), *(original.get("_veo_album_message_ids") or [])}
    ids.discard(None)
    done = set(payload.get("veo_deleted_message_ids") or [])
    for message in sorted(ids):
        if isinstance(message, bool) or not isinstance(message, int) or message <= 0:
            raise ValueError("Invalid saved Telegram receipt reference")
        if message in done:
            continue
        try:
            telegram_call("deleteMessage", {"chat_id": int(action["chat_id"]), "message_id": message})
        except TelegramError as error:
            if "message to delete not found" not in str(error).lower():
                raise
        done.add(message)
        payload["veo_deleted_message_ids"] = sorted(done)
        if not _update_claim(action, {"payload": payload}):
            raise RuntimeError("Receipt deletion checkpoint unavailable")
        action["payload"] = payload
    if (original.get("_veo_sending") and not (original.get("_veo_message_id") or source.get("telegram_message_id"))) or (original.get("_veo_details_sending") and not original.get("_veo_details_message_id")):
        raise RuntimeError("Unacknowledged Telegram receipt needs manual reconciliation")
    return True

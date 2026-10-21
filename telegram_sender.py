"""Send approved scheduled actions through Telegram's Bot API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from daily_automation import TRABAWHO_CHAT_ID, SUNO_GROUPS
from automation_controls import chat_enabled


class TelegramError(RuntimeError):
    pass


def telegram_call(method: str, payload: dict, *, timeout: float = 30) -> dict:
    if method not in {"getChat", "getChatMember"} and payload.get("chat_id") is not None and not chat_enabled(int(payload["chat_id"])):
        raise TelegramError("Telegram automation is paused for this group")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise TelegramError("Telegram bot token is not configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
            description = detail.get("description", "request failed")
        except (json.JSONDecodeError, UnicodeError):
            description = "request failed"
        raise TelegramError(f"Telegram HTTP {error.code}: {description}") from error
    except urllib.error.URLError as error:
        raise TelegramError(f"Telegram network error ({type(error.reason).__name__})") from error
    if method == "deleteMessage" and isinstance(result, dict) and result.get("ok") and result.get("result") is True:
        return {"deleted": True}
    if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("result"), dict):
        description = result.get("description", "request failed") if isinstance(result, dict) else "invalid response"
        raise TelegramError(f"Telegram API error: {description}")
    return result["result"]


def send_scheduled_action(action: dict) -> dict:
    from automation_controls import new_client_delivery_allowed
    if not new_client_delivery_allowed(action):
        raise TelegramError("New-client features are disabled for this VEO team")
    action_type = action["action_type"]
    source = action["payload"]
    common = {
        "chat_id": action["chat_id"],
        "disable_notification": bool(source.get("disable_notification", False)),
    }
    # Trabawho's General topic opens at /1, but Telegram rejects thread_id=1
    # when posting there. Omitting it targets General; named topics keep IDs.
    is_trabawho_general = int(action["chat_id"]) in SUNO_GROUPS and source.get("message_thread_id") == 1
    if source.get("message_thread_id") is not None and not is_trabawho_general:
        common["message_thread_id"] = source["message_thread_id"]
    if action_type == "message":
        if source.get("suno_create_topic"):
            from daily_automation import MAKATA_CHAT_ID
            if int(action["chat_id"]) != MAKATA_CHAT_ID or source["suno_create_topic"] not in {"active", "songs"}:
                raise TelegramError("Invalid Suno topic setup")
            result = telegram_call("createForumTopic", {"chat_id": action["chat_id"], "name": source["topic_name"]})
            return {**result, "message_id": int(result["message_thread_id"]), "_telegram_operation": "createForumTopic"}
        for operation, field in (("deleteMessage", "delete_message_id"), ("editMessageText", "edit_message_id"),
                                 ("editMessageReplyMarkup", "edit_reply_markup_message_id")):
            if source.get(field) is None:
                continue
            target = source[field]
            if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
                raise TelegramError("Invalid message operation target")
            payload = {"chat_id": action["chat_id"], "message_id": target}
            if operation == "editMessageReplyMarkup":
                payload["reply_markup"] = source["reply_markup"]
            if operation == "editMessageText":
                payload["text"] = source["text"]
                if source.get("parse_mode"):
                    payload["parse_mode"] = source["parse_mode"]
                # Explicitly remove old acceptance buttons on closure edits.
                if source.get("reply_markup") or source.get("client_handoff_kind") == "closure":
                    payload["reply_markup"] = source.get("reply_markup") or {"inline_keyboard": []}
            try:
                result = telegram_call(operation, payload)
            except TelegramError as error:
                detail = str(error).casefold()
                if operation == "editMessageReplyMarkup" and any(reason in detail for reason in (
                    "message is not modified", "message to edit not found", "message can't be edited", "message cannot be edited",
                )):
                    return {"message_id": target, "_telegram_operation": operation}
                if (operation == "deleteMessage"
                        and str(action.get("dedupe_key") or "").startswith("referral-public-poll-remove:")
                        and any(reason in detail for reason in ("message can't be deleted", "message cannot be deleted"))):
                    # Telegram limits deletion of older messages. Close the
                    # obsolete bot poll so members use the replacement roster.
                    try:
                        result = telegram_call("stopPoll", payload)
                    except TelegramError as stop_error:
                        if not any(reason in str(stop_error).casefold() for reason in (
                            "poll has already been closed", "poll is already closed", "message to stop poll not found",
                        )):
                            raise
                        result = {}
                    return {**result, "message_id": target, "_telegram_operation": "stopPoll"}
                if operation == "editMessageText" and any(reason in detail for reason in (
                    "message to edit not found", "message can't be edited", "message cannot be edited",
                )):
                    # An admin may already have removed the old announcement.
                    # Publish the instruction-only replacement exactly once via
                    # its existing durable scheduled-action key.
                    text = source["text"]
                    replacement = {**common, "text": text}
                    if source.get("parse_mode"):
                        replacement["parse_mode"] = source["parse_mode"]
                    if source.get("reply_markup"):
                        replacement["reply_markup"] = source["reply_markup"]
                    if source.get("client_handoff_kind") == "closure":
                        # Some old force-reply offers cannot be edited. Post a
                        # short closure in the same client topic, linked to the
                        # original offer, without repeating its client brief.
                        replacement["text"] = text.split("\n\n", 1)[0]
                        replacement["reply_parameters"] = {"message_id": target, "allow_sending_without_reply": True}
                        if not replacement.get("message_thread_id"):
                            from daily_automation import AVAILABILITY_GROUPS
                            thread = AVAILABILITY_GROUPS.get(int(action["chat_id"]), {}).get("contact_thread")
                            if thread:
                                replacement["message_thread_id"] = thread
                    return telegram_call("sendMessage", replacement)
                already_done = (operation == "editMessageText" and "message is not modified" in detail
                                or operation == "deleteMessage" and "message to delete not found" in detail)
                if not already_done:
                    raise
                result = {}
            return {**result, "message_id": target, "_telegram_operation": operation}
        text = None
        if action.get("sent_at"):
            text = source.get("freebie_reminder_text") or source.get("new_client_reminder_text")
        payload = {**common, "text": text or source["text"]}
        if source.get("parse_mode"):
            payload["parse_mode"] = source["parse_mode"]
        if source.get("reply_markup"):
            payload["reply_markup"] = source["reply_markup"]
        if source.get("reply_parameters"):
            payload["reply_parameters"] = source["reply_parameters"]
        return telegram_call("sendMessage", payload)
    if action_type == "poll":
        payload = {
            **common,
            "question": source["question"],
            "options": [{"text": option} for option in source["options"]],
            "is_anonymous": bool(source.get("is_anonymous", True)),
            "allows_multiple_answers": bool(source.get("allows_multiple_answers", False)),
        }
        return telegram_call("sendPoll", payload)
    raise TelegramError("Unsupported scheduled action type")

"""Send approved scheduled actions through Telegram's Bot API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from daily_automation import TRABAWHO_CHAT_ID


class TelegramError(RuntimeError):
    pass


def telegram_call(method: str, payload: dict) -> dict:
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
        with urllib.request.urlopen(request, timeout=30) as response:
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
    action_type = action["action_type"]
    source = action["payload"]
    common = {
        "chat_id": action["chat_id"],
        "disable_notification": bool(source.get("disable_notification", False)),
    }
    # Trabawho's General topic opens at /1, but Telegram rejects thread_id=1
    # when posting there. Omitting it targets General; named topics keep IDs.
    is_trabawho_general = int(action["chat_id"]) == TRABAWHO_CHAT_ID and source.get("message_thread_id") == 1
    if source.get("message_thread_id") is not None and not is_trabawho_general:
        common["message_thread_id"] = source["message_thread_id"]
    if action_type == "message":
        for operation, field in (("deleteMessage", "delete_message_id"), ("editMessageText", "edit_message_id")):
            if source.get(field) is None:
                continue
            target = source[field]
            if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
                raise TelegramError("Invalid message operation target")
            payload = {"chat_id": action["chat_id"], "message_id": target}
            if operation == "editMessageText":
                payload["text"] = source["text"]
            try:
                result = telegram_call(operation, payload)
            except TelegramError as error:
                detail = str(error).casefold()
                if operation == "editMessageText" and "message to edit not found" in detail:
                    # An admin may already have removed the old announcement.
                    # Publish the instruction-only replacement exactly once via
                    # its existing durable scheduled-action key.
                    return telegram_call("sendMessage", {
                        "chat_id": action["chat_id"], "text": source["text"],
                        "disable_notification": bool(source.get("disable_notification", False)),
                    })
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

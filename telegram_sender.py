"""Send approved scheduled actions through Telegram's Bot API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


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
    if source.get("message_thread_id") is not None:
        common["message_thread_id"] = source["message_thread_id"]
    if action_type == "message":
        return telegram_call("sendMessage", {**common, "text": source["text"]})
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

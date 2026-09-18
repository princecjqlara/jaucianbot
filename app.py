"""Single WSGI entrypoint for the Vercel Python runtime."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
from http import HTTPStatus
from urllib.parse import parse_qs

from cloud_http import authorized
from cloud_store import (
    allowed_chat_ids,
    archive_messages,
    archive_status,
    cancel_scheduled_action,
    claim_scheduled_actions,
    create_scheduled_action,
    finish_scheduled_action,
    known_chats,
    list_scheduled_actions,
    save_update,
)
from telegram_sender import send_scheduled_action


MAX_BODY_BYTES = 1_000_000
SCHEDULE_STATUSES = {"pending", "processing", "sent", "failed", "cancelled"}


def response(start_response, status: int, payload: dict):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        default=lambda value: value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else str(value),
    ).encode("utf-8")
    start_response(
        f"{status} {HTTPStatus(status).phrase}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def status_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        groups = archive_status(allowed_chat_ids())
    except Exception as error:
        print(f"Status query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, "groups": groups})


def health_route(environ, start_response):
    try:
        archive_status(allowed_chat_ids())
    except Exception as error:
        print(f"Health query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True})


def groups_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        groups = known_chats(allowed_chat_ids())
    except Exception as error:
        print(f"Group discovery query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, "groups": groups})


def messages_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    args = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        limit = int(args.get("limit", ["200"])[0])
        days = float(args.get("days", ["7"])[0])
        group = int(args["group"][0]) if "group" in args else None
        if not 1 <= limit <= 500 or not 0 <= days <= 3650:
            raise ValueError("out of range")
    except (ValueError, IndexError):
        return response(start_response, 400, {"ok": False, "error": "invalid query"})
    try:
        allowed = allowed_chat_ids()
        if group is not None and group not in allowed:
            return response(start_response, 403, {"ok": False})
        messages = archive_messages(
            allowed,
            since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days),
            limit=limit,
            group=group,
            query=args.get("q", [""])[0],
        )
    except Exception as error:
        print(f"Messages query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, "messages": messages})


def read_json_body(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH", "0"))
    except ValueError as error:
        raise ValueError("invalid body size") from error
    if not 0 < length <= MAX_BODY_BYTES:
        raise ValueError("invalid body size")
    try:
        payload = json.loads(environ["wsgi.input"].read(length))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("body must be an object")
    return payload


def validate_action(payload: dict) -> tuple[int, str, dict, dt.datetime, int | None]:
    try:
        chat_id = int(payload["chat_id"])
        action_type = payload["action_type"]
        raw_action = payload["payload"]
        raw_time = payload["scheduled_for"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("missing or invalid schedule fields") from error
    if action_type not in {"message", "poll"} or not isinstance(raw_action, dict):
        raise ValueError("invalid action")
    if not isinstance(raw_time, str):
        raise ValueError("scheduled_for must be an ISO timestamp")
    try:
        scheduled_for = dt.datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("scheduled_for must be an ISO timestamp") from error
    if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
        raise ValueError("scheduled_for must include a UTC offset")
    scheduled_for = scheduled_for.astimezone(dt.timezone.utc)

    repeat = payload.get("repeat_interval_minutes")
    if repeat is not None:
        if isinstance(repeat, bool):
            raise ValueError("invalid repeat interval")
        try:
            repeat = int(repeat)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid repeat interval") from error
        if not 1 <= repeat <= 525_600:
            raise ValueError("invalid repeat interval")

    normalized: dict = {}
    silent = raw_action.get("disable_notification", False)
    if not isinstance(silent, bool):
        raise ValueError("disable_notification must be boolean")
    normalized["disable_notification"] = silent
    if "message_thread_id" in raw_action:
        thread = raw_action["message_thread_id"]
        if isinstance(thread, bool) or not isinstance(thread, int) or thread <= 0:
            raise ValueError("invalid message_thread_id")
        normalized["message_thread_id"] = thread

    if action_type == "message":
        text = raw_action.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError("message text must contain 1 to 4096 characters")
        normalized["text"] = text
    else:
        question = raw_action.get("question")
        options = raw_action.get("options")
        if not isinstance(question, str) or not 1 <= len(question) <= 300:
            raise ValueError("poll question must contain 1 to 300 characters")
        if not isinstance(options, list) or not 2 <= len(options) <= 12:
            raise ValueError("poll must contain 2 to 12 options")
        if any(not isinstance(option, str) or not 1 <= len(option) <= 100 for option in options):
            raise ValueError("poll options must contain 1 to 100 characters")
        anonymous = raw_action.get("is_anonymous", True)
        multiple = raw_action.get("allows_multiple_answers", False)
        if not isinstance(anonymous, bool) or not isinstance(multiple, bool):
            raise ValueError("poll settings must be boolean")
        normalized.update({
            "question": question,
            "options": options,
            "is_anonymous": anonymous,
            "allows_multiple_answers": multiple,
        })
    return chat_id, action_type, normalized, scheduled_for, repeat


def schedules_route(environ, start_response, method: str):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        allowed = allowed_chat_ids()
        if method == "GET":
            args = parse_qs(environ.get("QUERY_STRING", ""))
            status = args.get("status", [None])[0]
            limit = int(args.get("limit", ["100"])[0])
            if status is not None and status not in SCHEDULE_STATUSES:
                raise ValueError("invalid status")
            if not 1 <= limit <= 500:
                raise ValueError("invalid limit")
            rows = list_scheduled_actions(allowed, status=status, limit=limit)
            return response(start_response, 200, {"ok": True, "schedules": rows})
        if method == "POST":
            chat_id, action_type, action_payload, scheduled_for, repeat = validate_action(read_json_body(environ))
            if chat_id not in allowed:
                return response(start_response, 403, {"ok": False, "error": "group is not approved"})
            row = create_scheduled_action(
                chat_id=chat_id,
                action_type=action_type,
                payload=action_payload,
                scheduled_for=scheduled_for,
                repeat_interval_minutes=repeat,
            )
            return response(start_response, 201, {"ok": True, "schedule": row})
        args = parse_qs(environ.get("QUERY_STRING", ""))
        schedule_id = int(args.get("id", [""])[0])
        if schedule_id <= 0:
            raise ValueError("invalid schedule ID")
        row = cancel_scheduled_action(allowed, schedule_id)
        if row is None:
            return response(start_response, 404, {"ok": False})
        return response(start_response, 200, {"ok": True, "schedule": row})
    except ValueError as error:
        return response(start_response, 400, {"ok": False, "error": str(error)})
    except Exception as error:
        print(f"Schedule request failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})


def dispatch_route(environ, start_response):
    try:
        allowed = allowed_chat_ids()
        actions = claim_scheduled_actions(allowed, limit=10)
    except Exception as error:
        print(f"Schedule claim failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "processed": 0, "sent": 0, "failed": 0})
    sent = 0
    failed = 0
    for action in actions:
        try:
            message = send_scheduled_action(action)
            message_id = int(message["message_id"])
            if not finish_scheduled_action(action["id"], success=True, telegram_message_id=message_id):
                raise RuntimeError("schedule was not finalized")
            sent += 1
            try:
                save_update({"message": message}, allowed)
            except Exception as archive_error:
                print(f"Sent message archive failed: {type(archive_error).__name__}")
        except Exception as error:
            failed += 1
            print(f"Scheduled delivery failed: {type(error).__name__}")
            try:
                finish_scheduled_action(action["id"], success=False, error=str(error))
            except Exception as finish_error:
                print(f"Schedule failure update failed: {type(finish_error).__name__}")
    status = 200 if failed == 0 else 502
    return response(start_response, status, {
        "ok": failed == 0,
        "processed": len(actions),
        "sent": sent,
        "failed": failed,
    })


def webhook_route(environ, start_response):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    supplied = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN")
    if not secret:
        return response(start_response, 503, {"ok": False, "error": "webhook not configured"})
    if not supplied or not hmac.compare_digest(supplied, secret):
        return response(start_response, 401, {"ok": False})
    try:
        allowed = allowed_chat_ids()
        if not allowed:
            return response(start_response, 503, {"ok": False, "error": "allowed groups not configured"})
        update = read_json_body(environ)
    except ValueError:
        return response(start_response, 400, {"ok": False, "error": "invalid update"})
    except RuntimeError:
        return response(start_response, 503, {"ok": False, "error": "invalid configuration"})
    try:
        save_update(update, allowed)
    except Exception as error:
        print(f"Webhook storage failed: {type(error).__name__}")
        return response(start_response, 500, {"ok": False})
    return response(start_response, 200, {"ok": True})


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "")
    if path == "/api/status" and method == "GET":
        return status_route(environ, start_response)
    if path == "/api/health" and method == "GET":
        return health_route(environ, start_response)
    if path == "/api/messages" and method == "GET":
        return messages_route(environ, start_response)
    if path == "/api/groups" and method == "GET":
        return groups_route(environ, start_response)
    if path == "/api/schedules" and method in {"GET", "POST", "DELETE"}:
        return schedules_route(environ, start_response, method)
    if path == "/api/cron/dispatch" and method == "GET":
        return dispatch_route(environ, start_response)
    if path == "/api/webhook" and method == "POST":
        return webhook_route(environ, start_response)
    if path in {"/api/health", "/api/status", "/api/messages", "/api/groups", "/api/schedules", "/api/cron/dispatch", "/api/webhook"}:
        return response(start_response, 405, {"ok": False})
    return response(start_response, 404, {"ok": False})

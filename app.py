"""Single WSGI entrypoint for the Vercel Python runtime."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
from http import HTTPStatus
from urllib.parse import parse_qs

from cloud_http import authorized
from cloud_store import allowed_chat_ids, archive_messages, archive_status, save_update


MAX_BODY_BYTES = 1_000_000


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


def webhook_route(environ, start_response):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    supplied = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN")
    if not secret:
        return response(start_response, 503, {"ok": False, "error": "webhook not configured"})
    if not supplied or not hmac.compare_digest(supplied, secret):
        return response(start_response, 401, {"ok": False})
    try:
        length = int(environ.get("CONTENT_LENGTH", "0"))
    except ValueError:
        length = 0
    if not 0 < length <= MAX_BODY_BYTES:
        return response(start_response, 413, {"ok": False, "error": "invalid body size"})
    try:
        allowed = allowed_chat_ids()
        if not allowed:
            return response(start_response, 503, {"ok": False, "error": "allowed groups not configured"})
        update = json.loads(environ["wsgi.input"].read(length))
        if not isinstance(update, dict):
            raise ValueError("not an update object")
    except (json.JSONDecodeError, ValueError):
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
    if path == "/api/webhook" and method == "POST":
        return webhook_route(environ, start_response)
    if path in {"/api/health", "/api/status", "/api/messages", "/api/webhook"}:
        return response(start_response, 405, {"ok": False})
    return response(start_response, 404, {"ok": False})

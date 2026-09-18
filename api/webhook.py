"""Telegram webhook endpoint for Vercel."""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler

from cloud_http import send_json
from cloud_store import allowed_chat_ids, connect, ensure_schema, save_update


MAX_BODY_BYTES = 1_000_000


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not secret:
            send_json(self, 503, {"ok": False, "error": "webhook not configured"})
            return
        if not supplied or not hmac.compare_digest(supplied, secret):
            send_json(self, 401, {"ok": False})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY_BYTES:
            send_json(self, 413, {"ok": False, "error": "invalid body size"})
            return
        try:
            allowed = allowed_chat_ids()
            if not allowed:
                send_json(self, 503, {"ok": False, "error": "allowed groups not configured"})
                return
            update = json.loads(self.rfile.read(length))
            if not isinstance(update, dict):
                raise ValueError("not an update object")
        except (json.JSONDecodeError, ValueError):
            send_json(self, 400, {"ok": False, "error": "invalid update"})
            return
        except RuntimeError:
            send_json(self, 503, {"ok": False, "error": "invalid configuration"})
            return
        try:
            with connect() as db:
                ensure_schema(db)
                save_update(db, update, allowed)
        except Exception as error:
            print(f"Webhook storage failed: {type(error).__name__}")
            send_json(self, 500, {"ok": False})
            return
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        send_json(self, 405, {"ok": False})

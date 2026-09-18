"""Small HTTP helpers shared by Vercel functions."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os


def authorized(header: str | None, env_name: str) -> bool:
    secret = os.environ.get(env_name)
    return bool(secret and header and hmac.compare_digest(header, f"Bearer {secret}"))


def send_json(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=lambda value: value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else str(value)).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

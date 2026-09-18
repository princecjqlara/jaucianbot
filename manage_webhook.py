"""Inspect or register the Telegram webhook after Vercel deployment."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlsplit

from local_env import load_local_env
from telegram_insights import api_request, require_token


def main() -> None:
    load_local_env()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    register = commands.add_parser("set")
    register.add_argument("url", help="Production HTTPS URL ending in /api/webhook")
    commands.add_parser("delete")
    args = parser.parse_args()
    token = require_token()
    if args.command == "info":
        print(json.dumps(api_request(token, "getWebhookInfo"), indent=2))
    elif args.command == "set":
        parsed = urlsplit(args.url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path != "/api/webhook":
            parser.error("URL must be the production HTTPS URL ending in /api/webhook")
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        if not secret:
            parser.error("TELEGRAM_WEBHOOK_SECRET is required")
        result = api_request(token, "setWebhook", {
            "url": args.url,
            "secret_token": secret,
            "allowed_updates": json.dumps(["message", "edited_message", "my_chat_member"]),
            "max_connections": 10,
        })
        print(json.dumps({"webhook_registered": result, "url": args.url}))
    else:
        result = api_request(token, "deleteWebhook", {"drop_pending_updates": "false"})
        print(json.dumps({"webhook_deleted": result}))


if __name__ == "__main__":
    main()

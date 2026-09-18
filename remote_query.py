"""Query the authenticated Vercel archive from this workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("groups")
    messages = commands.add_parser("messages")
    messages.add_argument("--group", type=int)
    messages.add_argument("--days", type=float, default=7)
    messages.add_argument("--limit", type=int, default=200)
    messages.add_argument("--query")
    schedules = commands.add_parser("schedules")
    schedules.add_argument("--status", choices=["pending", "processing", "sent", "failed", "cancelled"])
    schedules.add_argument("--limit", type=int, default=100)
    schedule_message = commands.add_parser("schedule-message")
    schedule_message.add_argument("--group", type=int, required=True)
    schedule_message.add_argument("--at", required=True, help="ISO timestamp with UTC offset")
    schedule_message.add_argument("--text", required=True)
    schedule_message.add_argument("--repeat-minutes", type=int)
    schedule_message.add_argument("--silent", action="store_true")
    schedule_message.add_argument("--thread", type=int)
    schedule_poll = commands.add_parser("schedule-poll")
    schedule_poll.add_argument("--group", type=int, required=True)
    schedule_poll.add_argument("--at", required=True, help="ISO timestamp with UTC offset")
    schedule_poll.add_argument("--question", required=True)
    schedule_poll.add_argument("--option", action="append", required=True)
    schedule_poll.add_argument("--repeat-minutes", type=int)
    schedule_poll.add_argument("--multiple", action="store_true")
    schedule_poll.add_argument("--public", action="store_true", dest="public_poll")
    schedule_poll.add_argument("--silent", action="store_true")
    schedule_poll.add_argument("--thread", type=int)
    cancel = commands.add_parser("cancel-schedule")
    cancel.add_argument("schedule_id", type=int)
    args = parser.parse_args()
    base = os.environ.get("INSIGHTS_BASE_URL", "").rstrip("/")
    key = os.environ.get("INSIGHTS_API_KEY")
    if not base.startswith("https://") or not key:
        parser.error("Set INSIGHTS_BASE_URL and INSIGHTS_API_KEY in the local environment")
    path = "/api/status"
    method = "GET"
    body = None
    if args.command == "groups":
        path = "/api/groups"
    elif args.command == "messages":
        params = {"days": args.days, "limit": args.limit}
        if args.group is not None:
            params["group"] = args.group
        if args.query:
            params["q"] = args.query
        path = "/api/messages?" + urllib.parse.urlencode(params)
    elif args.command == "schedules":
        params = {"limit": args.limit}
        if args.status:
            params["status"] = args.status
        path = "/api/schedules?" + urllib.parse.urlencode(params)
    elif args.command in {"schedule-message", "schedule-poll"}:
        action = {"disable_notification": args.silent}
        if args.thread is not None:
            action["message_thread_id"] = args.thread
        if args.command == "schedule-message":
            action["text"] = args.text
            action_type = "message"
        else:
            action.update({
                "question": args.question,
                "options": args.option,
                "is_anonymous": not args.public_poll,
                "allows_multiple_answers": args.multiple,
            })
            action_type = "poll"
        body = json.dumps({
            "chat_id": args.group,
            "action_type": action_type,
            "scheduled_for": args.at,
            "repeat_interval_minutes": args.repeat_minutes,
            "payload": action,
        }, ensure_ascii=False).encode("utf-8")
        path = "/api/schedules"
        method = "POST"
    elif args.command == "cancel-schedule":
        path = "/api/schedules?" + urllib.parse.urlencode({"id": args.schedule_id})
        method = "DELETE"
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

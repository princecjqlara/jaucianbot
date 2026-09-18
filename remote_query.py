"""Query the authenticated Vercel archive from this workspace."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    messages = commands.add_parser("messages")
    messages.add_argument("--group", type=int)
    messages.add_argument("--days", type=float, default=7)
    messages.add_argument("--limit", type=int, default=200)
    messages.add_argument("--query")
    args = parser.parse_args()
    base = os.environ.get("INSIGHTS_BASE_URL", "").rstrip("/")
    key = os.environ.get("INSIGHTS_API_KEY")
    if not base.startswith("https://") or not key:
        parser.error("Set INSIGHTS_BASE_URL and INSIGHTS_API_KEY in the local environment")
    path = "/api/status"
    if args.command == "messages":
        params = {"days": args.days, "limit": args.limit}
        if args.group is not None:
            params["group"] = args.group
        if args.query:
            params["q"] = args.query
        path = "/api/messages?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=30) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

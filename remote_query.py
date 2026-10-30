"""Query the authenticated Vercel archive from this workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.error
import urllib.request
import uuid


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("health", help="Read-only public automation health check")
    commands.add_parser("groups")
    commands.add_parser("freebies")
    commands.add_parser("new-clients")
    suno_supply = commands.add_parser("suno-supply", help="Read aggregate Suno contact eligibility and source freshness")
    suno_supply.add_argument("--chatbot", action="store_true", help="Read chatbot settings and recent processing instead of inventory")
    clients = commands.add_parser("clients", help="Read current client availability, editors and handover history")
    clients.add_argument("--group", type=int)
    commands.add_parser("scheduler-status", help="Read database scheduler configuration and recent HTTP results")
    scheduler_setup = commands.add_parser("configure-scheduler", help="Install or repair the database's one-minute dispatcher")
    scheduler_setup.add_argument("--verify-recovery", action="store_true", help="Test watchdog repair inside a rolled-back savepoint")
    commands.add_parser("daily-plan", help="Read-only tomorrow staffing and planned-ad-budget preview")
    commands.add_parser("report-topics", help="Read named topics in DAILY REPORTS")
    commands.add_parser("report-members", help="Read cached Telegram membership verification for capacity reports")
    warnings = commands.add_parser("warnings", help="Read completed-date VEO warnings without publishing")
    warnings.add_argument("--group", type=int)
    warnings.add_argument("--date", help="Completed PHT date, YYYY-MM-DD")
    warnings.add_argument("--check-sources", action="store_true", help="Read source coverage without assessing or publishing warnings")
    surveys = commands.add_parser("refresh-surveys", help="Queue manager-requested links to tracked page-access and tomorrow polls")
    surveys.add_argument("--group", type=int, action="append", required=True)
    surveys.add_argument("--date", required=True, help="Tomorrow's PHT date, YYYY-MM-DD")
    workload = commands.add_parser("workload", help="Read or record dated ads/work supply without sending messages")
    workload.add_argument("--group", type=int, required=True)
    workload.add_argument("--date", required=True, help="PHT work date, YYYY-MM-DD")
    workload.add_argument("--save", action="store_true", help="Save manager-confirmed context; otherwise read only")
    workload.add_argument("--member", type=int, help="Member who actually received work; required for sufficient workload")
    workload.add_argument("--ads", choices=["running", "paused", "unknown"], default="unknown")
    workload.add_argument("--workload", choices=["sufficient", "limited", "none", "unknown"], default="unknown")
    workload.add_argument("--note", help="Required evidence note when saving")
    workload.add_argument("--source-message", type=int, help="Source message ID in this same group")
    commands.add_parser("tomorrow-capacity", help="Read-only VEO average output estimates for tomorrow's actual Active voters")
    commands.add_parser("makata-status", help="Read Makata setup, topic IDs and report subscription status")
    commands.add_parser("makata-connect", help="Connect the fixed Makata report recipient using verified Telegram identity")
    makata_report = commands.add_parser("makata-report", help="Queue a dated Makata report to its fixed private recipient")
    makata_report.add_argument("--date", required=True, help="Philippine report date, YYYY-MM-DD")
    summary = commands.add_parser("daily-summary", help="Read-only Done Deal counts, total and average preview")
    summary.add_argument("--group", type=int)
    summary.add_argument("--date", help="Philippine work date, YYYY-MM-DD")
    replace_reports = commands.add_parser("replace-reports", help="Request replacement of yesterday's bot reports in DAILY REPORTS via the scheduler")
    replace_reports.add_argument("--date", required=True, help="Yesterday's Philippine date, YYYY-MM-DD")
    concerns = commands.add_parser("concerns", help="Read-only daily concerns preview")
    concerns.add_argument("--date", help="Philippine work date, YYYY-MM-DD")
    referrals = commands.add_parser("referrals")
    referrals.add_argument("--audit", action="store_true", help="Read-only report preview using live sales sources")
    commands.add_parser("dispatch", help="Run the shared dispatcher for all approved teams")
    messages = commands.add_parser("messages")
    messages.add_argument("--group", type=int)
    messages.add_argument("--days", type=float, default=7)
    messages.add_argument("--limit", type=int, default=200)
    messages.add_argument("--query")
    workers = commands.add_parser("workers")
    workers.add_argument("--group", type=int, required=True)
    workers.add_argument("--days", type=int, default=14)
    capacity = commands.add_parser("capacity", help="Read VEO output or Suno receipt/delivery plans over completed Philippine days; use --group for Suno")
    capacity.add_argument("--group", type=int)
    capacity.add_argument("--days", type=int, default=14)
    capacity.add_argument("--all-history", action="store_true", help="Backread available exports and live archive")
    schedules = commands.add_parser("schedules")
    schedules.add_argument("--status", choices=["pending", "processing", "sent", "failed", "cancelled"])
    schedules.add_argument("--limit", type=int, default=100)
    schedules.add_argument("--group", type=int)
    schedules.add_argument("--newest", action="store_true", help="Read newest actions first for delivery verification")
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
    elif args.command == "health":
        path = "/api/health"
    elif args.command == "refresh-surveys":
        path = "/api/reports/surveys"
        method = "POST"
        body = json.dumps({"groups": args.group, "work_date": args.date, "request_id": uuid.uuid4().hex}).encode("utf-8")
    elif args.command == "workload":
        path = "/api/reports/workload"
        if args.save:
            if not args.note or (args.workload == "sufficient" and args.member is None):
                parser.error("Saving needs --note; sufficient workload also needs --member")
            method = "POST"
            body = json.dumps({"chat_id": args.group, "work_date": args.date, "user_id": args.member,
                "ads": args.ads, "workload": args.workload, "note": args.note,
                "source_message_id": args.source_message}).encode("utf-8")
        else:
            path += "?" + urllib.parse.urlencode({"group": args.group, "date": args.date})
    elif args.command == "freebies":
        path = "/api/freebies/status"
    elif args.command == "new-clients":
        path = "/api/new-clients/status"
    elif args.command == "suno-supply":
        path = "/api/new-clients/suno-supply" + ("?chatbot=1" if args.chatbot else "")
    elif args.command == "clients":
        path = "/api/clients" + ("?" + urllib.parse.urlencode({"group": args.group}) if args.group is not None else "")
    elif args.command in {"scheduler-status", "configure-scheduler"}:
        path = "/api/scheduler"
        if args.command == "configure-scheduler":
            method = "POST"
            body = json.dumps({"verify_recovery": args.verify_recovery}).encode("utf-8")
    elif args.command == "daily-plan":
        path = "/api/reports/plan"
    elif args.command == "report-topics":
        path = "/api/reports/topics"
    elif args.command == "report-members":
        path = "/api/reports/members"
    elif args.command == "warnings":
        path = "/api/reports/warnings?" + urllib.parse.urlencode({k: v for k, v in
            {"group": args.group, "date": args.date, "check_sources": 1 if args.check_sources else None}.items() if v is not None})
    elif args.command == "tomorrow-capacity":
        path = "/api/workers/capacity?tomorrow=1"
    elif args.command == "makata-status":
        path = "/api/suno/makata"
    elif args.command == "makata-connect":
        path = "/api/suno/makata"
        method = "POST"
        body = b"{}"
    elif args.command == "makata-report":
        path = "/api/suno/makata"
        method = "POST"
        body = json.dumps({"action": "send_report", "date": args.date, "request_id": uuid.uuid4().hex}).encode("utf-8")
    elif args.command == "replace-reports":
        path = "/api/reports/replace"
        method = "POST"
        body = json.dumps({"date": args.date, "request_id": uuid.uuid4().hex}).encode("utf-8")
    elif args.command == "daily-summary":
        params = {k: v for k, v in {"group": args.group, "date": args.date}.items() if v is not None}
        path = "/api/reports/daily-summary?" + urllib.parse.urlencode(params)
    elif args.command == "concerns":
        path = "/api/reports/concerns" + ("?" + urllib.parse.urlencode({"date": args.date}) if args.date else "")
    elif args.command == "referrals":
        path = "/api/referrals/audit" if args.audit else "/api/referrals/status"
    elif args.command == "dispatch":
        path = "/api/cron/dispatch"
    elif args.command == "messages":
        params = {"days": args.days, "limit": args.limit}
        if args.group is not None:
            params["group"] = args.group
        if args.query:
            params["q"] = args.query
        path = "/api/messages?" + urllib.parse.urlencode(params)
    elif args.command == "workers":
        path = "/api/workers/activity?" + urllib.parse.urlencode({
            "group": args.group,
            "days": args.days,
        })
    elif args.command == "capacity":
        path = "/api/workers/capacity?" + urllib.parse.urlencode({
            **({"group": args.group} if args.group is not None else {}), "days": args.days,
            **({"all_history": 1} if args.all_history else {})})
    elif args.command == "schedules":
        params = {"limit": args.limit}
        if args.status:
            params["status"] = args.status
        if args.group is not None:
            params["group"] = args.group
        if args.newest:
            params["newest"] = "1"
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
    try:
        # A cold guarded forecast can take about a minute; cached reads are fast.
        timeout = 240 if args.command in {"tomorrow-capacity", "daily-plan"} else 30
        with urllib.request.urlopen(request, timeout=timeout) as response:
            print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as error:
        with error:
            try:
                detail = json.loads(error.read(20000))
            except (ValueError, UnicodeError):
                detail = {}
        result = {"ok": False, "http_status": error.code}
        if isinstance(detail, dict):
            result.update({name: detail[name] for name in ("error", "restriction", "automation_version") if name in detail})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

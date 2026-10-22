"""Single WSGI entrypoint for the Vercel Python runtime."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
from http import HTTPStatus
from urllib.parse import parse_qs

from cloud_http import authorized
from cloud_store import (
    allowed_chat_ids,
    archive_messages,
    archive_status,
    cancel_scheduled_action,
    claim_scheduled_actions,
    claim_automation_slot,
    create_scheduled_action,
    daily_poll_counts,
    defer_scheduled_action,
    finish_scheduled_action,
    known_chats,
    list_scheduled_actions,
    register_daily_poll,
    save_poll_answer,
    save_update,
)
from daily_automation import GROUPS, AVAILABILITY_GROUPS, DAILY_REPORTS_CHAT_ID, MANILA, TRABAWHO, TRABAWHO_CHAT_ID, SUNO_GROUPS, MAKATA_CHAT_ID, run_due_daily_automation
import hourly_availability
import page_access
import client_volunteers
import client_handoffs
import ready_availability
import client_workflow
import referral_incentives
import database_scheduler
import done_deal_tracking
import team_planning
import concern_reports
import automation_controls
import member_capacity_reports
import suno_capacity
import tomorrow_capacity
import makata_automation
import report_topics
import organized_reports
import member_presence
import availability_reminders
from daily_report_plans import planning_snapshot
from freebie_automation import (
    confirm_freebie_reply, freebie_delivery_allowed, is_freebie_action,
    freebie_status, poll_answer_chat_today, queue_freebie_assignments,
)
from new_client_automation import (
    confirm_new_client_reply, is_new_client_action, new_client_delivery_allowed,
    new_client_status, queue_new_client_assignments,
    new_client_ack_delivery_allowed,
)
from telegram_sender import send_scheduled_action
from worker_activity import worker_activity_report
from suno_store import suno_configured
from trabawho_automation import queue_trabawho_automation, trabawho_poll_work_date
from trabawho_reports import queue_trabawho_daily_report
from trabawho_songs import confirm_song_reply, is_song_notice, queue_song_followups, song_notice_delivery_allowed


MAX_BODY_BYTES = 1_000_000
AUTOMATION_VERSION = "2026-10-22.39"
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
        automation = automation_controls.status(allowed_chat_ids())
    except Exception as error:
        print(f"Status query failed: {type(error).__name__}")
        payload = {"ok": False, "automation_version": AUTOMATION_VERSION}
        if getattr(error, "http_status", None) == 402:
            payload["error"] = "archive_service_restricted"
            payload["restriction"] = getattr(error, "restriction", None) or "unknown"
        return response(start_response, 503, payload)
    return response(start_response, 200, {"ok": True, "groups": groups, "automation_version": AUTOMATION_VERSION,
                                        "automation": automation,
                                        "availability_reminders": {"enabled": availability_reminders.enabled(),
                                            "exclude_admins": True,
                                            "hours_pht": list(availability_reminders.HOURS)},
                                        "done_deal_tracking": done_deal_tracking.mode()})


def health_route(environ, start_response):
    try:
        allowed = allowed_chat_ids()
        archive_status(allowed)
        if os.environ.get("SUPABASE_DB_URL"):
            scheduler = database_scheduler.scheduler_status(allowed | makata_automation.delivery_ids(automation_controls.enabled_chat_ids(allowed)))
            if not scheduler["healthy"]:
                return response(start_response, 503, {"ok": False, "error": "automation_unhealthy"})
    except Exception as error:
        print(f"Health query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True})


def scheduler_route(environ, start_response, method: str):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        allowed = allowed_chat_ids()
        if method == "POST":
            body = read_json_body(environ) if int(environ.get("CONTENT_LENGTH") or 0) else {}
            verify = body.get("verify_recovery", False)
            if not isinstance(verify, bool):
                return response(start_response, 400, {"ok": False, "error": "verify_recovery must be boolean"})
            state = database_scheduler.configure_scheduler(allowed, verify_recovery=verify)
        else:
            state = database_scheduler.scheduler_status(allowed | makata_automation.delivery_ids(automation_controls.enabled_chat_ids(allowed)))
        return response(start_response, 200, {"ok": True, "scheduler": state})
    except ValueError:
        return response(start_response, 400, {"ok": False, "error": "invalid scheduler request"})
    except database_scheduler.SchedulerError as error:
        return response(start_response, 503, {"ok": False, "error": str(error)})
    except Exception as error:
        print(f"Scheduler maintenance failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "error": "scheduler_maintenance_failed"})


def groups_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        groups = known_chats(allowed_chat_ids())
    except Exception as error:
        print(f"Group discovery query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, "groups": groups,
                                          "approved_chat_ids": sorted(allowed_chat_ids())})


def makata_status_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    if MAKATA_CHAT_ID not in allowed_chat_ids():
        return response(start_response, 403, {"ok": False})
    try:
        if environ.get("REQUEST_METHOD", "GET").upper() == "POST":
            body = read_json_body(environ) if int(environ.get("CONTENT_LENGTH") or 0) else {}
            if body.get("action") == "send_report":
                day = dt.date.fromisoformat(str(body.get("date") or ""))
                return response(start_response, 200, makata_automation.queue_requested_report(
                    day, dt.datetime.now(dt.timezone.utc), allowed_chat_ids(), str(body.get("request_id") or "")))
            if body.get("action") not in (None, "connect"):
                raise ValueError("invalid Makata action")
            return response(start_response, 200, makata_automation.connect_recipient(
                allowed_chat_ids(), dt.datetime.now(dt.timezone.utc)))
        return response(start_response, 200, makata_automation.status(allowed_chat_ids()))
    except ValueError:
        return response(start_response, 400, {"ok": False, "error": "invalid Makata report request"})
    except Exception as error:
        print(f"Makata status failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "error": "makata_status_unavailable"})


def freebies_status_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        groups = freebie_status(allowed_chat_ids(), dt.datetime.now(dt.timezone.utc))
    except Exception as error:
        print(f"Freebie status failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, "groups": groups})


def new_clients_status_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        allowed = allowed_chat_ids()
        active = automation_controls.enabled_chat_ids(allowed)
        now = dt.datetime.now(dt.timezone.utc)
        groups = new_client_status(allowed.intersection(GROUPS), now)
        if allowed.intersection(SUNO_GROUPS):
            if suno_configured():
                try:
                    makata_automation.hydrate(allowed)
                    groups.extend(new_client_status(allowed.intersection(SUNO_GROUPS), now))
                except Exception as error:
                    print(f"Suno status failed: {type(error).__name__}")
                    groups.append({"team": TRABAWHO["name"], "chat_id": TRABAWHO_CHAT_ID,
                                   "configured": True, "ok": False, "error": "suno_source_unavailable"})
            else:
                groups.append({"team": TRABAWHO["name"], "chat_id": TRABAWHO_CHAT_ID,
                               "configured": False, "ok": False, "error": "suno_mapping_or_credentials_missing"})
    except Exception as error:
        print(f"New-client status failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    for group in groups:
        group["automation_enabled"] = int(group.get("chat_id", 0)) in active
        if group.get("chat_id") is not None and not group["automation_enabled"]:
            group["queue_state"] = "paused_by_manager"
    return response(start_response, 200, {"ok": True, "groups": groups})


def suno_supply_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    if TRABAWHO_CHAT_ID not in allowed_chat_ids():
        return response(start_response, 404, {"ok": False})
    try:
        from suno_store import suno_supply_status, suno_chatbot_status
        if parse_qs(environ.get("QUERY_STRING", "")).get("chatbot") == ["1"]:
            return response(start_response, 200, {"ok": True, "chatbot": suno_chatbot_status()})
        return response(start_response, 200, {"ok": True, "supply": suno_supply_status()})
    except Exception as error:
        print(f"Suno supply query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "error": "suno_source_unavailable"})


def client_directory_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    allowed = allowed_chat_ids().intersection(AVAILABILITY_GROUPS)
    args = parse_qs(environ.get("QUERY_STRING", ""))
    if args.get("group"):
        try:
            group = int(args["group"][0])
        except ValueError:
            return response(start_response, 400, {"ok": False, "error": "invalid group"})
        if group not in allowed:
            return response(start_response, 404, {"ok": False})
        allowed = {group}
    snapshots = []
    for chat in sorted(allowed):
        try:
            snapshots.append(client_workflow.directory(chat, dt.datetime.now(dt.timezone.utc)))
        except Exception as error:
            print(f"Client directory read failed: {type(error).__name__}")
            snapshots.append({"chat_id": chat, "team": AVAILABILITY_GROUPS[chat]["name"], "ok": False, "error": "client_source_unavailable"})
    return response(start_response, 200, {"ok": all(s.get("ok", True) for s in snapshots), "groups": snapshots})


def capacity_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    args = parse_qs(environ.get("QUERY_STRING", ""))
    if args.get("tomorrow") == ["1"]:
        try:
            return response(start_response, 200, tomorrow_capacity.snapshot(dt.datetime.now(dt.timezone.utc), allowed_chat_ids()))
        except PermissionError:
            return response(start_response, 403, {"ok": False})
        except Exception as error:
            print(f"Tomorrow capacity source failed: {type(error).__name__}")
            return response(start_response, 503, {"ok": False, "error": "tomorrow_capacity_source_unavailable"})
    teams = allowed_chat_ids().intersection(GROUPS)
    try:
        days = int(args.get("days", ["14" if report_topics.enabled() else "30"])[0])
        if not 1 <= days <= 366:
            raise ValueError("invalid period")
        if args.get("group"):
            group = int(args["group"][0])
            if group in SUNO_GROUPS and group in allowed_chat_ids():
                return response(start_response, 200, suno_capacity.report(dt.datetime.now(dt.timezone.utc),
                    days=days, all_history=args.get("all_history") == ["1"], **({"chat_id": group} if group != TRABAWHO_CHAT_ID else {})))
            if group not in teams:
                return response(start_response, 404, {"ok": False})
            teams = {group}
    except ValueError:
        return response(start_response, 400, {"ok": False, "error": "invalid capacity query"})
    except Exception as error:
        print(f"Suno capacity source failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "error": "suno_capacity_source_unavailable"})
    try:
        import member_capacity
        from concurrent.futures import ThreadPoolExecutor
        now = dt.datetime.now(dt.timezone.utc)
        if args.get("all_history") == ["1"]:
            return response(start_response, 200, member_capacity.compact(member_capacity.backread(teams, now)))
        with ThreadPoolExecutor(max_workers=max(1, len(teams))) as pool:
            reports = list(pool.map(lambda chat: member_capacity.report(chat, days, now), sorted(teams)))
        return response(start_response, 200, {"ok": True, "read_only": True, "completed_days_only": True,
            "groups": reports, "combined": member_capacity.combined(reports), "definitions": member_capacity.definitions()})
    except Exception as error:
        print(f"Capacity report failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "error": "capacity_source_unavailable"})


def referrals_status_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        data = referral_incentives.status(allowed_chat_ids(), dt.datetime.now(dt.timezone.utc))
    except Exception as error:
        print(f"Referral status failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, **data})

def referrals_audit_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    allowed = allowed_chat_ids()
    if not referral_incentives.enabled() or referral_incentives.RECRUITS_CHAT not in allowed:
        return response(start_response, 403, {"ok": False})
    try:
        now = dt.datetime.now(dt.timezone.utc)
        date = now.astimezone(MANILA).date()
        preview = referral_incentives.build_report(date, now, allowed, lock=False)
        return response(start_response, 200, {
            "ok": True, "read_only": True, "checked_at": now.isoformat(), "work_date": date.isoformat(),
            "sales_chat_ids": sorted(referral_incentives.WORK_TEAMS.intersection(allowed)),
            "report_preview": preview,
        })
    except Exception as error:
        print(f"Referral audit failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})


def daily_summary_route(environ, start_response):
    """Preview observed deals without queueing reports, payment or member actions."""
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    allowed = allowed_chat_ids()
    if DAILY_REPORTS_CHAT_ID not in allowed:
        return response(start_response, 403, {"ok": False})
    args = parse_qs(environ.get("QUERY_STRING", ""))
    now = dt.datetime.now(dt.timezone.utc)
    try:
        date = dt.date.fromisoformat(args["date"][0]) if args.get("date") else now.astimezone(MANILA).date()
        chat = int(args["group"][0]) if args.get("group") else None
    except (ValueError, IndexError):
        return response(start_response, 400, {"ok": False})
    teams = allowed.intersection(GROUPS)
    if chat is not None:
        if chat not in teams:
            return response(start_response, 404, {"ok": False})
        teams = {chat}
    if not done_deal_tracking.summary_enabled():
        return response(start_response, 409, {"ok": False, "error": "done_deal_summary_disabled"})
    try:
        import done_deal_summary
        from daily_automation import messages_for_day
        reports = []
        for chat_id in sorted(teams):
            rows, capped = messages_for_day(chat_id, date)
            summary = done_deal_summary.summarize(rows, GROUPS[chat_id], capped=capped)
            reports.append({"chat_id": chat_id, "team": GROUPS[chat_id]["name"],
                            "done_deals": summary["count"], "recorded_total": str(summary["total"]),
                            "average_amount": str(summary["average"]) if summary["average"] is not None else None,
                            "posts_needing_review": summary["review_count"], "partial_coverage": capped,
                            "report_preview": done_deal_summary.render(summary)})
        return response(start_response, 200, {"ok": True, "read_only": True, "work_date": date.isoformat(), "groups": reports})
    except Exception as error:
        print(f"Done Deal summary failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})


def daily_plan_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    try:
        data = planning_snapshot(dt.datetime.now(dt.timezone.utc), allowed_chat_ids())
    except PermissionError:
        return response(start_response, 403, {"ok": False})
    except Exception as error:
        print(f"Daily plan query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, **data})


def concerns_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    now = dt.datetime.now(dt.timezone.utc)
    args = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        date = dt.date.fromisoformat(args["date"][0]) if args.get("date") else now.astimezone(MANILA).date()
        data = concern_reports.snapshot(date, allowed_chat_ids(), now)
    except PermissionError:
        return response(start_response, 403, {"ok": False})
    except ValueError:
        return response(start_response, 400, {"ok": False, "error": "invalid concerns date"})
    except Exception as error:
        print(f"Concerns preview failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, **data})


def report_topics_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    if DAILY_REPORTS_CHAT_ID not in allowed_chat_ids():
        return response(start_response, 403, {"ok": False})
    try:
        return response(start_response, 200, {"ok": True, "read_only": True, "topics": report_topics.topics()})
    except Exception as error:
        print(f"Report topics query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})


def report_members_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    approved = allowed_chat_ids()
    if DAILY_REPORTS_CHAT_ID not in approved:
        return response(start_response, 403, {"ok": False})
    try:
        return response(start_response, 200, member_presence.snapshot(approved))
    except Exception as error:
        print(f"Report membership query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})


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


def workers_activity_route(environ, start_response):
    if not authorized(environ.get("HTTP_AUTHORIZATION"), "INSIGHTS_API_KEY"):
        return response(start_response, 401, {"ok": False})
    args = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        group = int(args["group"][0])
        days = int(args.get("days", ["14"])[0])
        if not 1 <= days <= 31:
            raise ValueError("days must be between 1 and 31")
    except (KeyError, ValueError, IndexError):
        return response(start_response, 400, {"ok": False, "error": "valid group and days are required"})
    allowed = allowed_chat_ids()
    if group not in allowed:
        return response(start_response, 403, {"ok": False})
    try:
        report = worker_activity_report(group, days, dt.datetime.now(dt.timezone.utc))
    except KeyError:
        return response(start_response, 400, {"ok": False, "error": "group has no worker automation configuration"})
    except Exception as error:
        print(f"Worker activity query failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False})
    return response(start_response, 200, {"ok": True, "activity": report})


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
    if not isinstance(action_type, str) or action_type not in {"message", "poll"} or not isinstance(raw_action, dict):
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
            if "group" in args:
                group = int(args["group"][0])
                if group not in allowed:
                    return response(start_response, 403, {"ok": False, "error": "group is not approved"})
                allowed = {group}
            newest = args.get("newest", ["0"])[0]
            if newest not in {"0", "1"}:
                raise ValueError("invalid newest flag")
            rows = list_scheduled_actions(allowed, status=status, limit=limit, **({"newest": True} if newest == "1" else {}))
            return response(start_response, 200, {"ok": True, "schedules": rows})
        if method == "POST":
            chat_id, action_type, action_payload, scheduled_for, repeat = validate_action(read_json_body(environ))
            if chat_id not in allowed:
                return response(start_response, 403, {"ok": False, "error": "group is not approved"})
            if not automation_controls.chat_enabled(chat_id):
                return response(start_response, 409, {"ok": False, "error": "automation is paused for this group"})
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


def deliver_due_actions(allowed: set[int], *, limit: int = 10) -> tuple[int, int, int]:
    # Deferred reminders should not use every slot ahead of a new client.
    # Bound both sends and scanning so a backlog cannot extend this indefinitely.
    allowed = automation_controls.enabled_chat_ids(allowed)
    allowed = makata_automation.delivery_ids(allowed)
    if not allowed:
        return 0, 0, 0
    processed = sent = failed = 0
    scan_limit = max(limit, 1) * 4
    while sent + failed < limit and processed < scan_limit:
        batch_limit = min(limit - sent - failed, scan_limit - processed, 25)
        actions = claim_scheduled_actions(allowed, limit=batch_limit)
        if not actions:
            break
        batch_processed, batch_sent, batch_failed = _deliver_claimed_actions(actions, allowed)
        processed += batch_processed
        sent += batch_sent
        failed += batch_failed
        if len(actions) < batch_limit:
            break
    return processed, sent, failed


def _deliver_claimed_actions(actions: list[dict], allowed: set[int]) -> tuple[int, int, int]:
    allowed = makata_automation.delivery_ids(allowed)
    sent = 0
    failed = 0
    report_cache = {}
    for action in actions:
        try:
            if int(action["chat_id"]) not in allowed or not automation_controls.chat_enabled(int(action["chat_id"])):
                defer_scheduled_action(action)
                continue
            if not automation_controls.new_client_delivery_allowed(action):
                if not defer_scheduled_action(action):
                    raise RuntimeError("disabled new-client action was not deferred")
                continue
            now = dt.datetime.now(dt.timezone.utc)
            if not availability_reminders.prepare_delivery(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("obsolete availability reminder was not finalized")
                continue
            if not report_topics.delivery_allowed(action):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("obsolete public report was not finalized")
                continue
            if not organized_reports.prepare_delivery(action, now, report_cache):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("staffing check was not finalized")
                continue
            from freebie_automation import retire_stale_delivery
            retired = retire_stale_delivery(action, now)
            if retired is not None:
                if not retired:
                    raise RuntimeError("stale task notification was not retired")
                continue
            if not makata_automation.private_delivery_allowed(action):
                action["repeat_interval_minutes"] = None
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("invalid private report action was not finalized")
                continue
            prior_topic = makata_automation.setup_delivery(action)
            if prior_topic:
                if not finish_scheduled_action(action["id"], success=True,
                        telegram_message_id=int(prior_topic["message_id"]), claim=action):
                    raise RuntimeError("created Makata topic was not finalized")
                continue
            if not member_capacity_reports.delivery_allowed(action, now) or not suno_capacity.delivery_allowed(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("obsolete capacity report was not finalized")
                continue
            if not done_deal_tracking.delivery_allowed(action):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("paused sale output was not finalized")
                continue
            if not tomorrow_capacity.prepare_delivery(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("tomorrow capacity check was not finalized")
                continue
            if not client_workflow.prepare_delivery(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("obsolete client update was not finalized")
                continue
            if not team_planning.prepare_delivery(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("obsolete team plan was not finalized")
                continue
            if not client_handoffs.delivery_allowed(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("closed handoff notice was not finalized")
                continue
            if int(action["chat_id"]) == referral_incentives.RECRUITS_CHAT and (
                action.get("dedupe_key") == "referral-rules:v1"
                or action["payload"].get("referral_poll") and (
                    action["payload"].get("referral_privacy_version", 1) < 3
                    or action["payload"].get("message_thread_id") != referral_incentives.RECRUITS_THREAD
                    or action["payload"].get("options") != list(referral_incentives.RECRUITERS))
                or (action.get("dedupe_key") or "").startswith("referral-instructions:")
                   and action["payload"].get("message_thread_id") != referral_incentives.RECRUITS_THREAD
            ):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("obsolete recruiter disclosure was not suppressed")
                continue
            if action["payload"].get("referral_report_date"):
                referral_incentives.prepare_report(action, now, allowed)
            if action["payload"].get("referral_poll") and referral_incentives.poll_registered(int(action["id"])):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("registered referral poll was not finalized")
                continue
            if action["payload"].get("new_client_ack_assignment_id") is not None and not new_client_ack_delivery_allowed(action):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("superseded assignment acknowledgment was not finalized")
                continue
            if action["payload"].get("volunteer_parent_id") is not None:
                state = client_volunteers.delivery_state(action, now)
                if state == "defer":
                    defer_scheduled_action(action)
                    continue
                if state == "suppress":
                    if not finish_scheduled_action(action["id"], success=True, claim=action):
                        raise RuntimeError("closed volunteer notice was not finalized")
                    continue
            if is_song_notice(action) and not song_notice_delivery_allowed(action, now):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("suppressed song reminder was not finalized")
                continue
            if is_freebie_action(action) and not freebie_delivery_allowed(action, now):
                defer_scheduled_action(action)
                continue
            if is_new_client_action(action) and not new_client_delivery_allowed(action, now):
                defer_scheduled_action(action)
                continue
            daily_poll_date = action["payload"].get("daily_poll_date")
            access_version = action["payload"].get("page_access_version")
            if action["action_type"] == "poll" and access_version:
                if access_version != page_access.version(int(action["chat_id"])) or page_access.poll_record(int(action["chat_id"]), int(action["payload"].get("page_access_part") or 0)):
                    if not finish_scheduled_action(action["id"], success=True, claim=action):
                        raise RuntimeError("registered page-access poll was not finalized")
                    continue
            is_daily_poll = action["action_type"] == "poll" and bool(daily_poll_date)
            hourly_date = action["payload"].get("hourly_poll_date")
            if hourly_date and not hourly_availability.enabled_for_chat(int(action["chat_id"]), dt.date.fromisoformat(hourly_date)):
                finish_scheduled_action(action["id"], success=True, claim=action)
                continue
            if hourly_date and hourly_availability.registered(int(action["chat_id"]), hourly_date, action["payload"]["hourly_poll_part"]):
                if not finish_scheduled_action(action["id"], success=True, claim=action):
                    raise RuntimeError("registered hourly poll was not finalized")
                continue
            if is_daily_poll:
                work_date = dt.date.fromisoformat(daily_poll_date)
                if hourly_availability.enabled_for_chat(int(action["chat_id"]), work_date):
                    finish_scheduled_action(action["id"], success=True, claim=action)
                    continue
                existing = daily_poll_counts({int(action["chat_id"])}, work_date).get(int(action["chat_id"]))
                if existing:
                    finish_scheduled_action(action["id"], success=True, claim=action)
                    continue
            paced = report_topics.defer_burst(action, dt.datetime.now(dt.timezone.utc))
            if paced is not None:
                if not paced:
                    raise RuntimeError("paced report was not deferred")
                continue
            message = send_scheduled_action(action)
            message_id = int(message["message_id"])
            makata_automation.record_topic(action, message, now)
            if access_version and not page_access.register_poll(action, message, now):
                raise RuntimeError("page-access poll was not registered")
            if message.get("date") and not action["payload"].get("_first_delivery_at"):
                action["payload"]["_first_delivery_at"] = dt.datetime.fromtimestamp(message["date"], dt.timezone.utc).isoformat()
            if hourly_date and not hourly_availability.register_poll(action, message, now):
                raise RuntimeError("hourly poll was not registered")
            if action["payload"].get("referral_poll") and not referral_incentives.register_poll(action, message, now):
                raise RuntimeError("referral poll was not registered")
            if is_daily_poll:
                if not register_daily_poll(
                    poll_id=message["poll"]["id"],
                    chat_id=int(action["chat_id"]),
                    thread_id=int(action["payload"]["message_thread_id"]),
                    work_date=work_date,
                    telegram_message_id=message_id,
                ):
                    raise RuntimeError("daily poll was not registered")
            if not finish_scheduled_action(action["id"], success=True, telegram_message_id=message_id, claim=action):
                raise RuntimeError("schedule was not finalized")
            sent += 1
            try:
                if message.get("_telegram_operation") == "editMessageText" and message.get("chat"):
                    save_update({"edited_message": message}, allowed)
                elif not message.get("_telegram_operation"):
                    save_update({"message": message}, allowed)
            except Exception as archive_error:
                print(f"Sent message archive failed: {type(archive_error).__name__}")
        except Exception as error:
            failed += 1
            print(f"Scheduled delivery failed: {type(error).__name__}")
            try:
                finish_scheduled_action(action["id"], success=False, error=str(error), claim=action)
            except Exception as finish_error:
                print(f"Schedule failure update failed: {type(finish_error).__name__}")
    return len(actions), sent, failed


def dispatch_route(environ, start_response):
    queued = 0
    queue_errors = []
    try:
        approved = allowed_chat_ids()
        allowed = automation_controls.enabled_chat_ids(approved)
        try:
            makata_automation.hydrate(approved)
            queued += makata_automation.queue_setup(dt.datetime.now(dt.timezone.utc), approved)
            queued += makata_automation.queue_reports(dt.datetime.now(dt.timezone.utc), approved)
        except Exception as setup_error:
            queue_errors.append("makata_setup_or_reports")
            print(f"Makata setup/report queue failed: {type(setup_error).__name__}")
        try:
            if not report_topics.enabled():
                queued += member_capacity_reports.queue_automation(dt.datetime.now(dt.timezone.utc), approved)
        except Exception as capacity_error:
            queue_errors.append("member_capacity")
            print(f"Daily capacity report failed: {type(capacity_error).__name__}")
        try:
            if not report_topics.enabled():
                queued += suno_capacity.queue_automation(dt.datetime.now(dt.timezone.utc), approved)
        except Exception as capacity_error:
            queue_errors.append("suno_capacity")
            print(f"Suno capacity report failed: {type(capacity_error).__name__}")
        try:
            if not report_topics.enabled():
                queued += tomorrow_capacity.queue_automation(dt.datetime.now(dt.timezone.utc), approved)
        except Exception as capacity_error:
            queue_errors.append("tomorrow_capacity")
            print(f"Tomorrow capacity report failed: {type(capacity_error).__name__}")
        try:
            if not report_topics.enabled():
                queued += concern_reports.queue_automation(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception as concerns_error:
            queue_errors.append("concerns")
            print(f"Concerns queue failed: {type(concerns_error).__name__}")
        try:
            queued += page_access.queue_veo_surveys(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception as page_error:
            queue_errors.append("veo_page_access")
            print(f"VEO page-access survey queue failed: {type(page_error).__name__}")
        try:
            queued += organized_reports.queue_automation(dt.datetime.now(dt.timezone.utc), approved)
        except Exception as report_error:
            queue_errors.append("organized_reports")
            print(f"Organized reports queue failed: {type(report_error).__name__}")
        if os.environ.get("SUPABASE_DB_URL"):
            try:
                if claim_automation_slot(approved, dt.datetime.now(dt.timezone.utc),
                                         workflow="scheduler-guard", interval_seconds=300):
                    database_scheduler.ensure_scheduler(approved)
            except Exception as guard_error:
                queue_errors.append("scheduler_guard")
                print(f"Scheduler guard failed: {type(guard_error).__name__}")
        try:
            queued += referral_incentives.queue_automation(dt.datetime.now(dt.timezone.utc), allowed)
            queued += referral_incentives.queue_share_recovery(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception as referral_error:
            queue_errors.append("referrals")
            print(f"Referral queue failed: {type(referral_error).__name__}")
        if os.environ.get("HOURLY_AVAILABILITY_START_DATE"):
            try:
                now = dt.datetime.now(dt.timezone.utc)
                if claim_automation_slot(allowed.intersection(AVAILABILITY_GROUPS), now, workflow="hourly-polls", interval_seconds=300):
                    today = now.astimezone(MANILA).date()
                    for work_date in (today, today + dt.timedelta(days=1)):
                        queued += hourly_availability.queue_polls(work_date, now, AVAILABILITY_GROUPS, allowed)
            except Exception as hourly_error:
                queue_errors.append("hourly_polls")
                print(f"Hourly poll queue failed: {type(hourly_error).__name__}")
        try:
            queued += automation_controls.queue_mode_notices(dt.datetime.now(dt.timezone.utc), allowed)
            queued += run_due_daily_automation(dt.datetime.now(dt.timezone.utc), allowed - set(SUNO_GROUPS), planning_allowed=allowed)
        except Exception as daily_error:
            queue_errors.append("daily")
            print(f"Daily queue failed: {type(daily_error).__name__}")
        try:
            queued += availability_reminders.queue(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception as reminder_error:
            queue_errors.append("availability_reminders")
            print(f"Availability reminder queue failed: {type(reminder_error).__name__}")
        if TRABAWHO_CHAT_ID in allowed:
            try:
                queued += queue_trabawho_automation(dt.datetime.now(dt.timezone.utc), allowed)
            except Exception as planning_error:
                queue_errors.append("trabawho_plan")
                print(f"Trabawho planning failed: {type(planning_error).__name__}")
            try:
                now = dt.datetime.now(dt.timezone.utc)
                if claim_automation_slot({TRABAWHO_CHAT_ID}, now, workflow="trabawho-songs", interval_seconds=60):
                    queued += queue_song_followups(now, allowed)
            except Exception as song_error:
                queue_errors.append("trabawho_songs")
                print(f"Trabawho song queue failed: {type(song_error).__name__}")
            try:
                queued += queue_trabawho_daily_report(dt.datetime.now(dt.timezone.utc), allowed)
            except Exception as report_error:
                queue_errors.append("trabawho_report")
                print(f"Trabawho report queue failed: {type(report_error).__name__}")
        assignment_groups = allowed.intersection(GROUPS)
        if os.environ.get("CRM_SUPABASE_SERVICE_ROLE_KEY"):
            now = dt.datetime.now(dt.timezone.utc)
            if not assignment_groups or claim_automation_slot(
                assignment_groups, now, workflow="new-client", interval_seconds=60,
            ):
                try:
                    queued += queue_new_client_assignments(now, allowed - set(SUNO_GROUPS))
                except Exception as assignment_error:
                    queue_errors.append("new_client")
                    import traceback
                    frames = traceback.extract_tb(assignment_error.__traceback__)
                    location = " -> ".join(f"{os.path.basename(frame.filename)}:{frame.lineno}" for frame in frames)
                    # Database wrappers expose only SQLSTATE, never raw queries or credentials.
                    safe_detail = str(assignment_error) if re.fullmatch(r"Archive PostgreSQL error \([A-Za-z0-9]+\)", str(assignment_error)) else ""
                    print(f"New-client queue failed: {type(assignment_error).__name__} {safe_detail} [{location}]")
            if not assignment_groups or claim_automation_slot(assignment_groups, now):
                try:
                    queued += queue_freebie_assignments(now, allowed)
                except Exception as freebie_error:
                    queue_errors.append("freebie")
                    print(f"Freebie queue failed: {type(freebie_error).__name__}")
        if allowed.intersection(SUNO_GROUPS) and suno_configured():
            try:
                now = dt.datetime.now(dt.timezone.utc)
                if claim_automation_slot(allowed.intersection(SUNO_GROUPS), now, workflow="new-client:suno", interval_seconds=60):
                    queued += queue_new_client_assignments(now, allowed.intersection(SUNO_GROUPS))
            except Exception as suno_error:
                queue_errors.append("suno_new_client")
                print(f"Suno assignment queue failed: {type(suno_error).__name__}")
        try:
            if os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_DB_URL"):
                client_workflow.maintain(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception as workflow_error:
            queue_errors.append("client_directory")
            print(f"Client directory queue failed: {type(workflow_error).__name__}")
        processed, sent, failed = deliver_due_actions(allowed, limit=25)
    except Exception as error:
        print(f"Schedule claim failed: {type(error).__name__}")
        return response(start_response, 503, {"ok": False, "queued": 0, "processed": 0, "sent": 0, "failed": 0})
    status = 502 if failed else 503 if queue_errors else 200
    return response(start_response, status, {
        "ok": failed == 0 and not queue_errors,
        "queued": queued,
        "processed": processed,
        "sent": sent,
        "failed": failed,
        **({"queue_errors": queue_errors} if queue_errors else {}),
    })


def webhook_route(environ, start_response):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    supplied = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN")
    if not secret:
        return response(start_response, 503, {"ok": False, "error": "webhook not configured"})
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8")):
        return response(start_response, 401, {"ok": False})
    try:
        allowed = allowed_chat_ids()
        if not allowed:
            return response(start_response, 503, {"ok": False, "error": "allowed groups not configured"})
        update = read_json_body(environ)
        active = automation_controls.enabled_chat_ids(allowed)
        makata_automation.hydrate(allowed)
        if makata_automation.register_private(update, allowed, dt.datetime.now(dt.timezone.utc)):
            return response(start_response, 200, {"ok": True})
    except ValueError:
        return response(start_response, 400, {"ok": False, "error": "invalid update"})
    except RuntimeError:
        return response(start_response, 503, {"ok": False, "error": "invalid configuration"})
    if "callback_query" in update:
        callback_chat = ((update.get("callback_query") or {}).get("message") or {}).get("chat", {}).get("id")
        if callback_chat not in active:
            return response(start_response, 200, {"ok": True})
        try:
            result = client_workflow.handle(update, active, dt.datetime.now(dt.timezone.utc))
            if ((update.get("callback_query") or {}).get("data") == "cw:ready"
                    and result == "Availability updated." and automation_controls.new_clients_enabled(callback_chat)):
                queue_new_client_assignments(dt.datetime.now(dt.timezone.utc), {callback_chat})
            # Telegram executes this callback acknowledgment from the webhook
            # response. All group messages stay in the scheduled outbox.
            return response(start_response, 200, {"method": "answerCallbackQuery",
                "callback_query_id": update["callback_query"].get("id"),
                "text": (result or "Use these buttons in your approved team's NEW CLIENTS topic.")[:200],
                "show_alert": True})
        except Exception as error:
            import traceback
            callback = update.get("callback_query") or {}
            message = callback.get("message") or {}
            frames = traceback.extract_tb(error.__traceback__)
            location = " -> ".join(f"{os.path.basename(frame.filename)}:{frame.lineno}" for frame in frames)
            command = str(callback.get("data") or "").split(":")[1:2]
            command = command[0] if command and re.fullmatch(r"[a-z_]{1,32}", command[0]) else "unknown"
            print(f"Client button failed: {type(error).__name__} chat={(message.get('chat') or {}).get('id')} message={message.get('message_id')} command={command} [{location}]")
            return response(start_response, 500, {"ok": False})
    try:
        if "poll_answer" in update:
            referral_stored = referral_incentives.save_answer(update, active)
            stored = save_poll_answer(update, allowed) if referral_stored is None else referral_stored
        else:
            stored = save_update(update, allowed)
            if stored:
                referral_incentives.observe_join(update, active, dt.datetime.now(dt.timezone.utc))
    except Exception as error:
        print(f"Webhook storage failed: {type(error).__name__}")
        return response(start_response, 500, {"ok": False})
    allowed = active
    message_chat = (update.get("message") or update.get("edited_message") or {}).get("chat", {}).get("id")
    if message_chat is not None and message_chat not in allowed:
        return response(start_response, 200, {"ok": True})
    if stored:
        try:
            if "poll_answer" in update:
                access_poll = page_access.context(update["poll_answer"].get("poll_id", ""), allowed)
                if access_poll:
                    chat_id = int(access_poll["chat_id"])
                    queue_new_client_assignments(dt.datetime.now(dt.timezone.utc), {chat_id})
                    return response(start_response, 200, {"ok": True})
            if client_workflow.handle(update, allowed, dt.datetime.now(dt.timezone.utc)) is not None:
                return response(start_response, 200, {"ok": True})
            ready_chat = ready_availability.handle(update, allowed, dt.datetime.now(dt.timezone.utc))
            if ready_chat is not None:
                if ready_chat not in SUNO_GROUPS or suno_configured():
                    queue_new_client_assignments(dt.datetime.now(dt.timezone.utc), {ready_chat})
                return response(start_response, 200, {"ok": True})
        except Exception as error:
            print(f"Ready queue update failed: {type(error).__name__}")
            return response(start_response, 500, {"ok": False})
    if stored and MAKATA_CHAT_ID in active:
        message = update.get("message") or update.get("edited_message") or {}
        if (message.get("chat") or {}).get("id") == MAKATA_CHAT_ID:
            now = dt.datetime.now(dt.timezone.utc)
            if confirm_song_reply(update, active, now):
                return response(start_response, 200, {"ok": True})
            if suno_configured():
                confirm_new_client_reply(update, {MAKATA_CHAT_ID})
            return response(start_response, 200, {"ok": True})
    if stored and TRABAWHO_CHAT_ID in allowed:
        handled_trabawho = False
        try:
            now = dt.datetime.now(dt.timezone.utc)
            if "poll_answer" in update:
                work_date = trabawho_poll_work_date(update["poll_answer"].get("poll_id", ""), now, allowed)
                if work_date is not None:
                    handled_trabawho = True
                    queued = queue_trabawho_automation(now, {TRABAWHO_CHAT_ID})
                    if work_date == now.astimezone(MANILA).date() and suno_configured():
                        queued += queue_new_client_assignments(now, {TRABAWHO_CHAT_ID})
                    if queued:
                        deliver_due_actions({TRABAWHO_CHAT_ID}, limit=5)
                    return response(start_response, 200, {"ok": True})
            else:
                message = update.get("message") or update.get("edited_message") or {}
                if (message.get("chat") or {}).get("id") == TRABAWHO_CHAT_ID:
                    handled_trabawho = True
                    if confirm_song_reply(update, allowed, now):
                        deliver_due_actions({TRABAWHO_CHAT_ID}, limit=5)
                    elif suno_configured() and confirm_new_client_reply(update, allowed):
                        queue_song_followups(now, {TRABAWHO_CHAT_ID})
                        deliver_due_actions({TRABAWHO_CHAT_ID}, limit=5)
                    return response(start_response, 200, {"ok": True})
        except Exception as error:
            print(f"Trabawho webhook automation delayed: {type(error).__name__}")
            if handled_trabawho:
                return response(start_response, 200, {"ok": True})
    if stored and os.environ.get("CRM_SUPABASE_SERVICE_ROLE_KEY"):
        try:
            if "poll_answer" in update:
                chat_id = poll_answer_chat_today(update["poll_answer"].get("poll_id", ""), allowed, dt.datetime.now(dt.timezone.utc))
                queued = 0
                if chat_id:
                    queued += queue_freebie_assignments(dt.datetime.now(dt.timezone.utc), {chat_id})
                    queued += queue_new_client_assignments(dt.datetime.now(dt.timezone.utc), {chat_id})
                if chat_id and queued:
                    deliver_due_actions({chat_id}, limit=5)
            elif confirm_new_client_reply(update, allowed) or confirm_freebie_reply(update, allowed):
                chat_id = (update.get("message") or update.get("edited_message") or {}).get("chat", {}).get("id")
                deliver_due_actions({chat_id}, limit=5)
        except Exception as error:
            print(f"Webhook assignment automation delayed: {type(error).__name__}")
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
    if path == "/api/workers/activity" and method == "GET":
        return workers_activity_route(environ, start_response)
    if path == "/api/groups" and method == "GET":
        return groups_route(environ, start_response)
    if path == "/api/freebies/status" and method == "GET":
        return freebies_status_route(environ, start_response)
    if path == "/api/new-clients/status" and method == "GET":
        return new_clients_status_route(environ, start_response)
    if path == "/api/new-clients/suno-supply":
        if method != "GET":
            return response(start_response, 405, {"ok": False})
        return suno_supply_route(environ, start_response)
    if path == "/api/clients" and method == "GET":
        return client_directory_route(environ, start_response)
    if path == "/api/workers/capacity" and method == "GET":
        return capacity_route(environ, start_response)
    if path == "/api/referrals/status" and method == "GET":
        return referrals_status_route(environ, start_response)
    if path == "/api/referrals/audit" and method == "GET":
        return referrals_audit_route(environ, start_response)
    if path == "/api/reports/plan" and method == "GET":
        return daily_plan_route(environ, start_response)
    if path == "/api/reports/daily-summary" and method == "GET":
        return daily_summary_route(environ, start_response)
    if path == "/api/reports/concerns" and method == "GET":
        return concerns_route(environ, start_response)
    if path == "/api/reports/topics" and method == "GET":
        return report_topics_route(environ, start_response)
    if path == "/api/reports/members" and method == "GET":
        return report_members_route(environ, start_response)
    if path == "/api/suno/makata" and method in {"GET", "POST"}:
        return makata_status_route(environ, start_response)
    if path == "/api/schedules" and method in {"GET", "POST", "DELETE"}:
        return schedules_route(environ, start_response, method)
    if path == "/api/scheduler" and method in {"GET", "POST"}:
        return scheduler_route(environ, start_response, method)
    if path == "/api/cron/dispatch" and method == "GET":
        return dispatch_route(environ, start_response)
    if path == "/api/webhook" and method == "POST":
        return webhook_route(environ, start_response)
    if path in {"/api/health", "/api/status", "/api/messages", "/api/workers/activity", "/api/workers/capacity", "/api/groups", "/api/clients", "/api/new-clients/status", "/api/referrals/status", "/api/referrals/audit", "/api/reports/plan", "/api/reports/daily-summary", "/api/reports/concerns", "/api/freebies/status", "/api/schedules", "/api/scheduler", "/api/cron/dispatch", "/api/webhook"}:
        return response(start_response, 405, {"ok": False})
    return response(start_response, 404, {"ok": False})

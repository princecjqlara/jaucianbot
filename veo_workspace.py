"""Authenticated VEO sales handoff. Only the normal scheduler can publish it.

The CRM ledger is authoritative. Telegram posts never create a second sale.
Photos are requested just before sending and never retained in this archive.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
import urllib.request
import uuid

from cloud_http import authorized
from cloud_store import allowed_chat_ids, enqueue_scheduled_action, request
from automation_controls import chat_enabled


def enabled() -> bool:
    return os.environ.get("VEO_WORKSPACE_ENABLED", "").lower() == "true"


def authoritative(submission_id: str) -> dict:
    uuid.UUID(submission_id)
    base = os.environ.get("VEO_WORKSPACE_URL", "").rstrip("/")
    secret = os.environ.get("VEO_JAUCIAN_SECRET", "")
    if not base.startswith("https://") or not secret:
        raise RuntimeError("VEO workspace handoff is not configured")
    target = base + "/api/workspace/handoff?" + urllib.parse.urlencode({"submission_id": submission_id})
    req = urllib.request.Request(target, headers={"Authorization": "Bearer " + secret, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        result = json.load(response)
    validate(result)
    return result


def validate(data: dict) -> None:
    for key in ("submission_id", "tenant_id", "team_id", "seller_id", "page_id", "contact_id", "order_id"):
        uuid.UUID(data[key])
    expected_tenant = os.environ.get("VEO_WORKSPACE_TENANT_ID")
    if expected_tenant and data["tenant_id"] != expected_tenant:
        raise ValueError("Sale belongs to another tenant")
    chat = int(data["telegram_chat_id"])
    from daily_automation import GROUPS
    if chat not in GROUPS or chat not in allowed_chat_ids() or not chat_enabled(chat):
        raise ValueError("Destination is not an approved active VEO team")
    if int(data["telegram_user_id"]) <= 0 or int(data["total_minor"]) <= 0 or int(data["paid_minor"]) != int(data["total_minor"]):
        raise ValueError("Verified settlement and worker identity required")
    if not data.get("closed_at") or not data.get("done_at") or dt.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")) <= dt.datetime.now(dt.timezone.utc):
        raise ValueError("Expired or incomplete sale")


def receive(environ, start_response, respond):
    if not enabled() or not authorized(environ.get("HTTP_AUTHORIZATION"), "VEO_JAUCIAN_SECRET"):
        return respond(start_response, 401, {"ok": False})
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < length <= 100_000:
            return respond(start_response, 400, {"ok": False, "error": "Invalid handoff size"})
        data = json.loads(environ["wsgi.input"].read(length))
        validate(data)
        # Re-read the authenticated application rather than trusting even a signed snapshot.
        current = authoritative(data["submission_id"])
        for key in ("tenant_id", "team_id", "seller_id", "page_id", "contact_id", "order_id", "telegram_chat_id", "telegram_user_id"):
            if current[key] != data[key]:
                raise ValueError("Sale attribution or destination changed")
        key = f"veo-sale:{current['tenant_id']}:{current['order_id']}"
        rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,chat_id,status,payload,telegram_message_id", "dedupe_key": "eq." + key, "limit": 1})) or []
        if not rows:
            enqueue_scheduled_action(chat_id=int(current["telegram_chat_id"]), action_type="message",
                scheduled_for=dt.datetime.now(dt.timezone.utc), dedupe_key=key,
                payload={"veo_submission_id": current["submission_id"], "veo_order_id": current["order_id"],
                    "veo_tenant_id": current["tenant_id"], "text": "Verified VEO order; authoritative details requested at delivery"})
            rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,chat_id,status,payload,telegram_message_id", "dedupe_key": "eq." + key, "limit": 1})) or []
        if not rows:
            raise RuntimeError("Handoff persistence failed")
        saved = rows[0]
        old_payload = saved.get("payload") or {}
        acknowledged = old_payload.get("_veo_message_id") or saved.get("telegram_message_id")
        if isinstance(acknowledged, int) and not isinstance(acknowledged, bool) and acknowledged > 0:
            if saved["status"] in {"pending", "failed"}:
                request("scheduled_actions?" + urllib.parse.urlencode({"id": "eq." + str(saved["id"]), "status": "in.(pending,failed)"}),
                    {"status": "sent", "telegram_message_id": acknowledged, "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "last_error": None, "repeat_interval_minutes": None}, method="PATCH")
            return respond(start_response, 200, {"ok": True, "status": "sent", "order_id": current["order_id"]})
        if saved["status"] != "processing" and (old_payload.get("_veo_sending") or old_payload.get("veo_delivery_unknown")):
            return respond(start_response, 200, {"ok": True, "status": "cancelled", "order_id": current["order_id"]})
        if saved["status"] in {"pending", "failed"} and (saved["status"] == "failed" or old_payload.get("veo_submission_id") != current["submission_id"] or saved.get("chat_id", current["telegram_chat_id"]) != current["telegram_chat_id"]):
            # Compare the old submission and acknowledgment state as well as status: a
            # concurrent successful sender must never lose its message checkpoint.
            request("scheduled_actions?" + urllib.parse.urlencode({"id": "eq." + str(saved["id"]), "status": "in.(pending,failed)",
                "payload->>veo_submission_id": "eq." + str(old_payload.get("veo_submission_id")),
                "payload->>_veo_message_id": "is.null", "telegram_message_id": "is.null",
                "payload->>_veo_sending": "eq.false" if old_payload.get("_veo_sending") is False else "is.null",
                "payload->>veo_delivery_unknown": "is.null"}),
                {"chat_id": current["telegram_chat_id"], "status": "pending", "attempts": 0, "last_error": None,
                    "scheduled_for": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "payload": {"veo_submission_id": current["submission_id"], "veo_order_id": current["order_id"], "veo_tenant_id": current["tenant_id"],
                    "text": "Verified VEO order; delivery eligibility refreshed"}}, method="PATCH")
            saved["status"] = "pending"
        return respond(start_response, 200, {"ok": True, "status": saved["status"], "order_id": current["order_id"]})
    except (ValueError, KeyError, TypeError):
        return respond(start_response, 409, {"ok": False, "error": "Invalid, expired or changed sale"})
    except Exception as error:
        print(f"VEO handoff unavailable: {type(error).__name__}")
        return respond(start_response, 503, {"ok": False, "error": "Handoff unavailable; retry later"})


def send(action: dict) -> dict:
    if not enabled():
        raise RuntimeError("VEO workspace delivery is disabled")
    data = authoritative(action["payload"]["veo_submission_id"])
    if data["order_id"] != action["payload"]["veo_order_id"] or data["tenant_id"] != action["payload"]["veo_tenant_id"] or int(data["telegram_chat_id"]) != int(action["chat_id"]):
        raise ValueError("Sale or destination changed; review required")
    from telegram_sender import telegram_call
    caption = "\n".join(["VEO · VERIFIED DONE DEAL", f"Seller: {data.get('seller_name') or 'Unknown'} ({data['telegram_user_id']})",
        f"Client: {data.get('contact_name') or 'Unknown'}", str(data.get("description") or "Order")[:250],
        f"Full verified payment: {int(data['total_minor']) / 100:.2f} {data['currency']}",
        f"Closed: {data['closed_at']}", f"Done: {data['done_at']}", f"Order: {data['order_id']}",
        "Source: VEO verified sales ledger. Count this order once; this summary is not a new receipt."])
    payload = {"chat_id": int(data["telegram_chat_id"]), "photo": data["photo_url"], "caption": caption[:1024]}
    if data.get("telegram_thread_id"):
        payload["message_thread_id"] = int(data["telegram_thread_id"])
    return telegram_call("sendPhoto", payload)


def report_rows(tenant_id: str, work_date: str) -> dict:
    """Read-only report reconciliation; Telegram receipts are excluded after launch."""
    import crm_store
    uuid.UUID(tenant_id)
    rows = crm_store.crm_request("veo_order_facts?" + urllib.parse.urlencode({
        "select": "id,seller_id,team_id,currency,total_minor,paid_minor,closed_date,done_date",
        "tenant_id": "eq." + tenant_id,
        "or": f"(closed_date.eq.{work_date},done_date.eq.{work_date})", "limit": 10000,
    })) or []
    return {"source": "VEO authoritative ledger", "work_date": work_date, "orders": rows,
        "closed": sum(row["closed_date"] == work_date for row in rows),
        "done": sum(row["done_date"] == work_date for row in rows), "telegram_receipts_counted": False,
        "limited": len(rows) == 10000}


def reporting(work_date: str) -> dict:
    tenant = os.environ.get("VEO_WORKSPACE_TENANT_ID", "")
    uuid.UUID(tenant)
    dt.date.fromisoformat(work_date)
    base = os.environ.get("VEO_WORKSPACE_URL", "").rstrip("/")
    if not base.startswith("https://") or not os.environ.get("VEO_JAUCIAN_SECRET"):
        raise RuntimeError("Workspace reporting is not configured")
    target = base + "/api/workspace/handoff?" + urllib.parse.urlencode({"tenant_id": tenant, "report_date": work_date})
    req = urllib.request.Request(target, headers={"Authorization": "Bearer " + os.environ["VEO_JAUCIAN_SECRET"]})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def review_required() -> bool:
    """Read-only health check for held, unacknowledged Telegram attempts."""
    return bool(request("scheduled_actions?" + urllib.parse.urlencode({"select": "id", "limit": 1,
        "dedupe_key": "like.veo-*", "status": "eq.cancelled", "payload->>_veo_sending": "eq.true",
        "payload->>_veo_message_id": "is.null", "telegram_message_id": "is.null"})))


def queue_reports(now: dt.datetime, allowed: set[int]) -> int:
    from daily_automation import MANILA, GROUPS
    local = now.astimezone(MANILA)
    if local.hour == 0 and local.minute < 10:
        return 0
    day = str(local.date() - dt.timedelta(days=1))
    launched = os.environ.get("VEO_WORKSPACE_LAUNCH_AT", "")
    if not launched or dt.date.fromisoformat(day) < dt.datetime.fromisoformat(launched.replace("Z", "+00:00")).astimezone(MANILA).date():
        return 0
    data = reporting(day)
    queued = 0
    for team in data["teams"]:
        if team.get("chat_id") not in allowed.intersection(GROUPS):
            continue
        queued += int(enqueue_scheduled_action(chat_id=team["chat_id"], action_type="message", scheduled_for=now,
            dedupe_key=f"veo-ledger-report:{data['tenant_id']}:{team['id']}:{day}", payload={"veo_report_date": day,
                "veo_team_id": team["id"], "text": "Authoritative VEO daily commission report"}))
    return queued


def send_report(action: dict) -> dict:
    from telegram_sender import telegram_call
    source = action["payload"]
    data = reporting(source["veo_report_date"])
    team = next((t for t in data["teams"] if t["id"] == source["veo_team_id"]), None)
    if not team or team.get("chat_id") != int(action["chat_id"]):
        raise ValueError("Team report mapping changed")
    lines = [f"VEO · {team['name']} · {data['work_date']} PHT", data["source"]]
    active = 0
    target = 0
    for worker in team["workers"]:
        salary = worker["salary"]
        lines.append(f"{worker['name'] or 'Unknown'}: CD {salary['closed']}/{salary['target']} · DD {salary['done']}/{salary['target']} · {salary['rate']}%")
        for total in salary["totals"]:
            lines.append(f"  Commission {total['commission_minor'] / 100:.2f} {total['currency']} on {total['gross_minor'] / 100:.2f}")
        if worker.get("active_tomorrow") is True:
            active += 1
            target += worker["tomorrow_target"]
    lines.append(f"Active tomorrow: {active} · CD and DD team target: {target} each")
    text = "\n".join(lines)
    if len(text) > 4096:
        # Do not silently drop workers or split a non-idempotent send mid-report.
        raise ValueError("Team report exceeds Telegram message size; split the team before enabling its report")
    payload = {"chat_id": int(action["chat_id"]), "text": text}
    if team.get("thread_id"):
        payload["message_thread_id"] = team["thread_id"]
    return telegram_call("sendMessage", payload)


def deliver(allowed: set[int], limit: int = 25) -> tuple[int, int, int]:
    from cloud_store import claim_scheduled_actions, finish_scheduled_action, _update_claim
    actions = claim_scheduled_actions(allowed, limit=limit)
    sent = failed = 0
    for action in actions:
        payload = action.get("payload") or {}
        if not (payload.get("veo_submission_id") or payload.get("veo_report_date")):
            if not _update_claim(action, {"status": "cancelled", "last_error": "Archived at VEO workspace launch"}):
                failed += 1
            continue
        try:
            message_id = payload.get("_veo_message_id")
            if not message_id:
                if payload.get("_veo_sending"):
                    _update_claim(action, {"status": "cancelled", "last_error": "Interrupted Telegram attempt requires reconciliation; resend suppressed"})
                    failed += 1
                    continue
                payload["_veo_sending"] = True
                if not _update_claim(action, {"payload": payload}):
                    raise RuntimeError("Could not reserve Telegram delivery")
                result = send(action) if payload.get("veo_submission_id") else send_report(action)
                message_id = int(result["message_id"])
                # Checkpoint the acknowledged message before completing the outbox lease.
                payload["_veo_message_id"] = message_id
                if not _update_claim(action, {"payload": payload}):
                    raise RuntimeError("Telegram acknowledgment checkpoint failed")
            if not finish_scheduled_action(action["id"], success=True, telegram_message_id=message_id, claim=action):
                raise RuntimeError("Delivery completion checkpoint failed")
            sent += 1
        except Exception as error:
            failed += 1
            if getattr(error, "delivery_uncertain", False):
                payload["veo_delivery_unknown"] = True
                _update_claim(action, {"status": "cancelled", "payload": payload, "last_error": "Telegram delivery requires reconciliation; automatic resend suppressed"})
            else:
                payload["_veo_sending"] = False
                _update_claim(action, {"payload": payload})
                finish_scheduled_action(action["id"], success=False, error="Workspace eligibility or delivery failed; retry scheduled", claim=action)
    return len(actions), sent, failed


def dispatch(environ, start_response, respond):
    from automation_controls import enabled_chat_ids
    import database_scheduler
    allowed = enabled_chat_ids(allowed_chat_ids())
    guard_error = False
    try:
        # Retain payloads while retiring old failures and future recurring actions,
        # including Suno actions which are no longer in the active dispatcher scope.
        approved = allowed_chat_ids()
        if approved:
            request("scheduled_actions?" + urllib.parse.urlencode({
                "chat_id": "in.(" + ",".join(map(str, sorted(approved))) + ")",
                "status": "in.(pending,failed)", "payload->>veo_submission_id": "is.null", "payload->>veo_report_date": "is.null",
            }), {"status": "cancelled", "last_error": "Archived at VEO workspace launch", "repeat_interval_minutes": None}, method="PATCH")
    except Exception:
        guard_error = True
    try:
        database_scheduler.ensure_scheduler(allowed)
    except Exception:
        guard_error = True
    try:
        queued = queue_reports(dt.datetime.now(dt.timezone.utc), allowed)
    except Exception:
        queued = 0
        guard_error = True
    try:
        processed, sent, failed = deliver(allowed)
        return respond(start_response, 503 if guard_error else 502 if failed else 200,
            {"ok": not guard_error and not failed, "queued": queued, "processed": processed, "sent": sent, "failed": failed,
                "source": "VEO workspace", "scheduler_guard_failed": guard_error})
    except Exception:
        return respond(start_response, 503, {"ok": False, "error": "Workspace scheduler unavailable"})

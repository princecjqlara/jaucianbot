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
import urllib.error
import uuid

from cloud_http import authorized
from cloud_store import allowed_chat_ids, enqueue_scheduled_action, request
from automation_controls import chat_enabled
from veo_mode import new_clients_only, intake_action, require_non_intake_enabled, approved_deals_enabled, reviewed_action, mode_name, delivery_cutoff, delivery_in_window


def enabled() -> bool:
    return os.environ.get("VEO_WORKSPACE_ENABLED", "").lower() == "true"


class ReceiptDeleted(ValueError):
    """The authoritative CRM permanently withdrew this receipt's evidence."""


def authoritative(submission_id: str) -> dict:
    uuid.UUID(submission_id)
    base = os.environ.get("VEO_WORKSPACE_URL", "").rstrip("/")
    secret = os.environ.get("VEO_JAUCIAN_SECRET", "")
    if not base.startswith("https://") or not secret:
        raise RuntimeError("VEO workspace handoff is not configured")
    target = base + "/api/workspace/handoff?" + urllib.parse.urlencode({"submission_id": submission_id})
    req = urllib.request.Request(target, headers={"Authorization": "Bearer " + secret, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 410:
            try:
                reason = json.loads(error.read(4096))
            except (ValueError, TypeError):
                reason = {}
            if reason.get("code") == "RECEIPT_DELETED":
                raise ReceiptDeleted("Receipt deleted; delivery cancelled") from error
        raise
    validate(result)
    return result


def validate(data: dict) -> None:
    if data.get("deal_review_id"):
        validate_review(data)
    for key in ("submission_id", "tenant_id", "team_id", "seller_id", "page_id", "contact_id"):
        uuid.UUID(data[key])
    expected_tenant = os.environ.get("VEO_WORKSPACE_TENANT_ID")
    if expected_tenant and data["tenant_id"] != expected_tenant:
        raise ValueError("Sale belongs to another tenant")
    chat = int(data["telegram_chat_id"])
    from daily_automation import GROUPS
    if chat not in GROUPS or chat not in allowed_chat_ids() or not chat_enabled(chat):
        raise ValueError("Destination is not an approved active VEO team")
    kind = data.get("receipt_kind", "done")
    if kind not in {"closed", "done"}:
        raise ValueError("Unknown receipt milestone")
    stage_closed = kind == "closed" and bool(data.get("closed_deal_id"))
    if stage_closed:
        uuid.UUID(data["closed_deal_id"])
        if data.get("lead_stage") not in {"qualified", "converted"} or int(data.get("closed_amount_minor", 0)) <= 0:
            raise ValueError("Qualified or Converted Closed Deal receipt required")
    else:
        if not isinstance(data.get("order_id"), str):
            raise ValueError("A created order is required for Done Deals")
        uuid.UUID(data["order_id"])
    total, paid = int(data["total_minor"]), int(data["paid_minor"])
    tolerance = int(data.get("settlement_tolerance_minor", 0))
    if tolerance not in {0, 200} or (data.get("currency") != "PHP" and tolerance):
        raise ValueError("Settlement tolerance exceeds PHP 2")
    if int(data["telegram_user_id"]) <= 0 or total <= 0 or paid < 0 or (not stage_closed and (paid <= 0 or paid > total + tolerance)) or (kind == "done" and (abs(paid - total) > tolerance or data.get("payment_verified") is False)):
        raise ValueError("Verified settlement and worker identity required")
    if not data.get("closed_at") or (kind == "done" and not data.get("done_at")) or dt.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")) <= dt.datetime.now(dt.timezone.utc):
        raise ValueError("Expired or incomplete sale")


def validate_review(data: dict, *, require_urls: bool = False) -> None:
    uuid.UUID(data["deal_review_id"])
    uuid.UUID(data["reviewed_by"])
    mode = data.get("approval_mode", "supervisor_review")
    direct = False
    if mode == "supervisor_submission":
        uuid.UUID(data["submitted_by"])
        direct = (data.get("direct_submission_authorized") is True
                  and data.get("submitted_by_role") in {"owner", "team_leader", "manager"}
                  and data["reviewed_by"] == data["submitted_by"])
        if not direct:
            raise ValueError("Authorized supervisor submission required")
    elif mode != "supervisor_review":
        raise ValueError("Unknown receipt authorization method")
    if data.get("review_status") != "approved" or not data.get("reviewed_at") or (not direct and data["reviewed_by"] in {data["seller_id"], data.get("submitted_by")}):
        raise ValueError("Independent supervisor approval required")
    photos = data.get("evidence_photos")
    if not isinstance(photos, list) or len(photos) != 3 or {p.get("slot") for p in photos} != {"stage", "payment", "order"}:
        raise ValueError("Three reviewed evidence photos required")
    if require_urls and any(not str(p.get("photo_url", "")).startswith("https://") for p in photos):
        raise ValueError("Current signed evidence URLs required")
    for key in ("done_amount_minor", "tip_minor", "closed_amount_minor", "total_received_minor"):
        v = data.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 27_000_000_000_000:
            raise ValueError("Reviewed financial amounts required")


def receive(environ, start_response, respond):
    if not enabled() or not authorized(environ.get("HTTP_AUTHORIZATION"), "VEO_JAUCIAN_SECRET"):
        return respond(start_response, 401, {"ok": False})
    if new_clients_only() and not approved_deals_enabled():
        return respond(start_response, 410, {"ok": False, "error": "Jaucian handles new clients only; deals stay in VEO", "mode": "new_clients_only"})
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < length <= 100_000:
            return respond(start_response, 400, {"ok": False, "error": "Invalid handoff size"})
        data = json.loads(environ["wsgi.input"].read(length))
        validate(data)
        # Re-read the authenticated application rather than trusting even a signed snapshot.
        current = authoritative(data["submission_id"])
        if new_clients_only() and not current.get("deal_review_id"):
            raise ValueError("Independent supervisor approval required")
        for key in ("tenant_id", "team_id", "seller_id", "page_id", "contact_id", "order_id", "telegram_chat_id", "telegram_user_id"):
            if current[key] != data[key]:
                raise ValueError("Sale attribution or destination changed")
        if current.get("receipt_kind", "done") != data.get("receipt_kind", "done"):
            raise ValueError("Receipt milestone changed")
        if current.get("closed_deal_id") != data.get("closed_deal_id"):
            raise ValueError("Closed Deal reference changed")
        kind = current.get("receipt_kind", "done")
        reference = current.get("closed_deal_id") if kind == "closed" else current["order_id"]
        reference = reference or current["order_id"]
        key = f"veo-sale:{current['tenant_id']}:{reference}" + (":closed" if kind == "closed" else "")
        rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,chat_id,status,payload,telegram_message_id", "dedupe_key": "eq." + key, "limit": 1})) or []
        if not rows:
            enqueue_scheduled_action(chat_id=int(current["telegram_chat_id"]), action_type="message",
                scheduled_for=dt.datetime.now(dt.timezone.utc), dedupe_key=key,
                payload={"veo_submission_id": current["submission_id"], "veo_order_id": current["order_id"],
                    "veo_closed_deal_id": current.get("closed_deal_id"), "veo_tenant_id": current["tenant_id"], "veo_receipt_kind": kind, "veo_deal_review_id": current.get("deal_review_id"), "veo_receipt_details": not bool(current.get("deal_review_id")), "text": "VEO receipt; authoritative eligibility requested at delivery"})
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
                    "payload": {"veo_submission_id": current["submission_id"], "veo_order_id": current["order_id"], "veo_tenant_id": current["tenant_id"], "veo_receipt_kind": kind,
                    "veo_closed_deal_id": current.get("closed_deal_id"), "veo_deal_review_id": current.get("deal_review_id"), "veo_receipt_details": not bool(current.get("deal_review_id")),
                    "text": "Verified VEO order; delivery eligibility refreshed"}}, method="PATCH")
            saved["status"] = "pending"
        return respond(start_response, 200, {"ok": True, "status": saved["status"], "order_id": current["order_id"]})
    except (ValueError, KeyError, TypeError):
        return respond(start_response, 409, {"ok": False, "error": "Invalid, expired or changed sale"})
    except Exception as error:
        print(f"VEO handoff unavailable: {type(error).__name__}")
        return respond(start_response, 503, {"ok": False, "error": "Handoff unavailable; retry later"})


def current_submission(action: dict) -> dict:
    """Keep the photo and follow-on details bound to the same receipt context."""
    data = authoritative(action["payload"]["veo_submission_id"])
    if data["order_id"] != action["payload"]["veo_order_id"] or data["tenant_id"] != action["payload"]["veo_tenant_id"] or int(data["telegram_chat_id"]) != int(action["chat_id"]):
        raise ValueError("Sale or destination changed; review required")
    kind = data.get("receipt_kind", "done")
    if data.get("closed_deal_id") != action["payload"].get("veo_closed_deal_id"):
        raise ValueError("Closed Deal reference changed; review required")
    if kind != action["payload"].get("veo_receipt_kind", "done"):
        raise ValueError("Receipt milestone changed; review required")
    if data.get("deal_review_id") != action["payload"].get("veo_deal_review_id"):
        raise ValueError("Supervisor review reference changed")
    return data


def receipt_topic(data: dict) -> int | None:
    from daily_automation import GROUPS
    return data.get("telegram_thread_id") or GROUPS.get(int(data["telegram_chat_id"]), {}).get(
        "close" if data.get("receipt_kind", "done") == "closed" else "done")


def send(action: dict) -> dict:
    if new_clients_only() and not reviewed_action(action):
        require_non_intake_enabled()
    if not enabled():
        raise RuntimeError("VEO workspace delivery is disabled")
    data = current_submission(action)
    kind = data.get("receipt_kind", "done")
    from veo_directory import verify_identity
    verify_identity(int(data["telegram_chat_id"]), int(data["telegram_user_id"]))
    from telegram_sender import telegram_call
    if data.get("deal_review_id"):
        validate_review(data, require_urls=True)
        currency = data["currency"]
        def amount(key):
            minor = int(data.get(key) or 0)
            return f"{minor // 100:,}.{minor % 100:02d} {currency}"
        name = lambda key: str(data.get(key) or "Unknown").replace("\n", " ")[:70]
        direct = data.get("approval_mode") == "supervisor_submission"
        authorization = ("Submitted by: " + name("reviewer_name") + " (" + str(data["submitted_by_role"]).replace("_", " ") + ")") if direct else "Approved by: " + name("reviewer_name")
        caption = "\n".join([("VEO · " if direct else "VEO · APPROVED ") + ("CLOSE DEAL" if kind == "closed" else "DONE DEAL"),
            "Client: " + name("contact_name"), "Page: " + name("page_name"), "Seller: " + name("seller_name"),
            authorization, "Deal price: " + amount("total_minor"),
            "Close Deal amount: " + amount("closed_amount_minor"), "Done Deal received now: " + amount("done_amount_minor"),
            "Tip received now: " + amount("tip_minor"), "Total received incl. tips: " + amount("total_received_minor"),
            "Order: " + data["order_id"], "Photos: 1 Lead stage · 2 Receipt · 3 Order status",
            "Close Deal awaits Done Deal reconciliation." if kind == "closed" else "Order paid. Earlier Close Deal counted once; tip separate."])
        media = [{"type": "photo", "media": p["photo_url"], **({"caption": caption} if i == 0 else {})}
                 for i, p in enumerate(sorted(data["evidence_photos"], key=lambda p: {"stage": 0, "payment": 1, "order": 2}[p["slot"]]))]
        payload = {"chat_id": int(data["telegram_chat_id"]), "media": media}
        topic = receipt_topic(data)
        if topic:
            payload["message_thread_id"] = int(topic)
        return telegram_call("sendMediaGroup", payload)
    caption = "\n".join(["VEO · VERIFIED CLOSED DEAL" if kind == "closed" else "VEO · VERIFIED DONE DEAL", f"Seller: {data.get('seller_name') or 'Unknown'} ({data['telegram_user_id']})",
        f"Client: {data.get('contact_name') or 'Unknown'}", str(data.get("description") or "Order")[:250],
        f"Verified received: {int(data['paid_minor']) / 100:.2f} / {int(data['total_minor']) / 100:.2f} {data['currency']}",
        f"Closed: {data['closed_at']}", f"Done: {data.get('done_at') or 'Balance pending'}", f"Order: {data['order_id']}",
        "Source: VEO verified sales ledger. Count this order once; this summary is not a new receipt."])
    if kind == "closed" and data.get("closed_deal_id"):
        caption = "\n".join(["VEO · CLOSED DEAL", f"Seller: {data.get('seller_name') or 'Unknown'} ({data['telegram_user_id']})",
            f"Client: {data.get('contact_name') or 'Unknown'}", f"Lead stage: {data['lead_stage']}",
            f"Reported Closed Deal payment: {int(data['closed_amount_minor']) / 100:.2f} {data['currency']}",
            "Payment matched to the verified ledger." if data.get("payment_verified") else "Payment verification pending Done Deal reconciliation; no salary yet.",
            f"Closed: {data['closed_at']}", f"Order: {data.get('linked_order_id') or data.get('order_id') or 'Unavailable; review VEO order link'}",
            f"Closed Deal: {data['closed_deal_id']}", "Source: VEO stage-qualified receipt. Count this deal once; this summary does not create a payment."])
    payload = {"chat_id": int(data["telegram_chat_id"]), "photo": data["photo_url"], "caption": caption[:1024]}
    topic = receipt_topic(data)
    if topic:
        payload["message_thread_id"] = int(topic)
    return telegram_call("sendPhoto", payload)


def send_receipt_details(action: dict) -> dict:
    require_non_intake_enabled()
    """Send the complete receipt context after the photo's durable checkpoint."""
    data = current_submission(action)
    from telegram_sender import telegram_call
    from veo_directory import verify_identity
    verify_identity(int(data["telegram_chat_id"]), int(data["telegram_user_id"]))
    lines = ["VEO · RECEIPT DETAILS", f"Milestone: {str(data.get('receipt_kind', 'done')).upper()}",
        f"Client: {data.get('contact_name') or 'Unknown'} · Page: {data.get('page_name') or 'Unknown'}",
        f"Lead stage: {data.get('lead_stage') or 'Unavailable'} · Seller: {data.get('seller_name') or 'Unknown'}",
        f"Order: {data.get('linked_order_id') or data.get('order_id') or 'Unavailable; review VEO order link'} · Facebook invoice: {data.get('invoice_id') or 'Not linked'}",
        f"Reported Closed Deal: {int(data.get('closed_amount_minor') or 0) / 100:.2f} {data.get('closed_currency') or data['currency']} · received {int(data['paid_minor']) / 100:.2f} / {int(data['total_minor']) / 100:.2f} {data['currency']}",
        f"Closed: {data.get('closed_at') or 'Not recorded'} · Done: {data.get('done_at') or 'Not completed'}",
        f"Source: {((data.get('source_evidence') or {}).get('source') or 'unavailable')} · {((data.get('source_evidence') or {}).get('stage_occurred_at') or 'stage time unavailable')}"]
    notes = str(data.get("receipt_notes") or "").strip()
    if notes:
        lines.extend(["Receipt notes:", notes[:2000]])
    text = "\n".join(lines)
    if len(text) > 4096:
        raise ValueError("Receipt detail message exceeds Telegram's message limit")
    payload = {"chat_id": int(data["telegram_chat_id"]), "text": text}
    topic = receipt_topic(data)
    if topic:
        payload["message_thread_id"] = int(topic)
    return telegram_call("sendMessage", payload)


def report_rows(tenant_id: str, work_date: str) -> dict:
    """Read-only report reconciliation; Telegram receipts are excluded after launch."""
    import crm_store
    uuid.UUID(tenant_id)
    rows = crm_store.crm_request("veo_sales_facts?" + urllib.parse.urlencode({
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
    target = base + "/api/workspace/handoff?" + urllib.parse.urlencode({"tenant_id": tenant, "report_date": work_date, "include_operations": "1"})
    req = urllib.request.Request(target, headers={"Authorization": "Bearer " + os.environ["VEO_JAUCIAN_SECRET"]})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def review_required() -> bool:
    """Read-only health check for held, unacknowledged Telegram attempts."""
    filters = [{"payload->>_veo_sending": "eq.true", "payload->>_veo_message_id": "is.null", "telegram_message_id": "is.null"},
               {"payload->>_veo_details_sending": "eq.true", "payload->>_veo_details_message_id": "is.null"}]
    for condition in filters:
        base = {"dedupe_key": "like.veo-*", "status": "eq.cancelled", **condition}
        tenant = os.environ.get("VEO_WORKSPACE_TENANT_ID")
        if new_clients_only() and tenant:
            uuid.UUID(tenant)
            base["payload->>veo_tenant_id"] = "eq." + tenant
        if delivery_cutoff():
            base["scheduled_for"] = "gte." + delivery_cutoff()
        if not new_clients_only():
            if request("scheduled_actions?" + urllib.parse.urlencode({"select": "id", "limit": 1, **base})):
                return True
            continue
        # Use filters shared by HTTP and PostgreSQL archive transports. Keep
        # obsolete holds intact, and fail health for every supported workflow.
        for offset in range(0, 10000, 100):
            rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,payload", "order": "id.asc", "limit": 100, "offset": offset, **base})) or []
            if any(intake_action(row) or reviewed_action(row) for row in rows):
                return True
            if len(rows) < 100:
                break
        else:
            raise RuntimeError("Delivery hold review exceeds the health scan limit")
    return False


def delivery_hold_summary(tenant: str) -> dict:
    """Read-only identifiers for investigating suppressed sends, without photos or text."""
    uuid.UUID(tenant)
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,chat_id,payload,updated_at,scheduled_for,telegram_message_id",
        "payload->>veo_tenant_id": "eq." + tenant, "status": "eq.cancelled", "payload->>_veo_sending": "eq.true",
        "payload->>_veo_message_id": "is.null", "telegram_message_id": "is.null", "order": "id.asc", "limit": 100})) or []
    return {"ok": True, "tenant_id": tenant, "read_only": True, "limit": 100,
            "holds": [{"id": row["id"], "chat_id": row["chat_id"], "scheduled_for": row.get("scheduled_for"), "updated_at": row.get("updated_at"),
                       "active_workflow": (intake_action(row) or reviewed_action(row)) and delivery_in_window(row),
                       **{key: (row.get("payload") or {}).get(key) for key in ("veo_submission_id", "veo_deal_review_id", "veo_report_date", "veo_availability_date", "veo_intake_request_id", "veo_intake_offer_id", "veo_intake_notice_id", "veo_intake_help")}}
                      for row in rows]}


def queue_reports(now: dt.datetime, allowed: set[int]) -> int:
    if new_clients_only():
        return 0
    from veo_shift_reports import queue
    return queue(now, allowed)


def send_report(action: dict) -> dict:
    require_non_intake_enabled()
    if action["payload"].get("veo_report_slot"):
        from veo_shift_reports import send as send_shift
        return send_shift(action)
    from telegram_sender import telegram_call
    source = action["payload"]
    data = reporting(source["veo_report_date"])
    team = next((t for t in data["teams"] if t["id"] == source["veo_team_id"] and t.get("chat_id") == int(action["chat_id"])), None)
    if not team or team.get("chat_id") != int(action["chat_id"]):
        raise ValueError("Team report mapping changed")
    if team.get('group_report'):
        from veo_shift_reports import ReportScopeChanged
        raise ReportScopeChanged('Use current Page-scoped group report snapshots')
    lines = [f"VEO · {team['name']} · {data['work_date']} PHT", data["source"]]
    capacity = team.get("capacity")
    if capacity:
        quota = capacity["quota"]
        history = capacity["history"]
        lines += [
            f"TEAM QUOTAS: Closed {capacity['closed']}/{quota['closed']} · Done {capacity['done']}/{quota['done']}",
            f"Combined full capacity: {capacity['capacity']} per day · average {capacity['average_worker_capacity'] if capacity['average_worker_capacity'] is not None else 'unavailable'} per worker",
            f"Participating workers: {capacity['participating']} · attendance unknown: {capacity['attendance_unknown']}",
            f"Combined daily average: {history['average_done_per_day'] if history['average_done_per_day'] is not None else 'unavailable'} Done Deals over {history['completed_days']} completed PHT dates (zero days included)",
            f"Best-three team days average: {history['best_three_average'] if history['best_three_average'] is not None else 'insufficient history'}",
        ]
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
    if capacity:
        plan = capacity["tomorrow"]
        lines.append(f"Active tomorrow ({plan['work_date']}): {plan['active']} · CD and DD team quota: {plan['capacity']} each · average active-worker capacity: {plan['average_active_capacity'] if plan['average_active_capacity'] is not None else 'unavailable'}")
        lines.append(f"Full-team potential tomorrow: {plan['full_capacity']} · awaiting declarations: {plan['unknown']}")
        lines.append("Individual commission still requires each worker to reach both personal targets.")
    else:
        lines.append(f"Active tomorrow: {active} · CD and DD team target: {target} each")
    text = "\n".join(lines)
    if len(text) > 4096:
        # Do not silently drop workers or split a non-idempotent send mid-report.
        raise ValueError("Team report exceeds Telegram message size; split the team before enabling its report")
    payload = {"chat_id": int(action["chat_id"]), "text": text}
    from daily_automation import GROUPS
    topic = team.get("thread_id") or GROUPS.get(int(action["chat_id"]), {}).get("announcements")
    if topic:
        payload["message_thread_id"] = int(topic)
    return telegram_call("sendMessage", payload)


def deliver(allowed: set[int], limit: int = 25) -> tuple[int, int, int]:
    from cloud_store import claim_scheduled_actions, finish_scheduled_action, _update_claim
    actions = claim_scheduled_actions(allowed, limit=limit)
    sent = failed = 0
    for action in actions:
        payload = action.get("payload") or {}
        if new_clients_only() and (not (intake_action(action) or reviewed_action(action)) or not delivery_in_window(action)):
            if not _update_claim(action, {"status": "cancelled", "last_error": "Jaucian is new-clients-only; non-intake delivery stopped", "repeat_interval_minutes": None}):
                failed += 1
            continue
        if not (payload.get("veo_submission_id") or payload.get("veo_report_date") or payload.get("veo_availability_date") or payload.get("veo_intake_request_id") or payload.get("veo_intake_offer_id") or payload.get("veo_intake_notice_id") or payload.get("veo_intake_help")):
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
                if payload.get("veo_intake_request_id") or payload.get("veo_intake_offer_id") or payload.get("veo_intake_notice_id") or payload.get("veo_intake_help"):
                    from veo_intake import send as send_intake
                    result = send_intake(action)
                elif payload.get("veo_availability_date"):
                    from veo_shift_reports import send_availability
                    result = send_availability(action)
                    payload["_veo_poll_id"] = result["poll"]["id"]
                else:
                    result = send(action) if payload.get("veo_submission_id") else send_report(action)
                message_id = int(result["message_id"])
                # Checkpoint the acknowledged message before completing the outbox lease.
                payload["_veo_message_id"] = message_id
                if result.get("message_ids"):
                    payload["_veo_album_message_ids"] = result["message_ids"]
                payload["_veo_sent_at"] = dt.datetime.fromtimestamp(int(result.get("date") or dt.datetime.now(dt.timezone.utc).timestamp()), dt.timezone.utc).isoformat()
                if not _update_claim(action, {"payload": payload}):
                    raise RuntimeError("Telegram acknowledgment checkpoint failed")
            if payload.get("veo_submission_id") and payload.get("veo_receipt_details") and not payload.get("_veo_details_message_id"):
                if payload.get("_veo_details_sending"):
                    payload["veo_delivery_unknown"] = True
                    _update_claim(action, {"status": "cancelled", "payload": payload, "last_error": "Receipt details Telegram attempt requires reconciliation; duplicate resend suppressed"})
                    failed += 1
                    continue
                payload["_veo_details_sending"] = True
                if not _update_claim(action, {"payload": payload}):
                    raise RuntimeError("Could not reserve receipt detail delivery")
                details = send_receipt_details(action)
                payload["_veo_details_message_id"] = int(details["message_id"])
                if not _update_claim(action, {"payload": payload}):
                    raise RuntimeError("Receipt detail acknowledgment checkpoint failed")
            if payload.get("veo_availability_date"):
                from veo_shift_reports import register_availability
                register_availability(action)
            if payload.get("veo_intake_notice_id") or payload.get("veo_intake_offer_id"):
                from veo_intake import acknowledge_delivery
                acknowledge_delivery(action, message_id)
            if not finish_scheduled_action(action["id"], success=True, telegram_message_id=message_id, claim=action):
                raise RuntimeError("Delivery completion checkpoint failed")
            sent += 1
        except Exception as error:
            if isinstance(error, ReceiptDeleted):
                payload["_veo_sending"] = False
                payload["veo_receipt_deleted"] = True
                if not _update_claim(action, {"status": "cancelled", "payload": payload, "last_error": "Receipt deleted in VEO; delivery cancelled"}):
                    failed += 1
                continue
            failed += 1
            if getattr(error, "scope_changed", False):
                payload["_veo_sending"] = False
                payload["veo_scope_changed"] = True
                _update_claim(action, {"status": "cancelled", "payload": payload, "last_error": "Report scope or poll date changed; prior snapshot withheld"})
            elif getattr(error, "delivery_uncertain", False):
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
    guard_errors = []
    try:
        # Retain payloads while retiring old failures and future recurring actions,
        # including Suno actions which are no longer in the active dispatcher scope.
        approved = allowed_chat_ids()
        if approved:
            archived = {
                "chat_id": "in.(" + ",".join(map(str, sorted(approved))) + ")",
                "status": "in.(pending,failed)", "payload->>veo_intake_request_id": "is.null", "payload->>veo_intake_offer_id": "is.null", "payload->>veo_intake_notice_id": "is.null", "payload->>veo_intake_help": "is.null",
            }
            if not new_clients_only():
                archived.update({"payload->>veo_submission_id": "is.null", "payload->>veo_report_date": "is.null", "payload->>veo_availability_date": "is.null"})
            elif approved_deals_enabled():
                archived["payload->>veo_deal_review_id"] = "is.null"
            request("scheduled_actions?" + urllib.parse.urlencode(archived), {"status": "cancelled", "last_error": "Jaucian new-clients-only mode" if new_clients_only() else "Archived at VEO workspace launch", "repeat_interval_minutes": None}, method="PATCH")
    except Exception:
        guard_error = True
        guard_errors.append("archive_legacy_actions")
    try:
        request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": "like.veo-ledger-report:*", "status": "in.(pending,failed)"}),
            {"status": "cancelled", "last_error": "Superseded by three scheduled VEO performance snapshots"}, method="PATCH")
    except Exception:
        guard_error = True
        guard_errors.append("archive_legacy_reports")
    try:
        database_scheduler.ensure_scheduler(allowed)
    except Exception:
        guard_error = True
        guard_errors.append("scheduler_configuration")
    try:
        processed, sent, failed = deliver(allowed)
        # Protect already scheduled client, receipt and warning messages from
        # slow report snapshots. Newly due reports enter the next normal tick.
        try:
            queued = queue_reports(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception:
            queued = 0
            guard_error = True
            guard_errors.append("report_queue")
        try:
            from veo_intake import queue as queue_intake
            queued += queue_intake(dt.datetime.now(dt.timezone.utc), allowed)
        except Exception:
            guard_error = True
            guard_errors.append("new_client_queue")
        return respond(start_response, 503 if guard_error else 502 if failed else 200,
            {"ok": not guard_error and not failed, "queued": queued, "processed": processed, "sent": sent, "failed": failed, "mode": mode_name(),
                "source": "VEO workspace", "scheduler_guard_failed": guard_error, "guard_errors": guard_errors})
    except Exception:
        return respond(start_response, 503, {"ok": False, "error": "Workspace scheduler unavailable"})

"""Read-only, tenant-bound database bridge for the existing VEO bot.

Identity candidates come from the archive, not from inferred sales or usernames.
Saving a mapping separately checks current human membership with Telegram.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from cloud_http import authorized
from cloud_store import allowed_chat_ids, request
from automation_controls import enabled_chat_ids
from daily_automation import GROUPS


def teams() -> set[int]:
    return enabled_chat_ids(allowed_chat_ids()).intersection(GROUPS)


def group_directory(chat: int) -> dict:
    # Bound reads explicitly. This is an observed-identity directory, never a
    # claim to list every current member of a Telegram group.
    rows = request("messages?" + urllib.parse.urlencode({
        "select": "author_id,author_name,sent_utc", "chat_id": f"eq.{chat}",
        "author_id": "gt.0", "order": "sent_utc.desc,message_id.desc", "limit": 1000,
    })) or []
    cached = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{chat}",
        "dedupe_key": f"like.capacity-membership:{chat}:*", "limit": 1000,
    })) or []
    people = {}
    for row in rows:
        uid = int(row["author_id"])
        people.setdefault(uid, {"user_id": uid, "name": row.get("author_name") or "Unknown name",
                                "last_seen_at": row.get("sent_utc"), "membership": "unverified"})
    for row in cached:
        p = row.get("payload") or {}
        uid = int(p.get("user_id") or 0)
        if uid <= 0 or p.get("is_bot") is True:
            people.pop(uid, None)
            continue
        person = people.setdefault(uid, {"user_id": uid, "name": p.get("user_name") or "Unknown name",
                                         "last_seen_at": None})
        person.update(membership=p.get("status", "unknown"), membership_checked_at=p.get("verified_at"),
                      membership_expires_at=p.get("expires_at"))
    config = GROUPS[chat]
    topics = {k: config[k] for k in ("announcements", "done", "close", "active") if k in config}
    if config.get("contact_thread"):
        topics["clients"] = config["contact_thread"]
    from automation_controls import new_clients_enabled
    return {"chat_id": chat, "name": config["name"], "new_clients_enabled": new_clients_enabled(chat),
            "topics": topics,
            "pages": config.get("crm_pages", {}),
            "identity_coverage": {"message_sample": len(rows), "message_limit": 1000,
                                  "membership_limit": 1000, "complete_member_list": False},
            "identities": sorted(people.values(), key=lambda p: (p["name"].casefold(), p["user_id"]))}


def directory(tenant: str) -> dict:
    approved = sorted(teams())
    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(group_directory, approved))
    deliveries = []
    if approved:
        deliveries = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id,chat_id,status,attempts,scheduled_for,updated_at,telegram_message_id,payload",
            "chat_id": "in.(" + ",".join(map(str, approved)) + ")",
            "dedupe_key": f"like.veo-*:{tenant}:*", "order": "updated_at.desc,id.desc", "limit": 50,
        })) or []
    # Whitelist fields; never return message text, signed photo URLs or arbitrary
    # scheduled-action payloads to another service.
    safe = []
    for row in deliveries:
        p = row.get("payload") or {}
        safe.append({k: row.get(k) for k in ("id", "chat_id", "status", "attempts", "scheduled_for", "updated_at", "telegram_message_id")}
                    | {k: p.get(k) for k in ("veo_order_id", "veo_closed_deal_id", "veo_receipt_kind", "veo_submission_id", "veo_team_id", "veo_report_date", "veo_report_slot", "veo_report_part", "veo_report_parts", "veo_availability_date", "veo_scope_changed", "veo_intake_offer_id", "veo_intake_request_id", "veo_intake_notice_id", "veo_intake_help")}
                    | {"review_required": bool((p.get("_veo_sending") or p.get("veo_delivery_unknown"))
                                                and not (p.get("_veo_message_id") or row.get("telegram_message_id"))
                                                and row.get("status") == "cancelled")})
    try:
        connection = workspace_connection(tenant)
    except Exception as error:
        print(f"VEO reverse directory unavailable: {type(error).__name__}")
        connection = {"ok": False, "error": "Jaucian cannot currently read the VEO directory"}
    from veo_intake import enabled as intake_enabled
    from veo_mode import mode_name
    return {"ok": True, "tenant_id": tenant, "source": "Jaucian database", "read_only": True, "mode": mode_name(),
            "client_assignments": {"enabled": intake_enabled(), "source": "Tokko current assignment records", "commands": ["/ready", "/busy", "/clients", "/offers", "/myclients", "/handover", "/reassign"]},
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "groups": groups,
            "veo_connection": connection,
            "recent_deliveries": safe, "delivery_limit": 50,
            "coverage": "Observed identities only; current human membership is checked when saving a mapping. Historical messages never verify VEO payments."}


def workspace_directory(tenant: str) -> dict:
    base = os.environ.get("VEO_WORKSPACE_URL", "")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Secure VEO workspace URL required")
    target = base.rstrip("/") + "/api/workspace/handoff?" + urllib.parse.urlencode({"resource": "directory", "tenant_id": tenant})
    req = urllib.request.Request(target, headers={"Authorization": "Bearer " + os.environ["VEO_JAUCIAN_SECRET"], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    if data.get("tenant_id") != tenant or data.get("ok") is not True or data.get("read_only") is not True:
        raise ValueError("VEO directory did not match the connected tenant")
    return data


def workspace_connection(tenant: str) -> dict:
    data = workspace_directory(tenant)
    groups = teams()
    workers = [a for a in data["accounts"] if a["role"] in {"member", "manager", "team_leader"}]
    return {"ok": True, "source": data["source"], "checked_at": data["checked_at"],
            "accounts": len(data["accounts"]), "teams": len(data["teams"]),
            "unmapped_teams": sum(not any(int(g.get("chat_id") or 0) in groups for g in
                (t.get("telegram_groups") if t.get("telegram_groups_managed") else [{"chat_id": t.get("telegram_chat_id")}]) or []) for t in data["teams"]),
            "unmapped_workers": sum(not w.get("telegram_user_id") for w in workers),
            "commission_rule": data["commission_rule"]}


def verify_identity(chat: int, user: int) -> dict:
    if chat not in teams() or user <= 0:
        raise ValueError("Choose an approved active VEO group and a human Telegram user")
    from telegram_sender import telegram_call
    from member_presence import is_current
    try:
        member = telegram_call("getChatMember", {"chat_id": chat, "user_id": user}, timeout=8)
    except Exception as error:
        # A read failed before any send. Do not turn a retryable lookup outage
        # into an uncertain-message hold in the sales outbox.
        raise RuntimeError("Telegram membership lookup unavailable") from error
    identity = member.get("user") or {}
    if int(identity.get("id") or 0) != user or identity.get("is_bot") is not False or not is_current(member):
        raise ValueError("Telegram identity must be a current human member of the selected group")
    return {"ok": True, "chat_id": chat, "user_id": user, "verified": True,
            "name": " ".join(filter(None, (identity.get("first_name"), identity.get("last_name")))) or "Unknown name",
            "username": identity.get("username"), "membership": member["status"],
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def route(environ, start_response, respond):
    if os.environ.get("VEO_WORKSPACE_ENABLED", "").lower() != "true" or not authorized(environ.get("HTTP_AUTHORIZATION"), "VEO_JAUCIAN_SECRET"):
        return respond(start_response, 401, {"ok": False})
    if environ.get("REQUEST_METHOD", "GET").upper() != "GET":
        return respond(start_response, 405, {"ok": False})
    params = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    tenant = params.get("tenant_id", [""])[0]
    try:
        uuid.UUID(tenant)
        if not os.environ.get("VEO_WORKSPACE_TENANT_ID") or tenant != os.environ["VEO_WORKSPACE_TENANT_ID"]:
            return respond(start_response, 403, {"ok": False, "error": "Tenant is not connected"})
        resource = params.get("resource", ["directory"])[0]
        if resource == "identity":
            result = verify_identity(int(params.get("chat_id", [""])[0]), int(params.get("user_id", [""])[0]))
        elif resource == "workforce":
            from veo_workforce import snapshot
            result = snapshot(tenant, offset=int(params.get("offset", ["0"])[0]), limit=int(params.get("limit", ["8"])[0]))
        elif resource == "automatic":
            from veo_automatic_teams import snapshot
            result = snapshot(tenant, offset=int(params.get("offset", ["0"])[0]), limit=int(params.get("limit", ["4"])[0]))
        elif resource == "delivery_holds":
            from veo_workspace import delivery_hold_summary
            result = delivery_hold_summary(tenant)
        elif resource == "directory":
            result = directory(tenant)
        else:
            raise ValueError("Unknown resource")
        return respond(start_response, 200, {**result, "tenant_id": tenant})
    except (ValueError, KeyError, TypeError):
        return respond(start_response, 400, {"ok": False, "error": "Invalid request or Telegram identity is not a current human member"})
    except Exception as error:
        print(f"VEO directory unavailable: {type(error).__name__}")
        return respond(start_response, 503, {"ok": False, "error": "Jaucian directory or membership verification unavailable"})

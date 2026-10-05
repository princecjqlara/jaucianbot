"""Read paid and completed-detail contacts from the CRM Supabase project."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


PAID_TAG = re.compile(r"^paid(?:\s*/\s*availed\s*services?)?$", re.IGNORECASE)


def crm_request(path: str):
    url = os.environ.get("CRM_SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("CRM_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url.startswith("https://") or not key:
        raise RuntimeError("CRM Supabase credentials are not configured")
    request = urllib.request.Request(
        url + "/rest/v1/" + path,
        headers={"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"CRM Data API HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"CRM network error ({type(error.reason).__name__})") from error


def paid_contacts(page_id: str) -> list[dict]:
    """Return CRM contacts tagged Paid for this exact page, without contact tokens."""
    tags = crm_request("tags?" + urllib.parse.urlencode({
        "select": "id,name", "page_id": f"eq.{page_id}", "limit": 1000,
    })) or []
    contacts: dict[str, dict] = {}
    for tag in tags:
        if not PAID_TAG.fullmatch((tag.get("name") or "").strip()):
            continue
        offset = 0
        while True:
            rows = crm_request("contact_tags?" + urllib.parse.urlencode({
                "select": "contact_id,contacts!inner(id,page_id,name,psid,last_interaction_at)",
                "tag_id": f"eq.{tag['id']}", "limit": 1000, "offset": offset,
            })) or []
            for row in rows:
                contact = row.get("contacts") or {}
                if isinstance(contact, list):
                    contact = contact[0] if contact else {}
                if (contact.get("page_id") == page_id and contact.get("id")
                        and (contact.get("name") or "").strip() and contact.get("psid")):
                    contacts[contact["id"]] = {
                        "id": contact["id"],
                        "name": contact["name"].strip(),
                        "last_interaction_at": contact.get("last_interaction_at"),
                    }
            if len(rows) < 1000:
                break
            offset += 1000
    return list(contacts.values())


def completed_detail_contacts(page_id: str) -> list[dict]:
    """Return new clients whose complete chatbot details are stored in the CRM.

    The chatbot state is authoritative for completion. ``contacts.pipeline_stage``
    is included as context, but it is not used as the completion predicate because
    the CRM updates that outcome independently of the chatbot state. A row must also
    contain collected details before it can be assigned to a worker. The chatbot's
    final ``details_collected`` outcome wins over ``missing_details`` because older
    states can retain stale or synonymous prompts after collection is complete.
    """
    filters = {
        "select": (
            "contact_id,page_id,status,stop_reason,collected_details,missing_details,"
            "last_inbound_at,last_bot_reply_at,contacts!inner("
            "id,page_id,name,psid,last_interaction_at,pipeline_stage)"
        ),
        "page_id": f"eq.{page_id}",
        "stop_reason": "eq.details_collected",
        "order": "contact_id.asc",
        "limit": 1000,
    }
    rows = []
    while True:
        batch = crm_request("chatbot_contact_states?" + urllib.parse.urlencode({**filters, "offset": len(rows)})) or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
    contacts: dict[str, dict] = {}
    for row in rows:
        contact = row.get("contacts") or {}
        if isinstance(contact, list):
            contact = contact[0] if contact else {}
        if (
            contact.get("page_id") == page_id
            and contact.get("id")
            and (contact.get("name") or "").strip()
            and contact.get("psid")
            and isinstance(row.get("collected_details"), dict)
            and row["collected_details"]
        ):
            contacts[contact["id"]] = {
                "id": contact["id"],
                "page_id": contact["page_id"],
                "psid": contact["psid"],
                "name": contact["name"].strip(),
                "last_interaction_at": contact.get("last_interaction_at"),
                "pipeline_stage": contact.get("pipeline_stage"),
                "status": row.get("status"),
                "stop_reason": row.get("stop_reason"),
                "collected_details": row.get("collected_details") or {},
                "missing_details": row.get("missing_details") or [],
                "last_inbound_at": row.get("last_inbound_at"),
                "last_bot_reply_at": row.get("last_bot_reply_at"),
            }
    return list(contacts.values())

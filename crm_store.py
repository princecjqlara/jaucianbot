"""Read paid and completed-detail contacts from the CRM Supabase project."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


PAID_TAG = re.compile(r"^paid(?:\s*/\s*availed\s*services?)?$", re.IGNORECASE)


class CrmSourceError(RuntimeError):
    def __init__(self, status: int, payload: dict, table: str | None = None):
        self.http_status = status
        self.query_table = table if table and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table) else None
        code = str(payload.get("code") or "")
        self.api_code = code if re.fullmatch(r"[A-Z0-9]{3,12}", code) else None
        match = re.search(r"(?:column|relation) [A-Za-z_][A-Za-z_0-9.]* does not exist", str(payload.get("message") or ""))
        self.schema_error = match.group(0) if match else None
        super().__init__(f"CRM Data API HTTP {status}" + (f" ({self.api_code})" if self.api_code else ""))


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
        try:
            payload = json.load(error)
            if not isinstance(payload, dict):
                payload = {}
        except (ValueError, UnicodeError):
            payload = {}
        raise CrmSourceError(error.code, payload, path.split("?", 1)[0]) from None
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


def completed_detail_contacts(page_id: str, *, exclude_handled: bool = False) -> list[dict]:
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
            "id,page_id,name,psid,last_interaction_at,pipeline_stage,chatbot_enabled)"
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
    blocked = set()
    if exclude_handled and rows:
        candidates = []
        for row in rows:
            contact = row.get("contacts") or {}
            if isinstance(contact, list):
                contact = contact[0] if contact else {}
            if contact.get("id"):
                candidates.append(str(contact["id"]))
        candidates = sorted(set(candidates))
        # Ownership reads concern only the completed briefs being considered,
        # not every outbound event and tagged contact on a large CRM page.
        for start in range(0, len(candidates), 100):
            blocked.update(blocked_contact_ids(page_id, contact_ids=set(candidates[start:start + 100])))
    for row in rows:
        contact = row.get("contacts") or {}
        if isinstance(contact, list):
            contact = contact[0] if contact else {}
        if (
            contact.get("page_id") == page_id
            and contact.get("id")
            and contact["id"] not in blocked
            and (not exclude_handled or str(contact.get("pipeline_stage") or "").casefold() not in {"paid", "converted", "won", "closed_won"})
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
                "chatbot_enabled": contact.get("chatbot_enabled"),
                "status": row.get("status"),
                "stop_reason": row.get("stop_reason"),
                "collected_details": row.get("collected_details") or {},
                "missing_details": row.get("missing_details") or [],
                "last_inbound_at": row.get("last_inbound_at"),
                "last_bot_reply_at": row.get("last_bot_reply_at"),
            }
    return list(contacts.values())


def blocked_contact_ids(page_id: str, contact_id: str | None = None, *, contact_ids: set[str] | None = None) -> set[str]:
    """Exclude paid/converted tags and recorded human intervention, read-only."""
    blocked = set()
    if contact_ids is not None and not contact_ids:
        return blocked
    tags = crm_request("tags?" + urllib.parse.urlencode({
        "select": "id,name,system_key", "page_id": f"eq.{page_id}", "limit": 1000,
    })) or []
    tag_ids = [str(t["id"]) for t in tags if PAID_TAG.fullmatch((t.get("name") or "").strip()) or
               (t.get("name") or "").strip().casefold() == "converted" or t.get("system_key") == "converted"]
    queries = []
    if tag_ids:
        queries.append(("contact_tags", {"tag_id": "in.(" + ",".join(tag_ids) + ")"}))
    queries.extend([
        ("outbound_message_events", {"page_id": f"eq.{page_id}", "actor_user_id": "not.is.null",
                                     "source_type": "not.in.(campaign,chatbot,workflow)"}),
        ("chatbot_interruption_events", {"page_id": f"eq.{page_id}", "actor_user_id": "not.is.null",
                                         "interruption_type": "neq.lead_stage_change"}),
    ])
    for table, filters in queries:
        if contact_id:
            filters["contact_id"] = f"eq.{contact_id}"
        elif contact_ids is not None:
            filters["contact_id"] = "in.(" + ",".join(sorted(contact_ids)) + ")"
        offset = 0
        while True:
            rows = crm_request(table + "?" + urllib.parse.urlencode({
                **filters, "select": "id,contact_id", "order": "contact_id.asc,id.asc", "limit": 1000, "offset": offset,
            })) or []
            blocked.update(str(r["contact_id"]) for r in rows if r.get("contact_id"))
            if len(rows) < 1000:
                break
            offset += 1000
    return blocked


def contact_assignable(page_id: str | None, contact_id: str) -> bool:
    if not page_id:
        rows = crm_request("contacts?" + urllib.parse.urlencode({
            "select": "page_id", "id": f"eq.{contact_id}", "limit": 1,
        })) or []
        page_id = rows[0]["page_id"] if rows else None
    if not page_id:
        return False
    rows = crm_request("contacts?" + urllib.parse.urlencode({
        "select": "id,pipeline_stage", "id": f"eq.{contact_id}", "page_id": f"eq.{page_id}", "limit": 1,
    })) or []
    if not rows or str(rows[0].get("pipeline_stage") or "").casefold() in {"paid", "converted", "won", "closed_won"}:
        return False
    states = crm_request("chatbot_contact_states?" + urllib.parse.urlencode({
        "select": "collected_details", "contact_id": f"eq.{contact_id}", "page_id": f"eq.{page_id}",
        "stop_reason": "eq.details_collected", "limit": 1,
    })) or []
    return bool(states and isinstance(states[0].get("collected_details"), dict) and states[0]["collected_details"]
                and contact_id not in blocked_contact_ids(page_id, contact_id))


def contact_identity_map(contact_ids: set[str]) -> dict[str, tuple[str, str]]:
    """Map existing CRM contact IDs to their stable page-scoped PSIDs."""
    result: dict[str, tuple[str, str]] = {}
    ordered = sorted(contact_id for contact_id in contact_ids if contact_id)
    for start in range(0, len(ordered), 100):
        chunk = ordered[start:start + 100]
        rows = crm_request("contacts?" + urllib.parse.urlencode({
            "select": "id,page_id,psid",
            "id": "in.(" + ",".join(chunk) + ")",
            "limit": len(chunk),
        })) or []
        for row in rows:
            if row.get("id") and row.get("page_id") and row.get("psid"):
                result[row["id"]] = (row["page_id"], row["psid"])
    return result

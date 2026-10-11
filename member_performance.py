"""Read-only assignment response and explicitly linked sales analytics."""
from __future__ import annotations

import datetime as dt
import re
import statistics
from decimal import Decimal

from deal_parser import normalize_page, normalized_text


TOKEN_RE = re.compile(r"(?im)^[ \t]*(?:client|assignment)[ \t]+token[ \t]*:[ \t]*([^\n]*)$")
CONTACT_RE = re.compile(r"(?im)^[ \t]*CRM[ \t]+contact[ \t]+ID[ \t]*:[ \t]*([^\n]*)$")
WITHDRAWN = {"assignment_delivery_failed", "assignee_inactive_before_first_delivery",
             "contact_already_acknowledged", "assignment_superseded"}


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def contact_key(row):
    p = row.get("payload") or {}
    if p.get("new_client_contact_identity"):
        return p["new_client_contact_identity"]
    if p.get("new_client_contact_page_id") and p.get("new_client_contact_psid"):
        return f"psid:{p['new_client_contact_page_id']}:{p['new_client_contact_psid']}"
    if p.get("new_client_contact_id"):
        return f"id:{p['new_client_contact_id']}"
    return f"offer:{row.get('id', p.get('new_client_token'))}"


def empty_client_metrics():
    return {**dict.fromkeys(("delivered_offers", "on_time_acceptances", "missed_offers",
        "pending_offers", "withdrawn_offers", "offers_with_unknown_deadline", "undelivered_assignments",
        "volunteer_claims", "response_samples", "accepted_clients", "converted_clients",
        "linked_dd_posts", "unlinked_dd_posts"), 0), "linked_gross": Decimal("0"),
        **dict.fromkeys(("on_time_response_percent", "average_response_minutes",
                         "median_response_minutes", "conversion_percent"))}


def build_client_performance(history, entries, config, start, end, now):
    """Offers use delivery dates; conversion cohorts use valid acceptance dates."""
    cutoff = min(end, now)
    members = {}
    evidence = []
    offers = [r for r in history if (r.get("payload") or {}).get("new_client_token")]
    # Merge CRM row replacements using page-scoped identities when available.
    identities = {r["payload"]["new_client_contact_id"]: contact_key(r) for r in offers
                  if r["payload"].get("new_client_contact_id") and
                  (r["payload"].get("new_client_contact_identity") or r["payload"].get("new_client_contact_psid"))}
    key = lambda r: identities.get(r["payload"].get("new_client_contact_id"), contact_key(r))

    def person(user_id, name=None):
        if user_id is None:
            return None
        user_id = int(user_id)
        target = members.setdefault(user_id, {
            "user_id": user_id, "name": name,
            "delivered_offers": 0, "on_time_acceptances": 0, "missed_offers": 0,
            "pending_offers": 0, "withdrawn_offers": 0, "offers_with_unknown_deadline": 0,
            "undelivered_assignments": 0, "volunteer_claims": 0,
            "accepted_clients": set(), "converted_clients": set(), "response_minutes": [],
            "linked_dd_posts": 0, "linked_gross": Decimal("0"), "unlinked_dd_posts": 0,
        })
        if name:
            target["name"] = name
        return target

    def acknowledged(row):
        p = row["payload"]
        if p.get("new_client_superseded_by") or p.get("new_client_revoked_acknowledged_at"):
            return None
        at = timestamp(p.get("new_client_acknowledged_at"))
        delivered = timestamp(p.get("_first_delivery_at") or row.get("sent_at"))
        return at if at and at <= cutoff and (not delivered or at >= delivered) else None

    accepted = [r for r in offers if acknowledged(r)]
    accepted_by_client = {}
    for row in accepted:
        accepted_by_client.setdefault(key(row), []).append(row)
    for row in offers:
        p = row["payload"]
        owner_id = p.get("new_client_assignee_id")
        original_id = p.get("new_client_original_assignee_id") or owner_id
        original_name = (p.get("new_client_original_assignee_name") if original_id != owner_id
                         else p.get("new_client_assignee_name"))
        delivered = timestamp(p.get("_first_delivery_at") or row.get("sent_at"))
        assigned = timestamp(p.get("new_client_assigned_at"))
        ack = acknowledged(row)
        timeout = p.get("new_client_direct_timeout_minutes") or p.get("new_client_response_minutes")
        reason = p.get("new_client_cancelled_reason")
        if not timeout:
            timeout = {"working_not_confirmed_within_30_minutes": 30,
                       "working_not_confirmed_within_one_hour": 60}.get(reason)
        deadline = delivered + dt.timedelta(minutes=int(timeout)) if delivered and timeout else None
        if delivered and start <= delivered < end and delivered <= cutoff:
            original = person(original_id, original_name)
            if original is not None:
                original["delivered_offers"] += 1
                cancelled = timestamp(p.get("new_client_cancelled_at"))
                day_end = dt.datetime.combine(delivered.astimezone(start.tzinfo).date() + dt.timedelta(days=1),
                                              dt.time.min, tzinfo=start.tzinfo)
                if deadline is None:
                    outcome = "offers_with_unknown_deadline"
                elif ack and owner_id == original_id and ack < deadline:
                    outcome = "on_time_acceptances"
                elif reason in WITHDRAWN or (reason == "assignment_day_ended" and deadline > day_end):
                    outcome = "withdrawn_offers"
                elif cancelled and cancelled < deadline and not ack and p.get("new_client_phase") != "volunteer":
                    outcome = "withdrawn_offers"
                elif cutoff >= deadline:
                    outcome = "missed_offers"
                else:
                    outcome = "pending_offers"
                original[outcome] += 1
                evidence.append({"kind": "offer", "assignment_id": row.get("id"),
                    "message_id": row.get("telegram_message_id"), "sent_utc": delivered.isoformat(),
                    "user_id": original_id, "outcome": outcome})
        elif not delivered and assigned and start <= assigned < end and assigned <= cutoff:
            original = person(original_id, original_name)
            if original is not None:
                original["undelivered_assignments"] += 1
        if ack and start <= ack < end:
            owner = person(owner_id, p.get("new_client_assignee_name"))
            if owner is not None:
                owner["accepted_clients"].add(key(row))
                if p.get("new_client_response_minutes") and delivered and ack >= delivered + dt.timedelta(minutes=20):
                    owner["volunteer_claims"] += 1
                elif delivered and owner_id == original_id:
                    owner["response_minutes"].append((ack - delivered).total_seconds() / 60)

    # Explicit tokens and CRM IDs identify a client; names never establish a link.
    token_index, contact_index = {}, {}
    for row in offers:
        token_index.setdefault(row["payload"]["new_client_token"].upper(), set()).add(key(row))
        cid = row["payload"].get("new_client_contact_id")
        if cid:
            contact_index.setdefault(cid, set()).add(key(row))
    for entry in entries:
        if entry["kind"] != "dd" or entry["count"] <= 0:
            continue
        row = entry["row"]
        sent = timestamp(row.get("sent_utc"))
        if not sent or not start <= sent < end or sent > cutoff:
            continue
        author = person(row.get("author_id"), row.get("author_name"))
        text = normalized_text(row.get("text") or "")
        identifiers = ([token_index.get(t.strip().upper(), set()) for t in TOKEN_RE.findall(text)] +
                       [contact_index.get(cid.strip(), set()) for cid in CONTACT_RE.findall(text)])
        candidates = set.intersection(*identifiers) if identifiers else set()
        why = "missing_client_reference" if not identifiers else "unknown_or_conflicting_reference"
        matched = None
        if len(candidates) == 1:
            client = next(iter(candidates))
            owners = [r for r in accepted_by_client.get(client, []) if acknowledged(r) <= sent]
            if owners:
                latest_time = max(acknowledged(r) for r in owners)
                latest = [r for r in owners if acknowledged(r) == latest_time]
                owner_ids = {r["payload"].get("new_client_assignee_id") for r in latest}
                matched = latest[0] if len(owner_ids) == 1 else None
            why = "no_confirmed_owner" if not matched else "owner_or_page_mismatch"
            if matched and (row.get("author_id") is None or
                    int(row["author_id"]) != int(matched["payload"]["new_client_assignee_id"]) or
                    normalize_page(matched["payload"].get("new_client_page") or "", config["pages"]) != entry["page"]):
                matched = None
        if matched and author is not None:
            author["linked_dd_posts"] += 1
            author["linked_gross"] += entry["gross"]
            if client in author["accepted_clients"]:
                author["converted_clients"].add(client)
            why = "linked"
        elif author is not None:
            author["unlinked_dd_posts"] += 1
        evidence.append({"kind": "sale", "message_id": row.get("message_id"),
            "sent_utc": sent.isoformat(), "user_id": row.get("author_id"), "outcome": why,
            "assignment_id": matched.get("id") if matched else None})

    for target in members.values():
        responses = target.pop("response_minutes")
        resolved = target["on_time_acceptances"] + target["missed_offers"]
        target["on_time_response_percent"] = round(100 * target["on_time_acceptances"] / resolved, 1) if resolved else None
        target["response_samples"] = len(responses)
        target["average_response_minutes"] = round(statistics.mean(responses), 2) if responses else None
        target["median_response_minutes"] = round(statistics.median(responses), 2) if responses else None
        target["accepted_clients"] = len(target["accepted_clients"])
        target["converted_clients"] = len(target["converted_clients"])
        target["conversion_percent"] = round(100 * target["converted_clients"] / target["accepted_clients"], 1) if target["accepted_clients"] else None
    return {"members": list(members.values()), "evidence": evidence, "definitions": {
        "offers": "First delivery within the report period; pending, withdrawn, undelivered and unknown-deadline offers are excluded from on-time response rate.",
        "response_time": "Direct WORKING confirmations in this period, measured from first delivery; volunteer claims are counted separately.",
        "conversion": "Unique clients accepted in this period with an explicitly linked DD by the same owner, on the same page, after acceptance and by the report cutoff; open clients remain in the denominator.",
        "linking": "Add Client token: ABCD1234 or CRM contact ID: <id> to a DD post. Conflicting references, unknown owners and name-only matches remain unlinked.",
        "completion": "A DD records a paid sale; WORKING records acceptance. Neither proves creative delivery or quality.",
    }}

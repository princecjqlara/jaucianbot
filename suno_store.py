"""Read completed Suno client details from its separate server-side Supabase project."""

from __future__ import annotations

import json
import datetime as dt
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter


SOURCE_ID = "suno:cxgynadprukyeuqbchbs"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
PATH_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?$")
REQUIRED_SETTINGS = (
    "SUNO_CLIENTS_TABLE", "SUNO_CLIENT_ID_COLUMN", "SUNO_CLIENT_NAME_COLUMN",
    "SUNO_CLIENT_DETAILS_COLUMN", "SUNO_COMPLETION_COLUMN", "SUNO_COMPLETION_VALUE",
)
DEFAULT_PAGE_NAME_COLUMN = "pages.name"
BLOCKED_HANDOFF_STATES = {
    "refusal", "opt_out", "opted_out", "not_qualified", "not_interested",
    "declined", "cancelled", "canceled", "paid", "converted", "won", "closed_won",
}


def suno_pages() -> dict[str, str]:
    """Public page identities for access polls, from Suno's own source only."""
    if not suno_configured():
        return {}
    project = (urllib.parse.urlsplit(os.environ.get("SUNO_SUPABASE_URL", "")).hostname or "").removesuffix(".supabase.co")
    pages, offset = {}, 0
    while True:
        rows = suno_request("pages?" + urllib.parse.urlencode({
            "select": "id,name", "order": "id.asc", "limit": 1000, "offset": offset,
        })) or []
        for row in rows:
            if row.get("id") and str(row.get("name") or "").strip():
                name = str(row["name"]).strip()
                if name in pages and pages[name] != f"suno:{project}:{row['id']}":
                    raise RuntimeError("Suno page names must be distinct for access polls")
                pages[name] = f"suno:{project}:{row['id']}"
        if len(rows) < 1000:
            return pages
        offset += len(rows)


def page_belongs_to_team(chat_id: int, page_name: str | None) -> bool:
    from daily_automation import MAKATA_CHAT_ID, SUNO_GROUPS
    if MAKATA_CHAT_ID not in SUNO_GROUPS:
        return True  # Preserve the original source until the explicit split is enabled.
    makata = re.sub(r"[^a-z0-9]", "", str(page_name or "").casefold()) in {"makatastudio", "makatastudios"}
    return makata if chat_id == MAKATA_CHAT_ID else not makata


def details_progress(details: dict, missing: list) -> tuple[int, int]:
    """Count unique required fields; blank or still-missing values are unfinished."""
    normalized = {str(field).strip().casefold(): value for field, value in details.items() if str(field).strip()}
    missing_keys = {str(field).strip().casefold() for field in missing if str(field).strip()}
    required = set(normalized).union(missing_keys)
    collected = sum(
        field not in missing_keys and value not in (None, "", [], {})
        and (not isinstance(value, str) or bool(value.strip()))
        for field, value in normalized.items()
    )
    return collected, len(required)


def suno_assignment_matches_project(action: dict) -> bool:
    """Keep old assignments from other databases out of the Suno workflow."""
    expected = os.environ.get("SUNO_EXPECTED_PROJECT_REF", "").strip()
    host = urllib.parse.urlsplit(os.environ.get("SUNO_SUPABASE_URL", "")).hostname
    project = expected or (host.removesuffix(".supabase.co") if host else SOURCE_ID.split(":", 1)[1])
    prefix = f"suno:{project}"
    page = (action.get("payload") or {}).get("new_client_contact_page_id") or ""
    return page == prefix or str(page).startswith(prefix + ":")


def suno_configured() -> bool:
    host = urllib.parse.urlsplit(os.environ.get("SUNO_SUPABASE_URL", "")).hostname
    expected = os.environ.get("SUNO_EXPECTED_PROJECT_REF", "").strip()
    return bool(
        os.environ.get("SUNO_ASSIGNMENTS_ENABLED", "").strip().lower() == "true"
        and
        os.environ.get("SUNO_SUPABASE_URL", "").startswith("https://")
        and os.environ.get("SUNO_SUPABASE_SERVICE_ROLE_KEY")
        and (not expected or host == f"{expected}.supabase.co")
        and all(os.environ.get(name, "").strip() for name in REQUIRED_SETTINGS)
    )


def suno_request(path: str):
    url = os.environ.get("SUNO_SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUNO_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url.startswith("https://") or not key:
        raise RuntimeError("Suno Supabase credentials are not configured")
    expected = os.environ.get("SUNO_EXPECTED_PROJECT_REF", "").strip()
    if expected and urllib.parse.urlsplit(url).hostname != f"{expected}.supabase.co":
        raise RuntimeError("Suno Supabase project does not match the verified project")
    request = urllib.request.Request(
        url + "/rest/v1/" + path,
        headers={"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Suno Data API HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Suno network error ({type(error.reason).__name__})") from None


def completed_suno_contacts(*, contact_id: str | None = None, diagnostics: dict | None = None) -> list[dict]:
    """Read completed briefs, or Suno handoffs meeting the configured percentage.

    Completion stays the default; partial handoffs require a separate setting
    and retain missing fields rather than claiming the brief is complete.
    """
    settings = {name: os.environ.get(name, "").strip() for name in REQUIRED_SETTINGS}
    if not all(settings.values()):
        raise RuntimeError("Suno client table and completion mapping are not configured")
    for name, value in settings.items():
        if name != "SUNO_COMPLETION_VALUE" and not PATH_IDENTIFIER.fullmatch(value):
            raise RuntimeError("Invalid Suno table or column mapping")
    table, id_col, name_col, details_col, complete_col, complete_value = (
        settings[name] for name in REQUIRED_SETTINGS
    )
    try:
        minimum_percent = int(os.environ.get("SUNO_MIN_DETAILS_PERCENT", "100"))
    except ValueError:
        raise RuntimeError("Invalid Suno details percentage") from None
    if not 1 <= minimum_percent <= 100:
        raise RuntimeError("Invalid Suno details percentage")
    allow_partial = table == "chatbot_contact_states" and minimum_percent < 100
    optional = {
        name: os.environ.get(name, "").strip()
        for name in ("SUNO_CLIENT_IDENTITY_COLUMN", "SUNO_CLIENT_DATE_COLUMN")
    }
    page_name_col = os.environ.get("SUNO_PAGE_NAME_COLUMN", DEFAULT_PAGE_NAME_COLUMN).strip()
    if page_name_col:
        optional["SUNO_PAGE_NAME_COLUMN"] = page_name_col
    if any(value and not PATH_IDENTIFIER.fullmatch(value) for value in optional.values()):
        raise RuntimeError("Invalid Suno optional column mapping")
    direct_columns = {id_col, details_col, complete_col}
    if table == "chatbot_contact_states":
        direct_columns.update({"page_id", "missing_details"})
        if allow_partial:
            direct_columns.add("status")
    relation_fields: dict[str, set[str]] = {}
    for setting in (name_col, *filter(None, optional.values())):
        if "." in setting:
            relation, field = setting.split(".", 1)
            relation_fields.setdefault(relation, set()).add(field)
        else:
            direct_columns.add(setting)
    if allow_partial:
        relation_fields.setdefault("contacts", set()).add("pipeline_stage")
    select_columns = list(dict.fromkeys(sorted(direct_columns)))
    for relation, fields in sorted(relation_fields.items()):
        select_columns.append(f"{relation}!inner({','.join(sorted(fields))})")
    contacts: dict[str, dict] = {}
    project = (urllib.parse.urlsplit(os.environ.get("SUNO_SUPABASE_URL", "")).hostname or "").removesuffix(".supabase.co")
    source_id = f"suno:{project}"
    offset = 0
    while True:
        filters = {
            "select": ",".join(select_columns),
            "order": f"{id_col}.asc", "limit": 1000, "offset": offset,
        }
        if not allow_partial:
            filters[complete_col] = f"eq.{complete_value}"
        if contact_id is not None:
            filters[id_col] = f"eq.{contact_id}"
        rows = suno_request(table + "?" + urllib.parse.urlencode(filters)) or []
        for row in rows:
            outcome = row.get(complete_col)
            outcome = str(outcome).lower() if isinstance(outcome, bool) else str(outcome)
            details = row.get(details_col)
            client_id = row.get(id_col)
            def mapped_value(mapping: str):
                if "." not in mapping:
                    return row.get(mapping)
                relation, field = mapping.split(".", 1)
                value = row.get(relation) or {}
                if isinstance(value, list):
                    value = value[0] if value else {}
                return value.get(field) if isinstance(value, dict) else None
            name = mapped_value(name_col)
            complete = outcome == complete_value and not row.get("missing_details")
            missing = row.get("missing_details")
            collected_count, required_count = details_progress(details, missing) if (
                isinstance(details, dict) and isinstance(missing, list)
            ) else (0, 0)
            # Partial handoffs depend on collected data, not whether the chatbot
            # is running or which positive completion label the site currently uses.
            blocked = any(
                str(value or "").strip().casefold() in BLOCKED_HANDOFF_STATES
                for value in (row.get("status"), row.get(complete_col), mapped_value("contacts.pipeline_stage"))
            )
            partial = (
                allow_partial and not blocked
                and required_count > 0
                and collected_count * 100 >= minimum_percent * required_count
            )
            if diagnostics is not None:
                reason = (
                    "blocked_outcome" if allow_partial and blocked else
                    "below_details_threshold" if not (complete or partial) else
                    "missing_contact_id" if client_id is None or not str(client_id).strip() else
                    "missing_name" if not isinstance(name, str) or not name.strip() else
                    "missing_details" if not isinstance(details, dict) or not details else
                    "missing_identity" if optional["SUNO_CLIENT_IDENTITY_COLUMN"] and not str(
                        mapped_value(optional["SUNO_CLIENT_IDENTITY_COLUMN"]) or "").strip() else
                    "eligible"
                )
                page_name = mapped_value(page_name_col) if page_name_col else None
                page_stats = diagnostics.setdefault(str(page_name or "Suno"), {
                    "states_scanned": 0, "eligibility": {}, "stop_reasons": {},
                    "pipeline_stages": {}, "latest_interaction_utc": None,
                    "excluded_progress": {},
                })
                page_stats["states_scanned"] += 1
                for key, value in (("eligibility", reason), ("stop_reasons", row.get(complete_col)),
                                   ("pipeline_stages", mapped_value("contacts.pipeline_stage"))):
                    label = str(value or "(none)")
                    page_stats[key][label] = page_stats[key].get(label, 0) + 1
                date_col = optional["SUNO_CLIENT_DATE_COLUMN"]
                interaction = mapped_value(date_col) if date_col else None
                if interaction and str(interaction) > (page_stats["latest_interaction_utc"] or ""):
                    page_stats["latest_interaction_utc"] = str(interaction)
                if reason == "below_details_threshold":
                    progress = f"{collected_count}/{required_count}"
                    counts = page_stats["excluded_progress"]
                    counts[progress] = counts.get(progress, 0) + 1
            if (
                not (complete or partial) or (allow_partial and blocked)
                or client_id is None or not str(client_id).strip()
                or not isinstance(name, str) or not name.strip()
                or not isinstance(details, dict) or not details
            ):
                continue
            client_id = str(client_id)
            identity_col = optional["SUNO_CLIENT_IDENTITY_COLUMN"]
            identity = mapped_value(identity_col) if identity_col else client_id
            if identity is None or not str(identity).strip():
                continue
            contacts[client_id] = {
                "id": client_id, "page_id": f"{source_id}:{row['page_id']}" if row.get("page_id") else source_id, "psid": str(identity),
                "name": name.strip(), "collected_details": details,
                "page_name": (str(mapped_value(page_name_col)).strip()
                               if page_name_col and mapped_value(page_name_col) else None),
                "last_interaction_at": mapped_value(optional["SUNO_CLIENT_DATE_COLUMN"]) if optional["SUNO_CLIENT_DATE_COLUMN"] else None,
                "stop_reason": row.get(complete_col),
                "pipeline_stage": mapped_value("contacts.pipeline_stage") if allow_partial else None,
                "details_complete": bool(complete), "missing_details": missing or [],
                "details_collected_count": collected_count, "details_required_count": required_count,
                "details_percent": round(100 * collected_count / required_count, 1) if required_count else None,
            }
        if len(rows) < 1000:
            return list(contacts.values())
        offset += len(rows)


def suno_supply_status() -> dict:
    """Read aggregate supply diagnostics without exposing client briefs or identities."""
    if not suno_configured():
        raise RuntimeError("Suno source is not configured")
    diagnostics: dict = {}
    eligible = completed_suno_contacts(diagnostics=diagnostics)
    result = {"minimum_details_percent": int(os.environ.get("SUNO_MIN_DETAILS_PERCENT", "100")),
              "eligible_contacts": len(eligible), "pages": diagnostics}
    if os.environ.get("SUNO_CLIENTS_TABLE", "").strip() == "chatbot_contact_states":
        states = sum(page["states_scanned"] for page in diagnostics.values())
        contacts = 0
        latest = None
        stages = Counter()
        while True:
            rows = suno_request("contacts?" + urllib.parse.urlencode({
                "select": "id,pipeline_stage,last_interaction_at", "order": "id.asc",
                "limit": 1000, "offset": contacts,
            })) or []
            contacts += len(rows)
            stages.update(str(row.get("pipeline_stage") or "(none)") for row in rows)
            latest = max([str(row["last_interaction_at"]) for row in rows if row.get("last_interaction_at")]
                         + ([latest] if latest else []), default=None)
            if len(rows) < 1000:
                break
        result.update({"total_contacts": contacts, "joined_chatbot_states": states,
                       "contact_pipeline_stages": dict(stages), "latest_contact_interaction_utc": latest})
    return result


def suno_chatbot_status() -> dict:
    """Read chatbot settings and recent processing activity, omitting customer content."""
    if not suno_configured():
        raise RuntimeError("Suno source is not configured")
    schema = suno_request("") or {}
    definitions = schema.get("definitions") or {}
    coverage_errors = {}

    def read(table: str, fields: list[str], *, date_col: str | None = None) -> list[dict]:
        available = (definitions.get(table) or {}).get("properties") or {}
        if not available or (date_col and date_col not in available):
            coverage_errors[table] = "diagnostic_source_unavailable"
            return []
        columns = [field for field in fields if field in available]
        params = {"select": ",".join(columns), "limit": 1000}
        if date_col:
            params.update({date_col: "gte." + since, "order": date_col + ".desc"})
        elif "page_id" in available:
            params["order"] = "page_id.asc"
        try:
            return suno_request(table + "?" + urllib.parse.urlencode(params)) or []
        except RuntimeError as error:
            match = re.fullmatch(r"Suno Data API HTTP (\d+)", str(error))
            coverage_errors[table] = "http_" + match.group(1) if match else "diagnostic_read_failed"
            return []

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    page_names = {str(row["id"]): str(row.get("name") or "Suno")
                  for row in read("pages", ["id", "name"]) if row.get("id") is not None}
    configs = read("chatbot_configs", [
        "page_id", "enabled", "updated_at", "trial_mode_enabled", "stop_when_details_collected",
        "stop_on_qualified", "stop_on_order_created", "details_completion_percent", "details_to_collect",
    ])
    result = {}
    for row in configs:
        page = page_names.get(str(row.get("page_id")), "Suno")
        fields = row.get("details_to_collect")
        result[str(row["page_id"])] = {
            "page": page, "config": {key: row.get(key) for key in (
                "enabled", "updated_at", "trial_mode_enabled", "stop_when_details_collected",
                "stop_on_qualified", "stop_on_order_created", "details_completion_percent",
            )},
            "required_details_count": len(fields) if isinstance(fields, (dict, list)) else None,
        }
    for table, select, date_col, prefix in (
        ("chatbot_reply_events", "page_id,status,created_at", "created_at", "reply_events"),
        ("contact_interactions", "page_id,is_from_contact,interaction_at", "interaction_at", "interactions"),
        ("chatbot_contact_states", "page_id,status,stop_reason,last_inbound_at,last_bot_reply_at", "last_inbound_at", "states"),
    ):
        rows = read(table, select.split(","), date_col=date_col)
        for row in rows:
            page = result.setdefault(str(row["page_id"]), {"page": page_names.get(str(row["page_id"]), "Suno"), "config": None})
            stats = page.setdefault(prefix, {"count": 0, "statuses": {}, "latest_utc": None})
            stats["count"] += 1
            label = ("inbound" if row.get("is_from_contact") else "outbound") if prefix == "interactions" else str(row.get("status") or "(none)")
            stats["statuses"][label] = stats["statuses"].get(label, 0) + 1
            stats["latest_utc"] = max(stats["latest_utc"] or "", str(row.get(date_col) or "")) or None
            if prefix == "states":
                stats["latest_bot_reply_utc"] = max(stats.get("latest_bot_reply_utc") or "", str(row.get("last_bot_reply_at") or "")) or None
        for page in result.values():
            page.setdefault(prefix, {"count": 0, "statuses": {}, "latest_utc": None})
        for page in result.values():
            page[prefix]["sample_capped"] = len(rows) == 1000
    return {"since_utc": since, "pages": list(result.values()), "coverage_errors": coverage_errors,
            "diagnostic_columns": {table: [field for field in (definitions.get(table, {}).get("properties") or {})
                if field not in {"instructions", "fallback_reply", "bot_dos", "bot_donts", "access_token"}]
                for table in ("chatbot_configs", "chatbot_contact_states", "chatbot_reply_events", "contact_interactions")},
            "coverage": "Latest 1000 rows per source in the last 24 hours; capped sources are partial."}


def suno_contact_assignable(action: dict) -> bool:
    """Recheck Suno handoffs in their mapped source, never in the Veo CRM."""
    from daily_automation import SUNO_GROUPS
    if action.get("chat_id") not in SUNO_GROUPS or not suno_configured() or not suno_assignment_matches_project(action):
        return False
    p = action["payload"]
    return any(page_belongs_to_team(int(action["chat_id"]), c.get("page_name")) and c["id"] == p["new_client_contact_id"] and
               c["page_id"] == p["new_client_contact_page_id"] and
               (not p.get("new_client_contact_psid") or c["psid"] == p["new_client_contact_psid"])
               for c in completed_suno_contacts(contact_id=p["new_client_contact_id"]))

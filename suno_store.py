"""Read completed Suno client details from its separate server-side Supabase project."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


SOURCE_ID = "suno:cxgynadprukyeuqbchbs"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
PATH_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?$")
REQUIRED_SETTINGS = (
    "SUNO_CLIENTS_TABLE", "SUNO_CLIENT_ID_COLUMN", "SUNO_CLIENT_NAME_COLUMN",
    "SUNO_CLIENT_DETAILS_COLUMN", "SUNO_COMPLETION_COLUMN", "SUNO_COMPLETION_VALUE",
)
DEFAULT_PAGE_NAME_COLUMN = "pages.name"


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


def completed_suno_contacts() -> list[dict]:
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
            positive_handoff = (
                row.get("status") == "active" and row.get(complete_col) is None
                or row.get("status") == "stopped" and outcome in {"qualified", complete_value}
            )
            partial = (
                allow_partial and positive_handoff
                and mapped_value("contacts.pipeline_stage") not in {"opted_out", "not_qualified"}
                and required_count > 0
                and collected_count * 100 >= minimum_percent * required_count
            )
            if (
                not (complete or partial)
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

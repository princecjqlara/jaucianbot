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
    """Require an explicitly mapped completion outcome and non-empty details object.

    No table or completion rule is guessed. This prevents partially collected
    Suno conversations from becoming assignments when a schema is unverified.
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
    relation_fields: dict[str, set[str]] = {}
    for setting in (name_col, *filter(None, optional.values())):
        if "." in setting:
            relation, field = setting.split(".", 1)
            relation_fields.setdefault(relation, set()).add(field)
        else:
            direct_columns.add(setting)
    select_columns = list(dict.fromkeys(sorted(direct_columns)))
    for relation, fields in sorted(relation_fields.items()):
        select_columns.append(f"{relation}!inner({','.join(sorted(fields))})")
    contacts: dict[str, dict] = {}
    project = (urllib.parse.urlsplit(os.environ.get("SUNO_SUPABASE_URL", "")).hostname or "").removesuffix(".supabase.co")
    source_id = f"suno:{project}"
    offset = 0
    while True:
        rows = suno_request(table + "?" + urllib.parse.urlencode({
            "select": ",".join(select_columns), complete_col: f"eq.{complete_value}",
            "order": f"{id_col}.asc", "limit": 1000, "offset": offset,
        })) or []
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
            if (
                outcome != complete_value
                or client_id is None or not str(client_id).strip()
                or not isinstance(name, str) or not name.strip()
                or not isinstance(details, dict) or not details
                or bool(row.get("missing_details"))
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
                "stop_reason": "details_collected",
            }
        if len(rows) < 1000:
            return list(contacts.values())
        offset += len(rows)

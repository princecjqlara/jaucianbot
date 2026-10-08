"""Execute the archive's bounded Data API operations through its existing PostgreSQL connection.

The CRM keeps its separate HTTP connection. SQL identifiers and values are always
quoted/parameterized; only archive tables and the existing RPCs are accepted.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading
import urllib.parse

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


TABLES = {"chats", "messages", "scheduled_actions", "daily_poll_answers"}
RPCS = {
    "insights_ingest_update": (("p_update", "jsonb"), ("p_allowed_ids", "bigint[]")),
    "insights_ingest_poll_answer": (("p_update", "jsonb"), ("p_allowed_ids", "bigint[]")),
    "insights_status": (("p_allowed_ids", "bigint[]"),),
    "insights_messages": (("p_allowed_ids", "bigint[]"), ("p_since", "timestamptz"),
                          ("p_limit", "integer"), ("p_group", "bigint"), ("p_query", "text")),
    "insights_claim_scheduled_actions": (("p_allowed_ids", "bigint[]"), ("p_limit", "integer")),
    "insights_finish_scheduled_action": (("p_id", "bigint"), ("p_success", "boolean"),
                                        ("p_telegram_message_id", "bigint"), ("p_error", "text")),
    "insights_register_daily_poll": (("p_poll_id", "text"), ("p_chat_id", "bigint"),
                                     ("p_thread_id", "bigint"), ("p_work_date", "date"),
                                     ("p_telegram_message_id", "bigint")),
    "insights_daily_poll_counts": (("p_work_date", "date"), ("p_allowed_ids", "bigint[]")),
}
BOOLEAN_RPCS = {"insights_ingest_update", "insights_ingest_poll_answer",
                "insights_finish_scheduled_action", "insights_register_daily_poll"}
INTEGER_COLUMNS = {"id", "chat_id", "message_id", "author_id", "reply_to_message_id",
                   "thread_id", "user_id", "telegram_message_id", "attempts", "repeat_interval_minutes"}
_lock = threading.Lock()
_connection = None


def _identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError("Unsupported archive identifier")
    return name


def _expression(value: str):
    match = re.fullmatch(r"(?:(daily_polls)\.)?([A-Za-z_][A-Za-z_0-9]*)(?:(->>?)\s*([A-Za-z_][A-Za-z_0-9]*))?", value)
    if not match:
        raise ValueError("Unsupported archive expression")
    relation, column, operator, key = match.groups()
    expression = sql.Identifier(relation or "a", column)
    if operator:
        expression = sql.SQL("{} {} {}").format(expression, sql.SQL(operator), sql.Literal(key))
    return expression


def _split_selection(value: str) -> list[str]:
    result, depth, start = [], 0, 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(value[start:index])
            start = index + 1
    if depth != 0:
        raise ValueError("Unsupported archive selection")
    return result + [value[start:]]


def _selection(value: str):
    expressions, join = [], False
    for part in _split_selection(value):
        if part == "*":
            expressions.append(sql.SQL('"a".*'))
        elif part == "daily_polls!inner(work_date,chat_id)":
            join = True
            expressions.append(sql.SQL("jsonb_build_object('work_date', daily_polls.work_date, "
                                       "'chat_id', daily_polls.chat_id) AS daily_polls"))
        else:
            alias, separator, field = part.partition(":")
            expression = _expression(field if separator else alias)
            if separator:
                expression = sql.SQL("{} AS {}").format(expression, sql.Identifier(_identifier(alias)))
            expressions.append(expression)
    return sql.SQL(",").join(expressions), join


def _filter_value(column: str, value: str):
    if "->" in column:
        return value
    column = column.split(".")[-1]
    if column in INTEGER_COLUMNS:
        return int(value)
    if column == "active":
        if value not in {"true", "false"}:
            raise ValueError("Unsupported boolean filter")
        return value == "true"
    if column.endswith("_at") or column.endswith("_utc"):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if column == "work_date":
        return dt.date.fromisoformat(value)
    return value


def _where(params: dict[str, list[str]]):
    conditions, values = [], []
    for column, filters in params.items():
        if column in {"select", "order", "limit", "offset", "on_conflict"}:
            continue
        expression = _expression(column)
        for filter_value in filters:
            operation, _, value = filter_value.partition(".")
            if operation == "is" and value == "null":
                conditions.append(sql.SQL("{} IS NULL").format(expression))
            elif operation == "in" and value.startswith("(") and value.endswith(")"):
                members = [_filter_value(column, item) for item in value[1:-1].split(",")]
                conditions.append(sql.SQL("{} = ANY(%s)").format(expression))
                values.append(members)
            elif operation in {"eq", "gte", "lte", "lt", "gt", "like", "ilike"}:
                operator = {"eq": "=", "gte": ">=", "lte": "<=", "lt": "<", "gt": ">",
                            "like": "LIKE", "ilike": "ILIKE"}[operation]
                conditions.append(sql.SQL("{} {} %s").format(expression, sql.SQL(operator)))
                values.append(value.strip('"').replace("*", "%") if operation in {"like", "ilike"}
                              else _filter_value(column, value))
            else:
                raise ValueError("Unsupported archive filter")
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL(""), values


def compile_request(path: str, payload=None, *, prefer=None, method=None):
    """Return a parameterized query, its arguments, and the result shape."""
    route, _, query = path.partition("?")
    method = method or ("POST" if payload is not None else "GET")
    if route.startswith("rpc/"):
        name = route[4:]
        if method != "POST" or name not in RPCS or not isinstance(payload, dict):
            raise ValueError("Unsupported archive RPC")
        arguments, values = [], []
        for key, cast in RPCS[name]:
            arguments.append(sql.SQL("{} => %s::{}").format(sql.Identifier(key), sql.SQL(cast)))
            value = payload[key]
            values.append(Jsonb(value) if cast == "jsonb" else value)
        return (sql.SQL("SELECT * FROM {}({})").format(sql.Identifier("public", name), sql.SQL(",").join(arguments)),
                values, "boolean" if name in BOOLEAN_RPCS else "rows")
    if route not in TABLES or method not in {"GET", "POST", "PATCH"}:
        raise ValueError("Unsupported archive operation")
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    latest_guard = params.pop("new_client_latest_guard", None)
    if latest_guard is not None and (latest_guard != ["true"] or route != "scheduled_actions" or method != "PATCH"
                                    or not isinstance(payload, dict) or not payload.get("payload", {}).get("new_client_acknowledged_at")
                                    or "id" not in params):
        raise ValueError("Latest-client guard requires a bounded ownership confirmation")
    selection, join = _selection(params.get("select", ["*"])[0])
    where, values = _where(params)
    table = sql.Identifier("public", route)
    if method == "GET":
        statement = sql.SQL("SELECT {} FROM {} AS a").format(selection, table)
        if join:
            if route != "daily_poll_answers":
                raise ValueError("Unsupported archive join")
            statement += sql.SQL(" JOIN public.daily_polls AS daily_polls USING (poll_id)")
        statement += where
        if "order" in params:
            ordering = []
            for item in params["order"][0].split(","):
                field, _, direction = item.partition(".")
                if direction not in {"asc", "desc"}:
                    raise ValueError("Unsupported archive order")
                ordering.append(sql.SQL("{} {}").format(_expression(field), sql.SQL(direction.upper())))
            statement += sql.SQL(" ORDER BY ") + sql.SQL(",").join(ordering)
        limit, offset = int(params.get("limit", ["1000"])[0]), int(params.get("offset", ["0"])[0])
        if not 1 <= limit <= 10000 or offset < 0:
            raise ValueError("Unsupported archive range")
        return statement + sql.SQL(" LIMIT %s OFFSET %s"), values + [limit, offset], "rows"
    if join:
        raise ValueError("Unsupported archive write selection")
    if method == "PATCH":
        has_filters = any(key not in {"select", "order", "limit", "offset", "on_conflict"} for key in params)
        if not has_filters or not isinstance(payload, dict) or not payload:
            raise ValueError("Archive updates require filters and values")
        assignments, changes = [], []
        for key, value in payload.items():
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(_identifier(key))))
            changes.append(Jsonb(value) if isinstance(value, dict) else value)
        statement = sql.SQL("UPDATE {} AS a SET {}").format(table, sql.SQL(",").join(assignments)) + where
        values = changes + values
        if latest_guard:
            details = payload["payload"]
            identity = details.get("new_client_contact_identity")
            name = re.sub(r"[^a-z0-9]", "", (details.get("new_client_contact_name") or "").casefold())
            statement += sql.SQL(""" AND NOT EXISTS (
                SELECT 1 FROM public.scheduled_actions AS newer
                WHERE newer.chat_id = a.chat_id AND newer.id > a.id
                  AND newer.dedupe_key LIKE 'new-client:%'
                  AND (newer.payload->>'new_client_contact_id' = %s
                    OR newer.payload->>'new_client_contact_identity' = %s
                    OR (%s::text IS NULL AND COALESCE(newer.payload->>'new_client_contact_identity', '') = ''
                        AND lower(newer.payload->>'new_client_page') = %s
                        AND regexp_replace(lower(newer.payload->>'new_client_contact_name'), '[^a-z0-9]', '', 'g') = %s
                        AND %s <> ''))
            )""")
            values += [details.get("new_client_contact_id"), identity, identity,
                       (details.get("new_client_page") or "").casefold(), name, name]
    else:
        rows = payload if isinstance(payload, list) else [payload]
        if not rows or any(not isinstance(row, dict) or not row for row in rows):
            raise ValueError("Unsupported archive insert")
        keys = sorted(set().union(*(row.keys() for row in rows)))
        columns = sql.SQL(",").join(sql.Identifier(_identifier(key)) for key in keys)
        value_rows, values = [], []
        for row in rows:
            placeholders = []
            for key in keys:
                if key not in row:
                    placeholders.append(sql.SQL("DEFAULT"))
                else:
                    placeholders.append(sql.Placeholder())
                    value = row[key]
                    values.append(Jsonb(value) if isinstance(value, dict) else value)
            value_rows.append(sql.SQL("({})").format(sql.SQL(",").join(placeholders)))
        statement = sql.SQL("INSERT INTO {} AS a ({}) VALUES {}").format(table, columns, sql.SQL(",").join(value_rows))
        if "on_conflict" in params:
            conflict = sql.SQL(",").join(sql.Identifier(_identifier(key)) for key in params["on_conflict"][0].split(","))
            statement += sql.SQL(" ON CONFLICT ({}) ").format(conflict)
            if prefer and "resolution=ignore-duplicates" in prefer:
                statement += sql.SQL("DO NOTHING")
            elif prefer and "resolution=merge-duplicates" in prefer:
                statement += sql.SQL("DO UPDATE SET ") + sql.SQL(",").join(
                    sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(key), sql.Identifier(key)) for key in keys
                )
            else:
                raise ValueError("Unsupported archive conflict policy")
    if prefer and "return=minimal" in prefer:
        return statement, values, "minimal"
    return statement + sql.SQL(" RETURNING ") + selection, values, "rows"


def _normalize(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def request(path: str, payload=None, *, prefer=None, method=None):
    global _connection
    statement, values, shape = compile_request(path, payload, prefer=prefer, method=method)
    with _lock:
        try:
            if _connection is None or _connection.closed:
                url = os.environ.get("SUPABASE_DB_URL", "").strip()
                if not url:
                    raise RuntimeError("SUPABASE_DB_URL is required for the PostgreSQL archive transport")
                _connection = psycopg.connect(url, sslmode="require", connect_timeout=10, autocommit=True,
                                              prepare_threshold=None, row_factory=dict_row)
            with _connection.cursor() as cursor:
                cursor.execute(statement, values)
                if shape == "minimal":
                    return None
                rows = cursor.fetchall()
                return bool(next(iter(rows[0].values()))) if shape == "boolean" else _normalize(rows)
        except psycopg.Error as error:
            if _connection is not None:
                _connection.close()
                _connection = None
            # Never expose connection strings or raw SQL error messages.
            raise RuntimeError(f"Archive PostgreSQL error ({error.sqlstate or type(error).__name__})") from None

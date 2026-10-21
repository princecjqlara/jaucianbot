"""Manage the archive database's one-minute dispatcher without exporting credentials."""

from __future__ import annotations

import datetime as dt
import json
import os

import psycopg
from psycopg import sql
from automation_controls import enabled_chat_ids


JOB_NAME = "telegram-insights-dispatch"
WATCHDOG_NAME = "telegram-insights-dispatch-watchdog"
CHECKPOINT = "database-dispatch-checkpoint"
DISPATCH_URL = "https://jaucianassitant.vercel.app/api/cron/dispatch"


class SchedulerError(RuntimeError):
    pass


def _connect():
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not url:
        raise SchedulerError("database_connection_not_configured")
    return psycopg.connect(url, sslmode="require", connect_timeout=10, prepare_threshold=None)


def _snapshot(conn, allowed: set[int] | None = None) -> dict:
    extensions = dict(conn.execute(
        "select name, installed_version from pg_available_extensions where name in ('pg_cron', 'pg_net')"
    ).fetchall())
    result = {"configured": False, "extensions": extensions, "job": None, "last_delivery": None}
    if not extensions.get("pg_cron"):
        return result
    row = conn.execute(
        "select jobid, schedule, active from cron.job where jobname = %s", (JOB_NAME,),
    ).fetchone()
    if not row:
        return result
    result["job"] = {"id": row[0], "schedule": row[1], "active": row[2]}
    result["configured"] = bool(row[2] and row[1] == "* * * * *" and extensions.get("pg_net"))
    watchdog = conn.execute(
        "select jobid, schedule, active from cron.job where jobname = %s", (WATCHDOG_NAME,),
    ).fetchone()
    result["watchdog"] = ({"id": watchdog[0], "schedule": watchdog[1], "active": watchdog[2]}
                          if watchdog else None)
    if watchdog:
        watchdog_run = conn.execute(
            "select status, start_time, end_time from cron.job_run_details where jobid = %s order by runid desc limit 1",
            (watchdog[0],),
        ).fetchone()
        if watchdog_run:
            result["last_watchdog_run"] = {"status": watchdog_run[0], "started_at": watchdog_run[1],
                                           "ended_at": watchdog_run[2]}
    runs = conn.execute(
        "select status, start_time, end_time from cron.job_run_details where jobid = %s order by runid desc limit 1",
        (row[0],),
    ).fetchone()
    if runs:
        result["last_database_run"] = {"status": runs[0], "started_at": runs[1], "ended_at": runs[2]}
    if not extensions.get("pg_net"):
        return result
    checkpoint = conn.execute(
        "select payload from public.scheduled_actions where dedupe_key = %s and status = 'cancelled'", (CHECKPOINT,),
    ).fetchone()
    meta = checkpoint[0] if checkpoint else {}
    result["last_requested_at"] = meta.get("requested_at")
    request_ids = [value for value in (meta.get("request_id"), meta.get("previous_request_id"))
                   if isinstance(value, int) and not isinstance(value, bool)]
    response = conn.execute(
        "select id, status_code, timed_out, error_msg is not null, content, created "
        "from net._http_response where id = any(%s) order by id desc limit 1", (request_ids,),
    ).fetchone() if request_ids else None
    if response:
        delivery = {"request_id": response[0], "http_status": response[1], "timed_out": response[2],
                    "transport_error": response[3], "received_at": response[5]}
        try:
            body = json.loads(response[4] or "{}")
        except (ValueError, TypeError):
            body = {}
        if isinstance(body, dict):
            delivery["result"] = {key: body[key] for key in
                                  ("ok", "queued", "processed", "sent", "failed", "queue_errors") if key in body}
        result["last_delivery"] = delivery
    if allowed is not None:
        queue = conn.execute(
            "select count(*) filter (where status = 'pending' and scheduled_for < now() - interval '5 minutes'), "
            "count(*) filter (where status = 'failed'), "
            "count(*) filter (where status = 'processing' and updated_at < now() - interval '15 minutes') "
            "from public.scheduled_actions where chat_id = any(%s) and status in ('pending','processing','failed')",
            (sorted(enabled_chat_ids(allowed)),),
        ).fetchone()
        result["queue"] = {"overdue_over_five_minutes": queue[0], "failed": queue[1], "stale_claims": queue[2]}
    return result


def health_reasons(state: dict, now: dt.datetime | None = None) -> list[str]:
    """Require recent successful execution, rather than just an installed job."""
    now = now or dt.datetime.now(dt.timezone.utc)
    reasons = []
    if not state.get("configured"):
        reasons.append("dispatcher_not_configured")
    watchdog = state.get("watchdog") or {}
    if not watchdog.get("active") or watchdog.get("schedule") != "*/2 * * * *":
        reasons.append("watchdog_not_configured")

    def recent(value, minutes=3):
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return dt.timedelta(seconds=-30) <= now - parsed <= dt.timedelta(minutes=minutes)
        except (ValueError, TypeError):
            return False

    if not recent(state.get("last_requested_at")):
        reasons.append("dispatcher_missing_runs")
    watchdog_run = state.get("last_watchdog_run") or {}
    if not recent(watchdog_run.get("started_at"), minutes=5):
        reasons.append("watchdog_missing_runs")
    if watchdog_run.get("status") not in {"succeeded", "running"}:
        reasons.append("watchdog_error")
    delivery = state.get("last_delivery") or {}
    if not recent(delivery.get("received_at")):
        reasons.append("dispatcher_response_missing")
    if (delivery.get("http_status") != 200 or delivery.get("timed_out") or delivery.get("transport_error")
            or (delivery.get("result") or {}).get("ok") is not True):
        reasons.append("dispatcher_delivery_error")
    queue = state.get("queue") or {}
    if queue.get("overdue_over_five_minutes"):
        reasons.append("delivery_backlog")
    if queue.get("failed"):
        reasons.append("failed_deliveries")
    if queue.get("stale_claims"):
        reasons.append("stale_delivery_claims")
    return reasons


def scheduler_status(allowed: set[int] | None = None) -> dict:
    try:
        with _connect() as conn:
            state = _snapshot(conn, allowed)
            state["health_reasons"] = health_reasons(state)
            state["healthy"] = not state["health_reasons"]
            return state
    except psycopg.Error as error:
        raise SchedulerError(f"database_scheduler_error:{error.sqlstate or type(error).__name__}") from None


def _dispatch_command(conn) -> str:
    return sql.SQL(
        "update public.scheduled_actions set payload = jsonb_build_object("
        "'request_id', net.http_get(url := {url}, timeout_milliseconds := 55000), "
        "'requested_at', now(), 'previous_request_id', payload->'request_id'), updated_at = now() "
        "where dedupe_key = {checkpoint} and status = 'cancelled'"
    ).format(url=sql.Literal(DISPATCH_URL), checkpoint=sql.Literal(CHECKPOINT)).as_string(conn)


def _watchdog_command(conn, chat_id: int, command: str) -> str:
    return sql.SQL("""
        DO $watchdog$
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtext({job_name}));
          INSERT INTO public.scheduled_actions
            (chat_id, action_type, payload, scheduled_for, status, dedupe_key)
            VALUES ({chat_id}, 'message', '{{}}'::jsonb, now(), 'cancelled', {checkpoint})
            ON CONFLICT (dedupe_key) DO UPDATE SET status = 'cancelled'
              WHERE public.scheduled_actions.status <> 'cancelled';
          IF NOT EXISTS (SELECT 1 FROM cron.job WHERE jobname = {job_name}
              AND active AND schedule = '* * * * *' AND command = {command}) THEN
            PERFORM cron.schedule({job_name}, '* * * * *', {command});
            PERFORM cron.alter_job(jobid, active := true) FROM cron.job WHERE jobname = {job_name};
          END IF;
          IF EXTRACT(MINUTE FROM now()) < 2
              AND has_table_privilege(current_user, 'cron.job_run_details', 'DELETE') THEN
            DELETE FROM cron.job_run_details WHERE jobid IN
              (SELECT jobid FROM cron.job WHERE jobname IN ({job_name}, {watchdog_name}))
              AND end_time < now() - interval '7 days';
          END IF;
        END;
        $watchdog$;
    """).format(job_name=sql.Literal(JOB_NAME), watchdog_name=sql.Literal(WATCHDOG_NAME),
                chat_id=sql.Literal(chat_id), checkpoint=sql.Literal(CHECKPOINT),
                command=sql.Literal(command)).as_string(conn)


def configure_scheduler(allowed: set[int], *, verify_recovery: bool = False) -> dict:
    """Idempotently install a named cron job; its first HTTP call occurs on a scheduled tick."""
    if not allowed:
        raise SchedulerError("no_approved_groups")
    stage = "connect"
    try:
        with _connect() as conn:
            stage = "extensions"
            conn.execute("set local statement_timeout = '20s'")
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (JOB_NAME,))
            conn.execute("create extension if not exists pg_cron with schema pg_catalog")
            conn.execute("create extension if not exists pg_net with schema extensions")
            chat = conn.execute(
                "select chat_id from public.chats where chat_id = any(%s) order by chat_id limit 1", (sorted(allowed),),
            ).fetchone()
            if not chat:
                raise SchedulerError("no_archived_approved_group")
            stage = "checkpoint"
            conn.execute(
                "insert into public.scheduled_actions "
                "(chat_id, action_type, payload, scheduled_for, status, dedupe_key) "
                "values (%s, 'message', '{}'::jsonb, now(), 'cancelled', %s) "
                "on conflict (dedupe_key) do nothing", (chat[0], CHECKPOINT),
            )
            command = _dispatch_command(conn)
            watchdog = _watchdog_command(conn, chat[0], command)
            stage = "schedule_jobs"
            conn.execute("select cron.schedule(%s, %s, %s)", (JOB_NAME, "* * * * *", command))
            conn.execute("select cron.schedule(%s, %s, %s)", (WATCHDOG_NAME, "*/2 * * * *", watchdog))
            conn.execute("select cron.alter_job(jobid, active := true) from cron.job where jobname = any(%s)",
                         ([JOB_NAME, WATCHDOG_NAME],))
            recovery_verified = None
            if verify_recovery:
                # Change only inside this uncommitted savepoint. The test cannot
                # expose a changed job to the scheduler or invoke an HTTP request.
                with conn.transaction():
                    stage = "verify_change"
                    conn.execute("select cron.schedule(%s, %s, %s)", (JOB_NAME, "0 0 * * *", command + " "))
                    conn.execute("select cron.alter_job(jobid, active := false) from cron.job where jobname = %s",
                                 (JOB_NAME,))
                    stage = "verify_watchdog"
                    conn.execute(watchdog)
                    recovery_verified = conn.execute(
                        "select active and schedule = '* * * * *' and command = %s "
                        "from cron.job where jobname = %s", (command, JOB_NAME),
                    ).fetchone()[0]
                    if not recovery_verified:
                        raise SchedulerError("watchdog_recovery_check_failed")
                    raise psycopg.Rollback()
            stage = "snapshot"
            state = _snapshot(conn, allowed)
            state["health_reasons"] = health_reasons(state)
            state["healthy"] = not state["health_reasons"]
            if recovery_verified is not None:
                state["recovery_verified"] = recovery_verified
                state["recovery_test"] = "disabled_job_schedule_and_command_restoration"
            return state
    except psycopg.Error as error:
        raise SchedulerError(f"database_scheduler_error:{error.sqlstate or type(error).__name__}:{stage}") from None


def ensure_scheduler(allowed: set[int]) -> None:
    state = scheduler_status()
    watchdog = state.get("watchdog") or {}
    if (not state.get("configured") or not watchdog.get("active")
            or watchdog.get("schedule") != "*/2 * * * *"
            or "watchdog_error" in state.get("health_reasons", [])):
        configure_scheduler(allowed)

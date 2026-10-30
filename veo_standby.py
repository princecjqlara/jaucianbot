"""Database-only contact bot permissions for VEO's unassigned standby queue.

Never sends Messenger/Telegram messages, resets briefs or changes page settings.
Contact row locks serialize standby permission changes with offer enqueueing.
"""
from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

MAX_CHANGES = 25


def enabled():
    return os.environ.get("VEO_STANDBY_BOT_ENABLED", "").strip().lower() == "true"


def connect():
    url = os.environ.get("CRM_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("VEO standby database is not configured")
    conn = psycopg.connect(url, sslmode="require", connect_timeout=5, prepare_threshold=None, row_factory=dict_row)
    conn.execute("set local lock_timeout = '2s'")
    conn.execute("set local statement_timeout = '8s'")
    return conn


def database_available():
    """Authenticated status diagnostic; read-only and never changes permissions."""
    try:
        with connect() as conn:
            conn.execute("set transaction read only")
            return bool(conn.execute("select to_regclass('public.telegram_client_standby') is not null as ready").fetchone()["ready"])
    except Exception as error:
        print(f"VEO standby database unavailable: {type(error).__name__}")
        return False


def locked_contact(conn, page, contact):
    return conn.execute("""
        select c.id, c.page_id, c.chatbot_enabled, c.pipeline_stage, c.pipeline_stage_source,
               s.automation_epoch, s.status, s.stop_reason, s.pause_kind, s.last_human_activity_at,
               b.enabled as page_enabled, b.trial_mode_enabled, b.trial_contact_id
        from public.contacts c join public.chatbot_contact_states s
          on s.contact_id=c.id and s.page_id=c.page_id
        join public.chatbot_configs b on b.page_id=c.page_id
        where c.id=%s and c.page_id=%s for update of c,s skip locked
    """, (contact, page)).fetchone()


def write_permission(conn, row, value, now):
    """Match veobot permission semantics: invalidate old work, never replay it."""
    conn.execute("update public.contacts set chatbot_enabled=%s,updated_at=%s where id=%s and page_id=%s",
                 (value, now, row["id"], row["page_id"]))
    state = conn.execute("""update public.chatbot_contact_states
        set automation_epoch=coalesce(automation_epoch,0)+1,updated_at=%s
        where contact_id=%s and page_id=%s returning automation_epoch""",
        (now, row["id"], row["page_id"])).fetchone()
    conn.execute("""update public.chatbot_follow_up_jobs set status='cancelled',cancelled_at=%s,
        claimed_at=null,error_message='VEO standby contact permission changed',updated_at=%s
        where contact_id=%s and page_id=%s and status in ('pending','processing','ready_manual','waiting_response')""",
        (now, now, row["id"], row["page_id"]))
    return state["automation_epoch"]


def pause_ours(conn, row, now):
    saved = conn.execute("""select original_enabled,resumed_epoch from public.telegram_client_standby
        where contact_id=%s and page_id=%s and active for update""", (row["id"], row["page_id"])).fetchone()
    if not saved:
        return
    # A later human permission/takeover wins; never undo that newer decision.
    if row["chatbot_enabled"] is True and row["automation_epoch"] == saved["resumed_epoch"]:
        write_permission(conn, row, saved["original_enabled"], now)
    conn.execute("update public.telegram_client_standby set active=false,released_at=%s where contact_id=%s and page_id=%s",
                 (now, row["id"], row["page_id"]))


def enqueue_offer(enqueue, *, now, **action):
    from daily_automation import GROUPS
    if not enabled() or int(action["chat_id"]) not in GROUPS:
        return enqueue(**action)
    payload = action["payload"]
    page, contact = payload.get("new_client_contact_page_id"), payload.get("new_client_contact_id")
    if not page or not contact:
        return False
    with connect() as conn:
        row = locked_contact(conn, page, contact)
        if not row:
            return False
        pause_ours(conn, row, now)
        created = enqueue(**action)
        if not created:
            conn.rollback()
        return created


def prepare_offer(action, now):
    """Reconcile a database commit failure before an existing offer is sent."""
    from daily_automation import GROUPS
    if not enabled() or int(action["chat_id"]) not in GROUPS:
        return True
    p = action["payload"]
    if not p.get("new_client_contact_page_id") or not p.get("new_client_contact_id"):
        return False
    with connect() as conn:
        row = locked_contact(conn, p["new_client_contact_page_id"], p["new_client_contact_id"])
        if not row:
            return False
        pause_ours(conn, row, now)
    return True


def can_resume(row):
    return (row["chatbot_enabled"] is False and row["page_enabled"] is True
        and (not row["trial_mode_enabled"] or row["trial_contact_id"] == row["id"])
        and row["stop_reason"] == "details_collected" and row["status"] == "stopped"
        and row["pause_kind"] not in {"explicit_hold", "human_takeover"}
        and row["last_human_activity_at"] is None
        and row["pipeline_stage_source"] not in {"manual", "messenger"}
        and str(row["pipeline_stage"] or "").lower() not in
            {"paid", "converted", "won", "closed_won", "opted_out", "not_qualified", "order_created"})


def reconcile(page_contacts, allowed, now):
    if not enabled():
        return 0
    import page_access
    import ready_availability as ready
    from automation_controls import new_clients_enabled, enabled_chat_ids
    from cloud_store import new_client_actions
    from daily_automation import GROUPS
    from new_client_automation import _reserved_contact_keys, _candidate_contact_keys, new_client_action_matches_source
    teams = {c for c in enabled_chat_ids(allowed).intersection(GROUPS) if new_clients_enabled(c)}
    if not teams:
        return 0
    # Shared pages respect offers/ownership in every approved VEO team.
    history = [r for r in new_client_actions(allowed.intersection(GROUPS)) if new_client_action_matches_source(r)]
    reserved = _reserved_contact_keys(history)
    wanted = {}
    for chat, pages in page_contacts.items():
        if chat not in teams:
            continue
        for page, contacts in pages:
            for contact in contacts:
                if contact.get("page_id") and contact.get("chatbot_enabled") is False and not _candidate_contact_keys(page, contact).intersection(reserved):
                    wanted[(contact["page_id"], contact["id"])] = (page, contact)
    if not wanted:
        return 0
    # One fresh availability read per team, after offers have consumed their turns.
    ready_pages = set()
    for chat in teams:
        access = page_access.members(chat)
        for person in ready.available(chat, now, history):
            ready_pages.update(access.get(int(person["user_id"]), set()))
    changes = 0
    with connect() as conn:
        for (page_id, contact_id), (page, contact) in sorted(wanted.items()):
            if changes >= MAX_CHANGES:
                break
            if page_id in ready_pages:
                continue
            row = locked_contact(conn, page_id, contact_id)
            if not row or not can_resume(row):
                continue
            epoch = write_permission(conn, row, True, now)
            conn.execute("""insert into public.telegram_client_standby
                (contact_id,page_id,original_enabled,resumed_epoch,active,enabled_at,released_at)
                values (%s,%s,false,%s,true,%s,null) on conflict(contact_id) do update
                set page_id=excluded.page_id,original_enabled=false,resumed_epoch=excluded.resumed_epoch,
                    active=true,enabled_at=excluded.enabled_at,released_at=null""", (contact_id, page_id, epoch, now))
            changes += 1
    return changes

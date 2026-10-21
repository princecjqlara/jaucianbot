"""Daily team client lists, Telegram buttons and historical transfer recovery.

All outbound messages use the existing scheduled outbox. State and audit records
stay on the accepted assignment; they never become independently claimable leads.
New handovers are disabled for all managed teams.
"""
from __future__ import annotations

import datetime as dt
import html
import hashlib
import json
import math
import re
import urllib.parse
import done_deal_tracking
import page_access
import automation_controls
from collections import Counter

from cloud_store import enqueue_scheduled_action, new_client_actions, request
from daily_automation import AVAILABILITY_GROUPS as GROUPS, MANILA, TRABAWHO_CHAT_ID, page_mapping_pending

PAGE_SIZE = 12
COMMAND = re.compile(r"^/(clients|pages|queue|clienthelp|ddtemplate|handover|accept_handover|decline_handover|cancel_handover)(?:@\w+)?(?:\s+(.*))?$", re.I | re.S)
HANDOVER_DISABLED = "Handovers are disabled. The current editor keeps the client."


def handover_enabled(chat_id: int) -> bool:
    return False


def owner_help(chat_id: int) -> str:
    return ("Tap Hand over to transfer, or Client list to view owners."
            if handover_enabled(chat_id) else "Tap Client list to view owners.")


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": "cw:" + data}


def menu() -> dict:
    return {"inline_keyboard": [[button("Ready", "ready"), button("Busy", "busy")],
                                [button("Client list", "list:0"), button("Why no client?", "queue")],
                                [button("How to use", "help")]]}


def offer_buttons(token: str, *, take: bool = False) -> dict:
    return {"inline_keyboard": [[button("Take client" if take else "Accept client", f"{'take' if take else 'accept'}:{token}")],
                                [button("Client list", "list:0")]]}


def owner_buttons(row: dict) -> dict:
    revision = int(row["payload"].get("new_client_handover_revision") or 0)
    keyboard = [[button("Client list", "list:0")]]
    if not handover_enabled(int(row["chat_id"])) and row.get("telegram_message_id"):
        keyboard.insert(0, [{"text": "Client details", "url":
            f"https://t.me/c/{str(abs(int(row['chat_id'])))[3:]}/{row['telegram_message_id']}"}])
    if handover_enabled(int(row["chat_id"])):
        keyboard[0].insert(0, button("Hand over", f"choose:{row['id']}:{revision}:0"))
    if done_deal_tracking.enabled() and GROUPS[int(row["chat_id"])].get("done"):
        keyboard.append([button("Done Deal template", f"dd:{row['id']}")])
    return {"inline_keyboard": keyboard}


def guide(chat_id: int) -> str:
    import ready_availability as ready
    config = GROUPS[chat_id]
    if ready.renewal_required(chat_id):
        setup = "PAGE SETUP PENDING: offers paused.\n" if page_mapping_pending(config) else ""
        ending = ("\nReceipts: post the receipt and written amounts in RECEIPTS.\n"
                  f"Songs: deliver within 24 hours, then post SONG SENT + token in SONGS.\nhttps://t.me/c/{str(abs(chat_id))[3:]}/{config['songs']}"
                  if config.get("songs") else "\nFREEBIE SENT + token — confirm a freebie.")
        sales = ("\nSales: post in DONE DEALS. No client-token code needed." if done_deal_tracking.summary_enabled()
                 else "\nDone Deal tracking is paused.") if not done_deal_tracking.enabled() else ""
        return (f"{config['name'].upper()} — CLIENT GUIDE\n\n" + setup +
                (f"{config['client_page_scope']} members only. Confirm access with /pages.\n" if config.get("client_page_scope") else "") +
                "Active tomorrow? Choose Yes or No.\n"
                "/pages — choose pages you can receive clients from.\n"
                "/ready — available for 60 minutes from your message.\n"
                "/busy — stop new offers.\n"
                "Clients follow round robin. Accept within 15 minutes.\n"
                "Missed offers go to the earliest Ready member: 10 minutes to accept.\n"
                "A missed offer stops Ready. Send /ready to rejoin.\n"
                "After accepting, send /ready to request another client.\n"
                "Accept client: button or WORKING + token. Details stay visible.\n"
                "/clients — client list and owners.\n"
                + ending + ("" if config.get("songs") else sales))
    ending = (f"Complete any missing details, then send the song today and within 24 hours. After sending it to the client, post SONG SENT + the client token in SONGS: https://t.me/c/{str(abs(chat_id))[3:]}/{config['songs']}. The original 24-hour deadline stays the same after handover."
              if config.get("songs") else
              "Paid sale? Tap Done Deal template or send /ddtemplate + the token in NEW CLIENTS. Copy the filled client/page/token, enter Price Deal, then post it yourself in DONE DEALS. FREEBIE tasks still use FREEBIE SENT + token.")
    if not config.get("songs") and not done_deal_tracking.enabled():
        ending = done_deal_tracking.notice()
        if done_deal_tracking.summary_enabled():
            ending += " For a readable sale, use Client: name, Page: your page, Price Deal: amount in DONE DEALS."
        ending += " FREEBIE tasks still use FREEBIE SENT + token."
    setup = ("PAGE SETUP PENDING: client and freebie assignments are paused until the team page is connected. Availability polls and reminders are active.\n\n"
             if page_mapping_pending(config) else "")
    availability = ("Your current-hour selection in today's hourly poll also joins the queue during your selected hours. "
                    "After a missed offer, send /ready or update the current-hour poll selection to resume. "
                    "After /busy, send /ready to rejoin.\n\n" if ready.poll_enabled(chat_id) else "")
    resume = "/ready or a new current-hour poll selection" if ready.poll_enabled(chat_id) else "/ready"
    transfer_guide = ("7. Need to transfer? Tap Hand over on your owner confirmation and choose a teammate. Or reply to their message with /handover TOKEN and a short note. They must accept; you remain responsible until the bot confirms the new owner. They can decline, and you can cancel.\n\n"
                      if handover_enabled(chat_id) else "Accepted clients stay with their confirmed editor.\n\n")
    return (f"{config['name'].upper()} — CLIENT GUIDE\n\n" + setup +
            availability +
            "Use the buttons or commands in NEW CLIENTS:\n"
            "1. Send /ready ONCE to receive clients. WORKING keeps you in the queue for the next client.\n"
            "2. Send /busy IMMEDIATELY when full, on break, or ending your shift. Your accepted clients stay with you.\n"
            f"3. Tap Accept client after reviewing the brief, or send WORKING A1B2C3D4 using the actual token. Accept within {ready.ACCEPTANCE_MINUTES} minutes of delivery. Wait for owner confirmation before contacting the client.\n"
            f"4. Reminder after {ready.ACCEPTANCE_MINUTES - ready.WARNING_BEFORE_MINUTES} minutes. Missed deadline: YOUR TURN ENDED, then PAUSED until {resume}.\n"
            "5. TAKE is STILL AVAILABLE: tap Take client or send TAKE + token during the 10-minute OPEN TAKE window. First eligible claim wins.\n"
            "6. Tap Client list or send /clients: see available clients, reserved offers, and current editors.\n"
            "Waiting without a client? Tap Why no client? or send /queue to see your queue status and what to do next.\n"
            + transfer_guide +
            "Keep answering the hourly availability polls. Unclaimed clients keep rotating. Older offers keep the deadline printed on their message.\n\n"
            + ending + "\n\nButtons not responding? Send the matching text command in your team's NEW CLIENTS topic. Use your personal Telegram account, not anonymous-admin mode.\nConflicting ownership or an existing CRM editor? Ask your team lead before contacting the client.")


def history_for(chat_id: int) -> list[dict]:
    from new_client_automation import new_client_action_matches_source, _enrich_history_contact_identities
    rows = [r for r in new_client_actions({chat_id}) if new_client_action_matches_source(r)]
    _enrich_history_contact_identities(rows)
    return rows


def current_rows(history: list[dict]) -> list[dict]:
    from new_client_automation import _current_assignments
    return list({int(r["id"]): r for r in _current_assignments(history).values()}.values())


def directory(chat_id: int, now: dt.datetime) -> dict:
    from new_client_automation import _page_contacts, _action_contact_keys, _candidate_contact_keys, _reserved_contact_keys, _utc_time
    history = history_for(chat_id)
    reserved = _reserved_contact_keys(history)
    if GROUPS[chat_id].get("client_source") == "suno":
        from daily_automation import SUNO_GROUPS
        from cloud_store import allowed_chat_ids
        others = allowed_chat_ids().intersection(SUNO_GROUPS) - {chat_id}
        if others:
            from new_client_automation import new_client_action_matches_source
            reserved.update(_reserved_contact_keys([r for r in new_client_actions(others) if new_client_action_matches_source(r)]))
    entries = []
    for row in current_rows(history):
        p = row["payload"]
        if not _action_contact_keys(row).intersection(reserved):
            continue
        handover = (p.get("new_client_handover") or {}) if handover_enabled(chat_id) else {}
        accepted = bool(p.get("new_client_acknowledged_at"))
        if chat_id in GROUPS:
            at = _utc_time(p.get("new_client_acknowledged_at"))
            today = now.astimezone(MANILA).date()
            if accepted and (not at or at.astimezone(MANILA).date() != today):
                continue
            if not accepted and p.get("new_client_work_date") != today.isoformat():
                continue
        state = ("Handover pending" if accepted and handover.get("state") == "pending" else
                 "Assigned" if accepted else "Available to TAKE" if p.get("new_client_phase") == "volunteer" else
                 "Awaiting reassignment" if p.get("new_client_phase") in {"expired", "review"} else "Reserved")
        entries.append({"contact_id": p.get("new_client_contact_id"), "client": p.get("new_client_contact_name") or "Client",
                        "page": p.get("new_client_page") or "Page", "status": state,
                        "available": state == "Available to TAKE", "editor_id": p.get("new_client_assignee_id") if state in {"Assigned", "Handover pending", "Reserved"} else None,
                        "editor": p.get("new_client_assignee_name") if state in {"Assigned", "Handover pending", "Reserved"} else None,
                        "editor_confirmed": accepted,
                        "next_editor": handover.get("to_name") if handover.get("state") == "pending" else None,
                        "token": p.get("new_client_token"), "assignment_id": row["id"],
                        "handover_revision": int(p.get("new_client_handover_revision") or 0),
                        "message_id": row.get("telegram_message_id"), "handover_history": p.get("new_client_handover_events") or []})
    # Only eligible CRM/Suno briefs are advertised as available. Historical
    # assignments whose source no longer qualifies keep their confirmed owner.
    seen = set(reserved)
    for page, contacts in _page_contacts(GROUPS[chat_id]):
        for contact in contacts:
            keys = _candidate_contact_keys(page, contact)
            if keys.intersection(seen):
                continue
            seen.update(keys)
            entries.append({"contact_id": contact["id"], "client": contact["name"], "page": page,
                            "status": "Available", "available": True, "editor_id": None, "editor": None,
                            "token": None, "message_id": None})
    entries.sort(key=lambda e: (e["status"], e["page"].casefold(), e["client"].casefold(), str(e["contact_id"])))
    hint = ""
    if GROUPS[chat_id].get("client_source") == "suno" and any(e["available"] for e in entries):
        import ready_availability
        people = ready_availability.available(chat_id, now, history)
        pages = {page_access.page_map(chat_id).get(e["page"]) for e in entries if e["available"]}
        access = page_access.members(chat_id)
        eligible = sum(bool(pages.intersection(access.get(int(u["user_id"]), set()))) for u in people)
        hint = (f"Ready: {eligible}. Offers follow round robin." if eligible else
                "Ready members need access to the available pages. Update Page access." if people else
                "Waiting for Ready members. Choose Page access, then tap Ready (60 minutes).")
    return {"team": GROUPS[chat_id]["name"], "chat_id": chat_id, "as_of_utc": now.isoformat(),
            "daily_only": True, "work_date": now.astimezone(MANILA).date().isoformat(),
            "handovers_enabled": handover_enabled(chat_id),
            "page_mapping_pending": page_mapping_pending(GROUPS[chat_id]),
            "assignment_fingerprint": fingerprint(history),
            "counts": dict(Counter(e["status"] for e in entries)), "clients": entries, "queue_hint": hint,
            "coverage": "Bot-tracked assignments and currently eligible CRM/Suno briefs. External or untracked assignments are not included."}


def render_directory(snapshot: dict, page: int = 0, *, own_view: bool = False) -> tuple[str, dict]:
    if snapshot.get("daily_only"):
        return render_daily_directory(snapshot, page, own_view=own_view)
    entries = snapshot["clients"]
    counts = snapshot["counts"]
    lines = [f"<b>{html.escape(snapshot['team'])} — CLIENT LIST</b>",
             " | ".join(f"{html.escape(state)}: {counts.get(state, 0)}" for state in ("Available", "Available to TAKE", "Reserved", "Assigned", "Handover pending", "Awaiting reassignment")
                        if state != "Handover pending" or handover_enabled(snapshot["chat_id"])),
             f"Updated {html.escape(snapshot['as_of_utc'])} (UTC)", ""]
    if snapshot.get("page_mapping_pending"):
        lines.append("<b>Page setup pending.</b> Client and freebie assignments are paused until your team page is connected.\n")
    internal = str(abs(snapshot["chat_id"]))[3:]
    controls = []
    for entry in entries:
        name = html.escape(directory_label(entry["client"]))
        if entry.get("message_id"):
            name = f'<a href="https://t.me/c/{internal}/{entry["message_id"]}">{name}</a>'
        owner = html.escape(directory_label(entry.get("editor") or str(entry.get("editor_id") or "No editor")))
        if entry.get("next_editor"):
            owner += " → " + html.escape(directory_label(entry["next_editor"])) + " (awaiting acceptance)"
        token = f" · <code>{entry['token']}</code>" if entry.get("token") else ""
        lines.extend([f"<b>{name}</b> · {html.escape(directory_label(entry['page']))}", f"{entry['status']} · {owner}{token}", ""])
        if entry["status"] == "Assigned":
            owner_controls = []
            if handover_enabled(snapshot["chat_id"]):
                owner_controls.append(button("Hand over: " + entry["client"][:35], f"choose:{entry['assignment_id']}:{entry['handover_revision']}:0"))
            if done_deal_tracking.enabled() and GROUPS[snapshot["chat_id"]].get("done"):
                owner_controls.append(button("DD template", f"dd:{entry['assignment_id']}"))
            if owner_controls:
                controls.append(owner_controls)
        elif entry["status"] == "Available to TAKE":
            controls.append([button("Take: " + entry["client"][:35], f"take:{entry['token']}")])
    if not entries:
        lines.append("No tracked or available clients right now.\n")
    lines.append("Available = waiting for assignment. Reserved = only the named editor can accept. TAKE uses the open offer's window.\nTap a client for its brief. /clients shows the latest list.\n" + html.escape(snapshot["coverage"]))
    route = "view" if own_view else "list"
    nav = [button("Refresh", f"{route}:0")]
    return "\n".join(lines), {"inline_keyboard": controls + [nav, [button("Ready", "ready"), button("Busy", "busy"), button("Help", "help")]]}


def render_daily_directory(snapshot: dict, page: int = 0, *, own_view: bool = False) -> tuple[str, dict]:
    entries = sorted(snapshot["clients"], key=lambda e: (
        0 if e["available"] else 2 if e.get("editor_confirmed") else 1,
        (e.get("editor") or "").casefold(), e["client"].casefold(), str(e.get("contact_id"))))
    counts = snapshot["counts"]
    lines = [f"<b>{html.escape(snapshot['team'])} · {snapshot['work_date']}</b>",
             f"Available: {counts.get('Available', 0)} · Pending: {counts.get('Reserved', 0)} · Accepted: {counts.get('Assigned', 0)}",
             ""]
    if snapshot.get("page_mapping_pending"):
        lines.append("Page setup pending. Offers paused.")
    if snapshot.get("queue_hint"):
        lines += [html.escape(snapshot["queue_hint"]), ""]
    previous = None
    totals = Counter(int(e["editor_id"]) for e in entries if e.get("editor_confirmed") and e.get("editor_id"))
    for entry in entries:
        section = "Available clients" if entry["available"] else (
            f"{directory_label(entry.get('editor') or 'Editor')} · {totals.get(int(entry.get('editor_id') or 0), 0)} accepted"
            if entry.get("editor_confirmed") else "Pending acceptance")
        if section != previous:
            lines.append(f"<b>{html.escape(section)}</b>")
            previous = section
        client = html.escape(directory_label(entry["client"]))
        if entry.get("message_id"):
            client = f'<a href="https://t.me/c/{str(abs(snapshot["chat_id"]))[3:]}/{entry["message_id"]}">{client}</a>'
        suffix = (f" → {html.escape(directory_label(entry.get('editor') or 'Editor'))}"
                  if entry["status"] == "Reserved" else "")
        lines.append(f"• {client} · {html.escape(directory_label(entry['page']))}{suffix}")
    if not entries:
        lines.append("No available or accepted clients today.")
    route = "view" if own_view else "list"
    nav = [button("Refresh", f"{route}:0")]
    return "\n".join(lines), {"inline_keyboard": [nav,
        [button("Ready", "ready"), button("Busy", "busy"), button("Page access", "pages")]]}


def directory_label(value: str) -> str:
    """Bound all four labels in a handover row, including emoji UTF-16 units."""
    value = " ".join(str(value).split())
    encoded = value.encode("utf-16-le")
    if len(encoded) <= 90:
        return value
    return encoded[:88].decode("utf-16-le", errors="ignore") + "…"


def directory_parts(text: str) -> list[str]:
    """Telegram-sized messages containing every row, with no paging controls."""
    result, lines = [], []
    for line in text.splitlines():
        candidate = "\n".join([*lines, line])
        visible = html.unescape(re.sub(r"<[^>]*>", "", candidate))
        if lines and len(visible.encode("utf-16-le")) // 2 > 3400:
            result.append("\n".join(lines))
            lines = []
        lines.append(line)
    if lines:
        result.append("\n".join(lines))
    return result or [""]


def sync_directory_continuations(action: dict, snapshot: dict, parts: list[str], markup: dict, now: dt.datetime) -> None:
    """Keep one set of continuation messages per list; refresh them in place."""
    p = action["payload"]
    if len(parts) == 1 and int(p.get("_directory_parts") or 1) == 1:
        return
    prefix = f"client-directory-continuation:{action['id']}:"
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,payload,updated_at,status,telegram_message_id", "chat_id": f"eq.{action['chat_id']}",
        "dedupe_key": f"like.{prefix}*", "limit": 1000,
    })) or []
    existing = {int(row["payload"]["client_directory_part"]): row for row in rows}
    for number in range(1, max(len(parts), int(p.get("_directory_parts") or 1))):
        obsolete = number >= len(parts)
        payload = {"text": "List updated. See the main list." if obsolete else parts[number], "parse_mode": "HTML",
            "disable_notification": True, "message_thread_id": GROUPS[int(action["chat_id"])]["contact_thread"],
            "client_directory_continuation": True, "client_directory_part": number,
            "client_directory_date": snapshot.get("work_date") or now.astimezone(MANILA).date().isoformat(),
            "client_directory_obsolete": obsolete, "_directory_assignment_fingerprint": snapshot["assignment_fingerprint"],
            "reply_markup": markup if number == len(parts) - 1 else {"inline_keyboard": []}}
        row = existing.get(number)
        if row:
            if row["status"] == "processing":
                continue
            old = row["payload"]
            if (row["status"] == "sent" and row.get("telegram_message_id")
                    and old.get("text") == payload["text"] and old.get("reply_markup") == payload["reply_markup"]):
                continue
            if row.get("telegram_message_id"):
                payload["edit_message_id"] = row["telegram_message_id"]
            filters = {"id": f"eq.{row['id']}", "chat_id": f"eq.{action['chat_id']}",
                "status": "in.(pending,sent,failed)", "updated_at": f"eq.{row['updated_at']}"}
            request("scheduled_actions?" + urllib.parse.urlencode(filters), {"payload": payload, "status": "pending",
                "attempts": 0, "scheduled_for": now.isoformat(), "updated_at": now.isoformat()}, method="PATCH")
        elif not obsolete:
            enqueue_scheduled_action(chat_id=int(action["chat_id"]), action_type="message", scheduled_for=now,
                dedupe_key=prefix + str(number), payload=payload)


def fingerprint(history: list[dict]) -> str:
    return hashlib.sha256(json.dumps([(r["id"], r.get("status"), r["payload"].get("new_client_phase"),
        r["payload"].get("new_client_acknowledged_at"), r["payload"].get("new_client_assignee_id"),
        r["payload"].get("new_client_handover")) for r in history], sort_keys=True).encode()).hexdigest()


def queue_board(chat_id: int, now: dt.datetime, *, refresh: bool = False, state: str | None = None) -> None:
    daily = chat_id in GROUPS
    date = now.astimezone(MANILA).date().isoformat()
    key = f"client-directory:v2:{chat_id}:{date}" if daily else f"client-directory:v1:{chat_id}"
    enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now, dedupe_key=key,
        repeat_interval_minutes=5, payload={"text": "Loading client list…", "client_directory": True,
                                          "message_thread_id": GROUPS[chat_id]["contact_thread"], "disable_notification": True,
                                          **({"client_directory_date": date} if daily else {})})
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": f"eq.{key}", "select": "fingerprint:payload->>_directory_assignment_fingerprint", "limit": 1})) or [] if state else []
    if refresh or state and rows and rows[0].get("fingerprint") != state:
        request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": f"eq.{key}", "status": "in.(pending,sent,failed)", "scheduled_for": f"gt.{now.isoformat()}"}),
                {"status": "pending", "scheduled_for": now.isoformat(), "attempts": 0, "updated_at": now.isoformat()}, method="PATCH")


def queue_message(chat_id: int, now: dt.datetime, key: str, text: str, markup: dict | None = None, **extra) -> None:
    enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now, dedupe_key=key,
        payload={"text": text, "parse_mode": "HTML", "message_thread_id": GROUPS[chat_id]["contact_thread"],
                 **({"reply_markup": markup} if markup else {}), **extra})


def full_assignment(chat_id: int, action_id: int) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({"select": "id,chat_id,payload,status,updated_at,telegram_message_id",
        "id": f"eq.{action_id}", "chat_id": f"eq.{chat_id}", "dedupe_key": "like.new-client:*", "limit": 1})) or []
    if not rows:
        return None
    row = rows[0]
    latest = current_rows(history_for(chat_id))
    return row if any(r["id"] == row["id"] for r in latest) and row["payload"].get("new_client_acknowledged_at") else None


def save_assignment(row: dict, payload: dict, now: dt.datetime) -> bool:
    import os
    filters = {"id": f"eq.{row['id']}", "chat_id": f"eq.{row['chat_id']}", "dedupe_key": "like.new-client:*",
               "updated_at": f"eq.{row['updated_at']}", "payload->>new_client_assignee_id": f"eq.{row['payload']['new_client_assignee_id']}"}
    revision = row["payload"].get("new_client_handover_revision")
    filters["payload->>new_client_handover_revision"] = f"eq.{revision}" if revision is not None else "is.null"
    if os.environ.get("ARCHIVE_TRANSPORT", "http").strip() == "postgres":
        filters["new_client_latest_guard"] = "true"
    saved = request("scheduled_actions?" + urllib.parse.urlencode({**filters, "select": "id"}),
                    {"payload": payload, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation")
    if saved:
        row["payload"] = payload
        row["updated_at"] = now.isoformat()
    return bool(saved)


def teammates(chat_id: int, now: dt.datetime) -> dict[int, str]:
    import ready_availability
    from new_client_automation import _active_members
    people = {}
    for row in history_for(chat_id):
        p = row["payload"]
        for prefix in ("new_client_assignee", "new_client_initial_assignee"):
            if p.get(prefix + "_id"):
                people[int(p[prefix + "_id"])] = p.get(prefix + "_name") or str(p[prefix + "_id"])
    for user in _active_members(chat_id, now.astimezone(MANILA).date()):
        people[int(user["user_id"])] = user.get("user_name") or str(user["user_id"])
    for record in ready_availability.records(chat_id):
        p = record["payload"]
        people[int(p["user_id"])] = p.get("user_name") or str(p["user_id"])
    return people


def choose(row: dict, actor: int, revision: int, page: int, now: dt.datetime, event: str) -> str:
    if not handover_enabled(int(row["chat_id"])):
        return HANDOVER_DISABLED
    p = row["payload"]
    if actor != int(p["new_client_assignee_id"]) or revision != int(p.get("new_client_handover_revision") or 0):
        return "Only the current editor can start a handover. Use the latest owner confirmation."
    if (p.get("new_client_handover") or {}).get("state") == "pending":
        return "A handover is already pending. Cancel it before choosing another editor."
    members = sorted(((uid, name) for uid, name in teammates(int(row["chat_id"]), now).items() if uid != actor), key=lambda pair: pair[1].casefold())
    pages = max(1, math.ceil(len(members) / 8))
    page = max(0, min(page, pages - 1))
    keyboard = [[button(name[:55], f"pick:{row['id']}:{revision}:{uid}")] for uid, name in members[page * 8:(page + 1) * 8]]
    nav = []
    if page:
        nav.append(button("Previous", f"choose:{row['id']}:{revision}:{page - 1}"))
    if page + 1 < pages:
        nav.append(button("Next", f"choose:{row['id']}:{revision}:{page + 1}"))
    if nav:
        keyboard.append(nav)
    queue_message(int(row["chat_id"]), now, f"client-picker:{event}",
                  f"<b>Hand over {html.escape(p.get('new_client_contact_name') or 'Client')}</b>\n"
                  f"Choose the receiving editor ({page + 1}/{pages}). Only the current owner can use these buttons.\n"
                  "For a note, reply to your teammate's message with /handover TOKEN followed by the context."
                  + ("\nNo other editors recorded yet. Reply to their message to choose them." if not members else ""), {"inline_keyboard": keyboard})
    return "Choose a teammate in NEW CLIENTS."


def start_handover(row: dict, actor: int, target: int, target_name: str, note: str, now: dt.datetime, event: str, revision: int) -> str:
    if not handover_enabled(int(row["chat_id"])):
        return HANDOVER_DISABLED
    p = row["payload"]
    if actor != int(p["new_client_assignee_id"]):
        return "Only the current editor can hand over this client."
    h = p.get("new_client_handover") or {}
    if h.get("event") == event or any(e.get("event") == event for e in p.get("new_client_handover_events") or []):
        reconcile_row(row, now)
        return "This handover request is already recorded."
    if revision != int(p.get("new_client_handover_revision") or 0) or h.get("state") == "pending":
        return "A handover is pending or this button is old. Use the latest owner confirmation."
    if target == actor:
        return "Choose a different editor."
    if not is_member(int(row["chat_id"]), target):
        return "The receiving editor must be a current member of this team."
    changed = dict(p, new_client_handover_revision=revision + 1, new_client_handover={
        "state": "pending", "revision": revision + 1, "from_id": actor,
        "from_name": p.get("new_client_assignee_name") or str(actor), "to_id": target,
        "to_name": target_name, "note": note[:600], "requested_at": now.isoformat(), "event": event})
    if not save_assignment(row, changed, now):
        raise RuntimeError("Handover changed concurrently; retry the webhook")
    reconcile_row(row, now)
    return "Handover requested. You remain the editor until your teammate accepts."


def finish_handover(row: dict, actor: int, revision: int, decision: str, now: dt.datetime, event: str) -> str:
    if not handover_enabled(int(row["chat_id"])):
        return HANDOVER_DISABLED
    p = row["payload"]
    h = p.get("new_client_handover") or {}
    permitted = h.get("from_id") if decision == "cancel" else h.get("to_id")
    if actor != permitted or h.get("revision") != revision:
        return "This handover button is old or belongs to another editor."
    if h.get("state") != "pending":
        reconcile_row(row, now)
        return "This handover has already been resolved."
    if decision == "yes" and not is_member(int(row["chat_id"]), actor):
        return "Only a current member of this team can accept the handover."
    changed = dict(p, new_client_handover_revision=int(p.get("new_client_handover_revision") or 0) + 1,
                   new_client_handover=dict(h, state={"yes": "accepted", "no": "declined", "cancel": "cancelled"}[decision], resolved_at=now.isoformat(), resolved_event=event))
    changed["new_client_handover_events"] = [*(p.get("new_client_handover_events") or []), changed["new_client_handover"]]
    if decision == "yes":
        changed.setdefault("new_client_initial_assignee_id", p["new_client_assignee_id"])
        changed.setdefault("new_client_initial_assignee_name", p.get("new_client_assignee_name"))
        changed.setdefault("new_client_original_assignee_id", p["new_client_assignee_id"])
        changed.setdefault("new_client_original_assignee_name", p.get("new_client_assignee_name"))
        changed["new_client_assignee_id"] = h["to_id"]
        changed["new_client_assignee_name"] = h["to_name"]
    if not save_assignment(row, changed, now):
        raise RuntimeError("Handover changed concurrently; retry the webhook")
    reconcile_row(row, now)
    return "Handover accepted. You are now the editor." if decision == "yes" else "Handover resolved. The original editor keeps the client."


def is_member(chat_id: int, user_id: int) -> bool:
    from telegram_sender import telegram_call
    member = telegram_call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    return not (member.get("user") or {}).get("is_bot") and (member.get("status") in {"creator", "administrator", "member"}
        or member.get("status") == "restricted" and member.get("is_member") is True)


def reconcile_row(row: dict, now: dt.datetime, *, refresh: bool = True) -> None:
    if not handover_enabled(int(row["chat_id"])):
        return
    from new_client_automation import _mention
    p = row["payload"]
    h = p.get("new_client_handover") or {}
    if not h:
        return
    revision = h["revision"]
    client = html.escape(p.get("new_client_contact_name") or "Client")
    page = html.escape(p.get("new_client_page") or "Page")
    before, after = _mention(h["from_id"], h["from_name"]), _mention(h["to_id"], h["to_name"])
    guard = {"client_transfer_assignment_id": row["id"], "client_transfer_revision": revision, "client_transfer_state": h["state"]}
    if h["state"] == "pending":
        markup = {"inline_keyboard": [[button("Accept handover", f"yes:{row['id']}:{revision}"), button("Decline", f"no:{row['id']}:{revision}")],
                                      [button("Cancel handover", f"cancel:{row['id']}:{revision}"), button("Client list", "list:0")]]}
        text = (f"<b>HANDOVER REQUEST</b>\nClient: {client}\nPage: {page}\n{before} → {after}\n\n"
                f"{after}, review the brief and tap Accept handover when ready. Only you can accept or decline.\n"
                f"{before} remains responsible until acceptance. Only the current editor can cancel.\n"
                f"Note: {html.escape(h['note']) if h['note'] else 'Review the CRM conversation and confirm pending work with the current editor.'}")
        text += (f"\nText alternatives: <code>/accept_handover {p['new_client_token']}</code>, "
                 f"<code>/decline_handover {p['new_client_token']}</code>, or <code>/cancel_handover {p['new_client_token']}</code>.")
    else:
        markup = owner_buttons(row)
        text = (f"<b>HANDOVER {h['state'].upper()}</b>\nClient: {client}\nPage: {page}\n"
                + (f"Current editor: {after}\nPrevious editor: {before}\nPrevious editor: stop working on this client and pass any pending files/context."
                   if h["state"] == "accepted" else f"Current editor: {before}\nThe client stays with this editor.")
                + f"\nClient token: <code>{p['new_client_token']}</code>")
        if int(row["chat_id"]) == TRABAWHO_CHAT_ID and h["state"] == "accepted":
            text += "\nThe original song delivery deadline stays the same. Use the same token for SONG SENT."
    if row.get("telegram_message_id"):
        text += f"\nClient brief: https://t.me/c/{str(abs(int(row['chat_id'])))[3:]}/{row['telegram_message_id']}"
    queue_message(int(row["chat_id"]), now, f"client-transfer:{row['id']}:{revision}:{h['state']}", text, markup, **guard)
    if h["state"] == "accepted":
        import client_handoffs
        client_handoffs.close_offers(row, now, f"Handled by {h['to_name']}. Handover accepted. Unavailable to other editors.")
    if refresh:
        queue_board(int(row["chat_id"]), now, refresh=True)


def maintain(now: dt.datetime, allowed: set[int]) -> None:
    from suno_store import suno_configured
    failures = []
    for chat in sorted(allowed.intersection(GROUPS)):
        if not automation_controls.new_clients_enabled(chat):
            continue
        if GROUPS[chat].get("client_source") == "suno" and (not suno_configured() or not GROUPS[chat].get("active") or not GROUPS[chat].get("songs")):
            continue
        try:
            page_access.queue_poll(chat, now)
            history = history_for(chat)
            queue_board(chat, now, state=fingerprint(history))
            for row in current_rows(history):
                if row["payload"].get("new_client_handover"):
                    reconcile_row(row, now, refresh=False)
        except Exception as error:
            failures.append(type(error).__name__)
    if failures:
        raise RuntimeError("Client workflow maintenance failed: " + ", ".join(failures))


def prepare_delivery(action: dict, now: dt.datetime) -> bool:
    p = action["payload"]
    if p.get("client_directory_continuation"):
        return (p.get("client_directory_date") == now.astimezone(MANILA).date().isoformat()
            and (p.get("client_directory_obsolete") or
                 p.get("_directory_assignment_fingerprint") == fingerprint(history_for(int(action["chat_id"])))))
    if p.get("client_directory") and not p.get("client_directory_once"):
        date = p.get("client_directory_date")
        if date and date != now.astimezone(MANILA).date().isoformat() or (action.get("dedupe_key") or "").startswith("client-directory:v1:"):
            action["repeat_interval_minutes"] = None
            return False
    if not handover_enabled(int(action["chat_id"])) and (
            p.get("client_transfer_assignment_id") or (action.get("dedupe_key") or "").startswith("client-picker:")):
        return False
    key = action.get("dedupe_key") or ""
    if not handover_enabled(int(action["chat_id"])) and key.startswith(("ready-instructions:", "ready-rollout:", "client-help:")):
        p["text"] = html.escape(guide(int(action["chat_id"])))
        p["parse_mode"] = "HTML"
        p["reply_markup"] = menu()
    if not handover_enabled(int(action["chat_id"])):
        markup = p.get("reply_markup") or {}
        if "inline_keyboard" in markup:
            markup["inline_keyboard"] = [[b for b in row if not str(b.get("callback_data", "")).startswith(
                ("cw:choose:", "cw:pick:", "cw:yes:", "cw:no:", "cw:cancel:"))] for row in markup["inline_keyboard"]]
            markup["inline_keyboard"] = [row for row in markup["inline_keyboard"] if row]
    if p.get("client_queue_user_id"):
        p["text"] = queue_status_text(int(action["chat_id"]), int(p["client_queue_user_id"]), now)
        p["parse_mode"] = "HTML"
        p["reply_markup"] = menu()
    if p.get("client_directory"):
        snapshot = directory(int(action["chat_id"]), now)
        full_text, markup = render_directory(snapshot, own_view=bool(p.get("client_directory_once")))
        parts = directory_parts(full_text)
        sync_directory_continuations(action, snapshot, parts, markup, now)
        p["text"] = parts[0]
        p["reply_markup"] = markup if len(parts) == 1 else {"inline_keyboard": []}
        p["_directory_parts"] = max(len(parts), int(p.get("_directory_parts") or 1))
        p["_directory_assignment_fingerprint"] = snapshot["assignment_fingerprint"]
        p["parse_mode"] = "HTML"
        if not p.get("client_directory_once") and action.get("telegram_message_id"):
            p["edit_message_id"] = int(action["telegram_message_id"])
    if p.get("client_transfer_assignment_id"):
        row = full_assignment(int(action["chat_id"]), int(p["client_transfer_assignment_id"]))
        h = (row["payload"].get("new_client_handover") or {}) if row else {}
        return h.get("revision") == p["client_transfer_revision"] and h.get("state") == p["client_transfer_state"]
    if p.get("client_template_assignment_id"):
        if not done_deal_tracking.enabled():
            return False
        row = full_assignment(int(action["chat_id"]), int(p["client_template_assignment_id"]))
        return bool(row and int(row["payload"]["new_client_assignee_id"]) == int(p["client_template_owner_id"]))
    return True


def queue_status_text(chat_id: int, user_id: int, now: dt.datetime) -> str:
    from new_client_automation import new_client_status
    status = new_client_status({chat_id}, now)[0]
    member = next((m for m in status["members"] if int(m["user_id"]) == user_id), None)
    state = member["state"] if member else "not_ready_now"
    reasons = {
        "not_ready_now": "You are not in the Ready queue. Tap Ready below to join.",
        "awaiting_ready_after_missed_offer": "Your previous offer timed out. Tap Ready to rejoin when available.",
        "outside_selected_hours": "Your selected working hours have not started. Update your availability poll.",
        "awaiting_working_reply": "You already have an offer waiting. Open it and tap Accept client before its deadline.",
        "awaiting_delivery": "Your offer is queued for delivery. Please wait for it in NEW CLIENTS.",
        "delivery_failed": "Your offer could not be delivered. The bot will retry or release it; ask your team lead if this continues.",
        "awaiting_volunteer_claim": "An open TAKE offer is waiting. Use the latest Take client button if eligible.",
        "no_available_complete_clients": "You are Ready. No eligible unassigned client is available for this team right now. Stay queued; no need to keep pressing Ready.",
        "ready_for_assignment": "You are Ready and waiting for your turn. Members with fewer clients receive priority.",
        "waiting_for_team_round": "You are queued for the next round. Other eligible members receive their turn first.",
        "waiting_for_page_mapping": "The team's client source is not connected yet. Your team lead needs to check it.",
        "reply_cooldown": "A previous offer timed out. Wait for the retry time shown by the bot or send /ready when available.",
        "source_read_error": "The client source is temporarily unavailable. The bot will retry; your place is kept.",
        "page_access_required": "Choose your accessible pages in /pages before receiving clients.",
    }
    if chat_id in GROUPS:
        reasons.update({
            "not_ready_now": "Send /ready when available. Ready lasts 60 minutes.",
            "no_available_complete_clients": "You are Ready. Waiting for a client. Renew /ready every 60 minutes.",
            "ready_for_assignment": "Ready. Waiting for your turn.",
            "awaiting_working_reply": "Offer waiting. Tap Accept client before its deadline.",
        })
    available = sum(status.get("available_clients", {}).values())
    lines = [f"<b>{html.escape(status['team'])} — YOUR CLIENT QUEUE</b>",
             html.escape(member["name"] if member else str(user_id)), "",
             html.escape(reasons.get(state, "Tap Ready when available, then wait for your next offer."))]
    if status.get("source_available", True):
        lines += [f"Eligible unassigned clients: {available}"]
    else:
        lines += ["Client availability could not be checked yet."]
    if member:
        lines += [f"Offers today: {member['offers_today']} | Accepted today: {member['acknowledged_today']}"]
    lines += ["", "Busy stops new offers. Accepted clients keep their editor.", f"Checked {now.astimezone(MANILA):%b %d, %I:%M %p} Manila time."]
    return "\n".join(lines)


def callback_topic(chat_id: int, message: dict) -> int | None:
    """Resolve missing Telegram topic metadata from a delivered bot message only."""
    if message.get("message_thread_id") is not None:
        return int(message["message_thread_id"])
    message_id = message.get("message_id")
    if not isinstance(message_id, int) or message_id <= 0:
        return None
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "thread:payload->>message_thread_id", "chat_id": f"eq.{chat_id}",
        "telegram_message_id": f"eq.{message_id}", "order": "id.desc", "limit": 1,
    })) or []
    try:
        return int(rows[0]["thread"]) if rows and rows[0].get("thread") else None
    except (TypeError, ValueError):
        return None


def done_deal_template(row: dict, actor: int, now: dt.datetime, event: str, amount: str = "") -> str:
    if not done_deal_tracking.enabled():
        return done_deal_tracking.notice()
    from decimal import Decimal, InvalidOperation
    chat_id = int(row["chat_id"])
    p = row["payload"]
    config = GROUPS[chat_id]
    if not config.get("done") or not config.get("pages"):
        return "This team uses Suno receipts or is waiting for page setup. Done Deal templates are for configured Veo pages."
    if actor != int(p["new_client_assignee_id"]):
        return "Only the current editor can prepare this client's Done Deal template."
    if amount:
        if not re.fullmatch(r"\d{1,9}(?:\.\d{1,2})?", amount):
            return "Enter a positive Price Deal amount, for example /ddtemplate TOKEN 650."
        try:
            if Decimal(amount) <= 0:
                return "Price Deal must be greater than zero."
        except InvalidOperation:
            return "Enter a valid Price Deal amount."
    # Put the draft in NEW CLIENTS. A bot-authored draft must never count as a
    # sale or assign the bot's Telegram ID to an employee's earnings.
    value = amount or "[ENTER AMOUNT]"
    client_name = " ".join((p.get("new_client_contact_name") or "Client").split())
    page_name = " ".join(p["new_client_page"].split())
    lines = [f"Client: {client_name}", f"Page: {page_name}",
             f"Price Deal: {value}", "Paid", f"Client token: {p['new_client_token']}"]
    target = f"https://t.me/c/{str(abs(chat_id))[3:]}/{config['done']}"
    queue_message(chat_id, now, f"client-dd-template:{event}",
        "<b>DONE DEAL TEMPLATE — not recorded yet</b>\n"
        "Use this only after the client has paid. Copy the block, "
        + ("check the amount, " if amount else "replace [ENTER AMOUNT] with Price Deal, ")
        + f'then post it yourself in <a href="{target}">DONE DEALS</a>. Attach payment evidence if your team requires it.\n\n'
        + "<pre>" + html.escape("\n".join(lines)) + "</pre>\n\n"
        "Keep tips, revisions and down payments separate. This draft does not record payment or complete delivery.",
        client_template_assignment_id=row["id"], client_template_owner_id=actor)
    return "Template queued in NEW CLIENTS. Copy it and post it yourself in DONE DEALS after payment."


def handle(update: dict, allowed: set[int], now: dt.datetime) -> str | None:
    """Return a short result, or None for an unrelated update. Never dispatch."""
    import ready_availability
    from new_client_automation import confirm_new_client_reply
    callback = update.get("callback_query") or {}
    message = callback.get("message") or update.get("message") or {}
    user = callback.get("from") or message.get("from") or {}
    chat = (message.get("chat") or {}).get("id")
    if chat not in allowed or chat not in GROUPS or not user.get("id") or user.get("is_bot"):
        return None
    thread = callback_topic(chat, message) if callback else message.get("message_thread_id")
    if thread != GROUPS[chat]["contact_thread"]:
        return None
    if callback and message.get("message_thread_id") is None:
        message = {**message, "message_thread_id": thread}
    if GROUPS[chat].get("client_source") == "suno":
        from suno_store import suno_configured
        if not suno_configured():
            return "The Suno client source is unavailable. Ask your team lead to check the connection." if callback else None
    event = f"{chat}:{update.get('update_id') or callback.get('id') or message.get('message_id')}"
    actor = int(user["id"])
    data = callback.get("data") or ""
    if callback:
        if not data.startswith("cw:") or len(data.encode()) > 64:
            return None
        parts = data[3:].split(":")
        command = parts[0]
    else:
        match = COMMAND.fullmatch((message.get("text") or "").strip())
        if not match:
            return None
        command = {"clients": "list", "clienthelp": "help"}.get(match[1].lower(), match[1].lower())
        parts = [command, match[2] or ""]
    if not automation_controls.new_clients_enabled(chat):
        if not callback:
            enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
                dedupe_key=f"feature-disabled:new-clients:{event}", payload={"text": automation_controls.NEW_CLIENTS_OFF,
                    "message_thread_id": GROUPS[chat]["contact_thread"]})
        return automation_controls.NEW_CLIENTS_OFF
    if not handover_enabled(chat) and command in {"choose", "pick", "yes", "no", "cancel", "handover", "accept_handover", "decline_handover", "cancel_handover"}:
        if not callback:
            queue_message(chat, now, f"client-command-result:{event}", HANDOVER_DISABLED)
        return HANDOVER_DISABLED
    if command in {"ready", "busy"}:
        synthetic = {"update_id": update.get("update_id"), "message": {**message, "from": user,
                    "text": "/" + command, "date": int(now.timestamp())}}
        if not ready_availability.enabled(chat):
            return "Use your team's availability poll; the ready queue is disabled."
        ready_availability.handle(synthetic, allowed, now)
        return "Availability updated."
    if command == "pages" and page_access.enabled(chat):
        page_access.queue_poll(chat, now)
        records = [page_access.poll_record(chat, part) for part in range((len(page_access.page_map(chat)) + 8) // 9)]
        links = [[{"text": "Page access" + (f" {i + 1}" if len(records) > 1 else ""), "url":
            f"https://t.me/c/{str(abs(chat))[3:]}/{record['message_id']}"}] for i, record in enumerate(records) if record]
        text = "Choose your accessible pages in the polls below. Update them when access changes."
        queue_message(chat, now, f"page-access-help:{event}", text if links else "Page-access polls queued.",
                      {"inline_keyboard": links} if links else None)
        return "Page-access polls opened." if links else "Page-access polls queued."
    if command in {"accept", "take"} and callback and len(parts) == 2 and re.fullmatch(r"[A-F0-9]{8}", parts[1]):
        history = history_for(chat)
        matches = [row for row in current_rows(history) if row["payload"].get("new_client_token") == parts[1]]
        if len(matches) != 1:
            return "This offer is old or unavailable. Open the latest offer in NEW CLIENTS."
        row = matches[0]
        if row["payload"].get("new_client_acknowledged_at"):
            if int(row["payload"]["new_client_assignee_id"]) == actor:
                return "Already accepted. This client is yours; check your owner confirmation."
            return "This client is already assigned to another editor."
        synthetic = {"message": {**message, "from": user, "update_id": update.get("update_id"), "date": int(now.timestamp()), "text": f"{'WORKING' if command == 'accept' else 'TAKE'} {parts[1]}"}}
        handled = confirm_new_client_reply(synthetic, allowed, queue_next=False, history=history)
        if handled and row["payload"].get("new_client_acknowledged_at") and int(row["payload"]["new_client_assignee_id"]) == actor:
            if handover_enabled(chat):
                queue_board(chat, now, refresh=True)
            return "Accepted. Client details stay in this message."
        if handled:
            return "The offer changed while you were accepting it. Check the latest owner confirmation."
        return f"Offer expired or reserved for another editor. Check the latest offer; you can also send {'WORKING' if command == 'accept' else 'TAKE'} {parts[1]}."
    if command in {"list", "view"}:
        page = int(parts[1]) if callback and len(parts) == 2 and parts[1].isdigit() else 0
        # Render at scheduled delivery so a queued response cannot show old owners.
        queue_message(chat, now, f"client-directory-query:{event}", "Loading client list…", client_directory=True,
                      client_directory_once=True, client_directory_page=min(page, 100000),
                      **({"edit_message_id": message["message_id"]} if command == "view" and callback else {}))
        return "The latest client list will appear in NEW CLIENTS."
    if command == "queue":
        queue_message(chat, now, f"client-queue-query:{event}", "Checking your queue…", client_queue_user_id=actor)
        return "Your queue status will appear in NEW CLIENTS."
    if command == "help":
        queue_message(chat, now, f"client-help:{event}", html.escape(guide(chat)), menu())
        return "Guide queued in NEW CLIENTS."
    if command in {"dd", "ddtemplate"} and not done_deal_tracking.enabled():
        if not callback:
            queue_message(chat, now, f"client-command-result:{event}", html.escape(done_deal_tracking.notice()))
        return done_deal_tracking.notice()
    if command == "dd" and callback:
        row = full_assignment(chat, int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else None
        return done_deal_template(row, actor, now, event) if row else "Open the latest owner confirmation for this client."
    if command == "ddtemplate" and not callback:
        args = parts[1].strip().split()
        token = args[0].upper() if args else ""
        matches = [row for row in current_rows(history_for(chat)) if row["payload"].get("new_client_token") == token]
        row = full_assignment(chat, int(matches[0]["id"])) if len(matches) == 1 and len(args) in {1, 2} else None
        result = done_deal_template(row, actor, now, event, args[1] if len(args) == 2 else "") if row else "Use /ddtemplate TOKEN or /ddtemplate TOKEN 650 for your accepted client."
        if not result.startswith("Template queued"):
            queue_message(chat, now, f"client-command-result:{event}", html.escape(result))
        return result
    if command in {"choose", "pick", "yes", "no", "cancel"} and callback:
        expected = 4 if command in {"choose", "pick"} else 3
        if len(parts) != expected or not all(part.isdigit() for part in parts[1:]):
            return "Invalid button. Open the latest client confirmation."
        row = full_assignment(chat, int(parts[1]))
        if not row:
            return "Client no longer has this assignment. Open the latest confirmation."
        revision = int(parts[2])
        if command == "choose":
            return choose(row, actor, revision, int(parts[3]), now, event)
        if command == "pick":
            members = teammates(chat, now)
            target = int(parts[3])
            if target not in members:
                return "Choose a teammate recorded in this group."
            return start_handover(row, actor, target, members[target], "", now, event, revision)
        return finish_handover(row, actor, revision, command, now, event)
    if command in {"handover", "accept_handover", "decline_handover", "cancel_handover"} and not callback:
        stamp = dt.datetime.fromtimestamp(message.get("date") or now.timestamp(), dt.timezone.utc)
        args = parts[1].strip().split(maxsplit=1)
        token = args[0].upper() if args else ""
        matches = [r for r in current_rows(history_for(chat)) if r["payload"].get("new_client_token", "").upper() == token]
        row = full_assignment(chat, int(matches[0]["id"])) if len(matches) == 1 else None
        if not row:
            result = "Use the actual token of an accepted client: /handover TOKEN."
        elif command == "handover":
            target = (message.get("reply_to_message") or {}).get("from") or {}
            if not target.get("id") or target.get("is_bot"):
                result = choose(row, actor, int(row["payload"].get("new_client_handover_revision") or 0), 0, now, event)
            else:
                name = " ".join(filter(None, (target.get("first_name"), target.get("last_name")))) or target.get("username") or str(target["id"])
                result = start_handover(row, actor, int(target["id"]), name, args[1] if len(args) > 1 else "", now, event,
                                       int(row["payload"].get("new_client_handover_revision") or 0))
        else:
            h = row["payload"].get("new_client_handover") or {}
            if h.get("requested_at") and stamp < dt.datetime.fromisoformat(h["requested_at"]):
                result = "This reply predates the current handover. Use its latest buttons."
            else:
                result = finish_handover(row, actor, int(h.get("revision") or 0),
                                         {"accept_handover": "yes", "decline_handover": "no", "cancel_handover": "cancel"}[command], now, event)
        queue_message(chat, now, f"client-command-result:{event}", html.escape(result))
        return result
    return "This button is no longer supported. Send /clienthelp."

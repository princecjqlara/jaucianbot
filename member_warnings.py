"""Evidence-based VEO warnings; daily announcements use the scheduled outbox.

No payroll, assignment or membership penalties. No historical warning backfill.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import automation_controls
import member_presence
from cloud_store import request, enqueue_scheduled_action, activity_messages, poll_answers_for_range, new_client_actions
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID, MANILA
from deal_parser import collect_deals

VERSION = 1
PREFIX = "veo-warning-ledger:v1:"
POLICY_KEY = "veo-warning-policy:v1"
MIN_RESPONSE_HOURS = 2
LABELS = {"missed_offer": "Delivered client offer not accepted before its deadline",
    "availability_unanswered": "Tomorrow's availability poll unanswered after a personal reminder",
    "pages_unanswered": "Page-access poll unanswered after a personal reminder",
    "active_no_deals": "Active work date with no recorded CD or DD"}


def enabled():
    return os.environ.get("VEO_WARNINGS_ENABLED", "").strip().lower() == "true"


def zero_mode():
    value = os.environ.get("VEO_WARNINGS_ZERO_OUTPUT", "supplied").strip().lower()
    if value not in {"supplied", "review", "always"}:
        raise ValueError("Invalid VEO zero-output warning mode")
    return value


def stamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(dt.timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def bounds(day):
    start = dt.datetime.combine(day, dt.time.min, MANILA).astimezone(dt.timezone.utc)
    return start, start + dt.timedelta(days=1)


def read_rows(filters):
    rows = []
    while True:
        batch = request("scheduled_actions?" + urllib.parse.urlencode([
            *filters, ("order", "id.asc"), ("offset", len(rows)), ("limit", 1000)])) or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows


def saved(key):
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "dedupe_key": f"eq.{key}", "limit": 1})) or []
    return rows[0]["payload"] if rows else None


def save(key, data, now, chat=DAILY_REPORTS_CHAT_ID):
    rows = request("scheduled_actions?on_conflict=dedupe_key&select=payload", {
        "chat_id": chat, "action_type": "message", "status": "cancelled",
        "dedupe_key": key, "scheduled_for": now.isoformat(), "updated_at": now.isoformat(), "payload": data,
    }, prefer="resolution=ignore-duplicates,return=representation") or []
    result = rows[0]["payload"] if rows else saved(key)
    if result is None:
        raise RuntimeError("Warning snapshot was not saved")
    return result


def observe_ready(update, chat, payload, now):
    if not enabled() or chat not in GROUPS or payload.get("state") != "ready":
        return
    event = update.get("update_id")
    if event is None or int(event) != int(payload.get("update_id", -1)):
        return
    when = stamp(payload.get("ready_at"))
    until = stamp(payload.get("ready_until"))
    if not when or not until or not when <= now < until:
        return
    message = update.get("message") or {}
    save(f"veo-ready-audit:v1:{chat}:{event}", {
        "work_date": str(when.astimezone(MANILA).date()), "user_id": payload["user_id"],
        "name": payload["user_name"], "sent_utc": when.isoformat(), "message_id": message.get("message_id"),
        "source": "live bot Ready update", "event_id": int(event)}, now, chat)


def evidence(chat, message, when, source="live bot"):
    return {"chat_id": chat, "group": GROUPS[chat]["name"], "message_id": message,
        "sent_utc": when.isoformat() if isinstance(when, dt.datetime) else when, "source": source,
        "url": f"https://t.me/c/{str(abs(chat))[3:]}/{message}" if message else None}


def incident(chat, uid, rule, key, detail, proof):
    return {"id": key, "chat_id": chat, "user_id": int(uid), "rule": rule, "detail": detail, "evidence": proof}


def evaluate(day, inputs, history, reminders, audits, contexts, enforced_from, mode="supplied"):
    """Pure rules. Each missed delivered offer counts once; patterns add no points."""
    start, end = bounds(day)
    since = max(start, enforced_from)
    events, checks, names = [], [], {}
    output = Counter()
    uncertain = set()
    for chat, data in inputs.items():
        output.update({int(k): v for k, v in data.get("output", {}).items()})
        uncertain.update(data.get("uncertain", []))
        names.update({(chat, int(k)): v for k, v in data.get("names", {}).items()})
    complete_output = True
    # Only live, delivered, timed offers are obligations. A queue entry is not a delivery.
    offered, accepted, ready_signals = Counter(), Counter(), Counter()
    seen_offers, seen_audits = set(), set()
    for row in history:
        chat, p = int(row["chat_id"]), row["payload"]
        if (chat, row["id"]) in seen_offers:
            continue
        seen_offers.add((chat, row["id"]))
        if chat not in inputs or not p.get("new_client_ready_protocol"):
            continue
        if p.get("new_client_phase") in {"ineligible", "superseded"} or p.get("new_client_cancelled_reason") in {
                "crm_client_already_handled", "assignee_inactive_before_first_delivery", "contact_already_acknowledged"}:
            continue
        delivered = stamp(p.get("_first_delivery_at") or row.get("sent_at"))
        minutes = p.get("new_client_response_minutes")
        if not delivered or not row.get("telegram_message_id") or not isinstance(minutes, (int, float)) or minutes <= 0:
            continue
        deadline = delivered + dt.timedelta(minutes=minutes)
        if delivered < enforced_from or not since <= deadline < end:
            continue
        uid = int(p.get("new_client_original_assignee_id") or p["new_client_assignee_id"])
        names[(chat, uid)] = p.get("new_client_original_assignee_name") or p.get("new_client_assignee_name") or str(uid)
        offered[(chat, uid)] += 1
        acknowledged = stamp(p.get("new_client_acknowledged_at"))
        owner = int(p.get("new_client_initial_assignee_id") or p["new_client_assignee_id"])
        if acknowledged and owner == uid and delivered <= acknowledged < deadline:
            accepted[(chat, uid)] += 1
            continue
        events.append(incident(chat, uid, "missed_offer", f"missed-offer:{chat}:{row['id']}",
            f"No acceptance within {minutes:g} minutes; deadline {deadline:%H:%M} UTC.",
            [evidence(chat, row["telegram_message_id"], delivered)]))
    for row in audits:
        p, chat = row["payload"], int(row["chat_id"])
        key = (chat, p.get("event_id"))
        if key in seen_audits or p.get("event_id") is None:
            continue
        seen_audits.add(key)
        when = stamp(p.get("sent_utc"))
        if chat in inputs and when and since <= when < end:
            ready_signals[(chat, int(p["user_id"]))] += 1
    for row in reminders:
        chat, p = int(row["chat_id"]), row["payload"]
        when = stamp(p.get("_first_delivery_at") or row.get("sent_at"))
        if chat not in inputs or row.get("status") != "sent" or not row.get("telegram_message_id"):
            continue
        if not when or not since <= when < end or end - when < dt.timedelta(hours=MIN_RESPONSE_HOURS):
            continue
        data = inputs[chat]
        if p.get("availability_reminder_poll") and p.get("availability_reminder_date") == str(day + dt.timedelta(days=1)):
            poll = p["availability_reminder_poll"]
            if poll != data.get("next_poll_id"):
                continue
            notified, answered = p.get("availability_reminder_notified_ids", []), data.get("next_answered", [])
            rule, obligation = "availability_unanswered", str(poll)
        elif p.get("page_access_reminder_poll"):
            part = int(p.get("page_access_reminder_part") or 0)
            poll = p["page_access_reminder_poll"]
            if poll != data.get("page_poll_ids", {}).get(part):
                continue
            notified, answered = p.get("page_access_reminder_notified_ids", []), data.get("page_answered", {}).get(part, [])
            rule, obligation = "pages_unanswered", str(poll)
        else:
            continue
        for uid in {int(u) for u in notified} - {int(u) for u in answered}:
            events.append(incident(chat, uid, rule, f"{rule}:{chat}:{day}:{obligation}:{uid}",
                f"No response after a delivered personal reminder with at least {MIN_RESPONSE_HOURS} hours before the daily cutoff.",
                [evidence(chat, row["telegram_message_id"], when)]))
    # Cross-team output prevents false zeros for a person working in multiple teams.
    active = defaultdict(list)
    for chat, data in inputs.items():
        if not data.get("output_complete", True):
            complete_output = False
        for uid in data.get("active", []):
            active[int(uid)].append(chat)
    supplied = {(int(r["chat_id"]), int(r["user_id"])): r for r in contexts
        if r.get("source") == "manager_record" and r.get("actual") is True
        and r.get("work_date") == str(day) and r.get("user_id") and r.get("workload") == "sufficient"}
    if complete_output and start >= enforced_from:
        for uid, chats in active.items():
            if output[uid] or uid in uncertain:
                continue
            covered = sorted(c for c in chats if (c, uid) in supplied)
            chat = covered[0] if covered else min(chats)
            record = supplied.get((chat, uid))
            formal = mode == "always" or mode == "supplied" and record is not None
            detail = "Active for this work date; no readable CD or DD recorded across monitored VEO teams."
            detail += " Supplied work was personally confirmed." if record else " Supplied work is unverified."
            proof = [evidence(chat, record.get("source_message_id"), record.get("recorded_at_utc"), "manager workload record")] if record else []
            item = incident(chat, uid, "active_no_deals", f"active-no-deals:{day}:{uid}", detail, proof)
            (events if formal else checks).append(item)
    unique = {e["id"]: e for e in events}
    patterns = {f"{chat}:{uid}": {"ready_signals": ready_signals[(chat, uid)], "missed_offers": n}
        for (chat, uid), n in offered.items() if n >= 2 and not accepted[(chat, uid)] and ready_signals[(chat, uid)] >= 2}
    return {"events": list(unique.values()), "checks": checks, "patterns": patterns, "names": names}


def load_inputs(day, teams):
    import availability_reminders
    import page_access
    import workload_context
    start, end = bounds(day)
    reminder_filters = [("select", "id,chat_id,status,payload,sent_at,telegram_message_id"),
        ("chat_id", "in.(" + ",".join(map(str, sorted(teams))) + ")"), ("status", "eq.sent"),
        ("sent_at", f"gte.{start.isoformat()}"), ("sent_at", f"lt.{end.isoformat()}")]
    reminders = [row for prefix in ("availability-nonvoter:", "page-access-nonvoter:")
        for row in read_rows([*reminder_filters, ("dedupe_key", f"like.{prefix}*")])]
    audits = read_rows([("select", "chat_id,payload"), ("chat_id", "in.(" + ",".join(map(str, sorted(teams))) + ")"),
        ("dedupe_key", "like.veo-ready-audit:v1:*"), ("payload->>work_date", f"eq.{day}")])
    def load(chat):
        rows = activity_messages(chat, start, end, {GROUPS[chat]["done"], GROUPS[chat]["close"]})
        parsed = collect_deals(rows, GROUPS[chat])
        output, uncertain, names, attendance = Counter(), set(), {}, defaultdict(set)
        for r in rows:
            if r.get("author_id") and r.get("author_name"):
                names[int(r["author_id"])] = r["author_name"]
        for entry in parsed["entries"]:
            uid = entry["row"].get("author_id")
            if uid:
                output[int(uid)] += entry["count"]
        for r in parsed["uncertain_rows"]:
            if r.get("author_id"):
                uncertain.add(int(r["author_id"]))
        for answer in poll_answers_for_range(chat, day, day):
            uid = int(answer["user_id"])
            if isinstance(answer.get("active"), bool):
                attendance[uid].add(answer["active"])
            names[uid] = answer.get("user_name") or str(uid)
        poll = availability_reminders.poll(chat, day + dt.timedelta(days=1))
        from cloud_store import daily_poll_answers
        next_answers = daily_poll_answers(poll["poll_id"]) if poll else []
        page_ids, page_answers = {}, {}
        parts = {int(r["payload"].get("page_access_reminder_part") or 0) for r in reminders
            if int(r["chat_id"]) == chat and r["payload"].get("page_access_reminder_poll")}
        for part in parts:
            current = page_access.poll_record(chat, part)
            if current:
                page_ids[part] = current["page_access_poll_id"]
                page_answers[part] = list(page_access.answered_members(chat, part))
        unattributed = any(not e["row"].get("author_id") and e["count"] > 0 for e in parsed["entries"])
        return chat, {"output": dict(output), "uncertain": list(uncertain), "names": names,
            "output_complete": not unattributed, "active": [u for u, a in attendance.items() if a == {True}],
            "next_poll_id": poll["poll_id"] if poll else None, "next_answered": [int(r["user_id"]) for r in next_answers],
            "page_poll_ids": page_ids, "page_answered": page_answers}
    with ThreadPoolExecutor(max_workers=4) as pool:
        inputs = dict(pool.map(load, sorted(teams)))
    return inputs, new_client_actions(teams), reminders, audits, workload_context.read(teams, day, day)


def totals_before(teams, day):
    rows = read_rows([("select", "chat_id,payload->work_date,payload->member_counts"),
        ("dedupe_key", f"like.{PREFIX}*"), ("status", "eq.cancelled"),
        ("chat_id", "in.(" + ",".join(map(str, sorted(teams))) + ")"), ("payload->>work_date", f"lt.{day}")])
    total, recent = Counter(), Counter()
    for row in rows:
        date = dt.date.fromisoformat(row["work_date"])
        for uid, count in (row.get("member_counts") or {}).items():
            total[(int(row["chat_id"]), int(uid))] += count
            if date >= day - dt.timedelta(days=29):
                recent[(int(row["chat_id"]), int(uid))] += count
    return total, recent


def snapshot(day, teams, now, policy, *, frozen=()):
    inputs, history, reminders, audits, contexts = load_inputs(day, teams)
    result = evaluate(day, inputs, history, reminders, audits, contexts, stamp(policy["enforced_from_utc"]), zero_mode())
    prior, recent = totals_before(teams, day)
    groups, pending, budget = [], [], {"remaining": 32}
    for chat in sorted(teams):
        if chat in frozen:
            continue
        events = [e for e in result["events"] if e["chat_id"] == chat]
        checks = [e for e in result["checks"] if e["chat_id"] == chat]
        candidates = {e["user_id"] for e in events + checks}
        people, coverage = member_presence.filter_current(chat, [{"user_id": u} for u in candidates], now,
            budget=budget, humans_only=True)
        if coverage.get("pending_verification"):
            pending.append(chat)
            continue
        current = {int(p["user_id"]) for p in people}
        events, checks = [e for e in events if e["user_id"] in current], [e for e in checks if e["user_id"] in current]
        members = []
        cached = member_presence.records(chat) if current else {}
        for uid in sorted(current):
            own = [e for e in events if e["user_id"] == uid]
            members.append({"user_id": uid, "name": cached.get(uid, {}).get("user_name") or result["names"].get((chat, uid), str(uid)),
                "warnings_today": len(own), "warnings_30_days": recent[(chat, uid)] + len(own),
                "warnings_total": prior[(chat, uid)] + len(own), "events": own,
                "checks": [e for e in checks if e["user_id"] == uid], "pattern": result["patterns"].get(f"{chat}:{uid}")})
        groups.append({"chat_id": chat, "team": GROUPS[chat]["name"], "members": members, "events": events,
            "checks": checks, "member_counts": {str(u): sum(e["user_id"] == u for e in events) for u in current},
            "warning_count": len(events), "coverage": coverage, "work_date": str(day)})
    return {"ok": True, "read_only": True, "work_date": str(day), "policy": policy, "zero_output_mode": zero_mode(),
        "groups": groups, "pending_teams": pending, "warning_count": sum(g["warning_count"] for g in groups),
        "checked_at_utc": now.isoformat(), "coverage": "Live bot offers and actual personal reminder recipients; missing evidence does not establish a breach."}


def preview(day, teams, now, policy):
    """Use frozen ledgers where available; previewing never writes or publishes."""
    rows = read_rows([("select", "chat_id,payload"), ("dedupe_key", f"like.{PREFIX}{day}:*"),
        ("chat_id", "in.(" + ",".join(map(str, sorted(teams))) + ")"), ("status", "eq.cancelled")])
    existing = {int(r["chat_id"]): r["payload"] for r in rows}
    if teams.issubset(existing):
        data = {"ok": True, "read_only": True, "work_date": str(day), "policy": policy,
            "zero_output_mode": zero_mode(), "groups": [existing[c] for c in sorted(teams)], "saved_ledger": True}
    else:
        data = snapshot(day, teams, now, policy, frozen=existing)
        data["groups"] += [existing[c] for c in sorted(existing)]
    data["warning_count"] = sum(g["warning_count"] for g in data["groups"])
    return data


def member_blocks(group, mentions=True):
    blocks = []
    for person in group["members"]:
        if not person["warnings_today"]:
            continue
        name = html.escape(" ".join(person["name"].split())[:64])
        if mentions:
            name = f'<a href="tg://user?id={person["user_id"]}">{name}</a>'
        lines = [f"{name} — Today: {person['warnings_today']} · Last 30 days: {person['warnings_30_days']} · Recorded total: {person['warnings_total']}"]
        for rule, n in Counter(e["rule"] for e in person["events"]).items():
            label = LABELS[rule]
            lines.append(f"• {html.escape(label)}: {n}")
        if person.get("pattern"):
            p = person["pattern"]
            lines.append(f"• Repeated Ready: {p['ready_signals']} signals; missed all {p['missed_offers']} due offers. Pattern adds no extra warnings.")
        proof = [v for e in person["events"] for v in e["evidence"] if v.get("url")][:3]
        for p in proof:
            lines.append(f'<a href="{p["url"]}">{html.escape(p["group"])} #{p["message_id"]}</a> · {html.escape(p.get("sent_utc") or "time unavailable")} UTC · {html.escape(p["source"])}')
        blocks.append("\n".join(lines))
    return blocks


def chunks(header, blocks, limit=3500):
    parts, text = [], header
    for block in blocks:
        candidate = text + "\n\n" + block
        if len(candidate.encode("utf-16-le")) // 2 > limit and text != header:
            parts.append(text)
            text = header + " (continued)\n\n" + block
        else:
            text = candidate
    parts.append(text)
    return parts


def render(data, chat=None):
    groups = [g for g in data["groups"] if chat is None or g["chat_id"] == chat]
    if chat is not None:
        group = groups[0]
        if not group["warning_count"]:
            return []
        header = f"<b>DAILY MEMBER WARNINGS — {html.escape(group['team'])}</b>\n{data['work_date']} (PHT) · Warnings: {group['warning_count']}"
        return chunks(header, member_blocks(group) + ["Please respond and update the required records. No automatic pay or membership penalties."])
    blocks = []
    for group in groups:
        if group["warning_count"]:
            blocks.append(f"<b>{html.escape(group['team'])} — {group['warning_count']} warnings</b>")
            blocks += member_blocks(group)
        for person in group["members"]:
            if person["checks"]:
                blocks.append(f"Activity check — {html.escape(group['team'])} / {html.escape(person['name'])}: Active with no recorded CD/DD; supplied work unknown or review-only. Not counted as a warning.")
    if not sum(g["warning_count"] for g in groups):
        blocks.insert(0, "No evidence-based warnings for this date.")
    unverified = sum(g.get("coverage", {}).get("unverified", 0) for g in groups)
    if unverified:
        blocks.append(f"Unverified people excluded from assessment: {unverified}.")
    blocks.append("Personal counts are per team since activation. Zero CD/DD checks require a full monitored day; they do not prove low ability. Only live bot evidence is used; exports and unobserved activity are not assessed.")
    return chunks(f"<b>DAILY VEO WARNING SUMMARY</b>\n{data['work_date']} (PHT) · Warnings: {sum(g['warning_count'] for g in groups)}", blocks)


def queue(now, allowed):
    if not enabled() or DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    teams = automation_controls.enabled_chat_ids(allowed).intersection(GROUPS)
    if not teams:
        return 0
    policy = saved(POLICY_KEY) or save(POLICY_KEY, {"version": VERSION, "enforced_from_utc": now.isoformat()}, now)
    local = now.astimezone(MANILA)
    day = local.date() - dt.timedelta(days=1)
    if local.hour == 0 and local.minute < 10 or day < stamp(policy["enforced_from_utc"]).astimezone(MANILA).date():
        return 0
    scope = ",".join(map(str, sorted(teams)))
    key = f"veo-warning-batch:v1:{day}:{scope}"
    if saved(key + ":complete"):
        return 0
    data = saved(key + ":snapshot")
    if data is None:
        data = preview(day, teams, now, policy)
        data["groups"] = [save(f"{PREFIX}{day}:{group['chat_id']}", group, now, group["chat_id"])
                          for group in data["groups"]]
        data["warning_count"] = sum(g["warning_count"] for g in data["groups"])
        if not data.get("pending_teams"):
            data = save(key + ":snapshot", data, now)
    count = 0
    completed_teams = {g["chat_id"] for g in data["groups"]}
    destinations = [*sorted(completed_teams)] + ([] if data.get("pending_teams") else [None])
    for chat in destinations:
        destination = DAILY_REPORTS_CHAT_ID if chat is None else chat
        for number, text in enumerate(render(data, chat), 1):
            count += int(enqueue_scheduled_action(chat_id=destination, action_type="message", scheduled_for=now,
                dedupe_key=f"veo-warning-report:v1:{day}:{destination}:part:{number}", payload={"text": text, "parse_mode": "HTML",
                    "disable_notification": chat is None, "veo_warning_date": str(day), "veo_warning_version": VERSION,
                    "veo_warning_teams": sorted(teams),
                    **({"message_thread_id": GROUPS[chat]["announcements"]} if chat is not None else {})}))
    if not data.get("pending_teams"):
        save(key + ":complete", {"complete": True}, now)
    return count


def delivery_allowed(action, now):
    p = action.get("payload") or {}
    if not p.get("veo_warning_date"):
        return True
    action["repeat_interval_minutes"] = None
    from cloud_store import allowed_chat_ids
    allowed = automation_controls.enabled_chat_ids(allowed_chat_ids())
    destination = int(action["chat_id"])
    teams = set(p.get("veo_warning_teams") or [])
    local = now.astimezone(MANILA)
    return (enabled() and p.get("veo_warning_version") == VERSION
        and not (local.hour == 0 and local.minute < 10)
        and p["veo_warning_date"] == str(local.date() - dt.timedelta(days=1))
        and teams and teams.issubset(allowed.intersection(GROUPS)) and destination in allowed
        and (destination == DAILY_REPORTS_CHAT_ID or destination in teams
            and p.get("message_thread_id") == GROUPS[destination]["announcements"]))

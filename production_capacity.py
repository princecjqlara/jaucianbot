"""VEO capacity estimates from linked CD/DD posts, independent of idle demand.

Post-to-post elapsed time includes nights, client waits and reporting delays.
It is not hands-on production time or a verified maximum.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import defaultdict

from deal_parser import author_identity, client_name, collect_deals, parse_price

POLICY = "veo-project-cycle-v1"
WINDOW_DAYS = 56
MIN_PROJECTS = 3
MANILA = dt.timezone(dt.timedelta(hours=8))


def stamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(dt.timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def normalized_client(value):
    # Only strip a known annotation, never fuzzy-match two client identities.
    return re.sub(r"\s*\(?repeat client\)?\s*$", "", value or "", flags=re.I).strip()


def history(chat, rows, config, names):
    """Keep accepted project events and earliest same-day repost timestamps."""
    parsed = collect_deals(rows, config, historical=True)

    def key(row, kind, page):
        when = stamp(row.get("sent_utc"))
        return (kind, when.astimezone(MANILA).date() if when else None,
                author_identity(row), page, client_name(row.get("text") or ""),
                parse_price(row.get("text") or ""))

    earliest = {}
    for row in parsed["duplicate_rows"]:
        for entry in collect_deals([row], config, historical=True)["entries"]:
            k = key(row, entry["kind"], entry["page"])
            if stamp(row.get("sent_utc")) and (k not in earliest or row["sent_utc"] < earliest[k]["sent_utc"]):
                earliest[k] = row
    events = []
    for entry in parsed["entries"]:
        if entry["summary"] or entry["count"] != 1:
            continue
        row = entry["row"]
        previous = earliest.get(key(row, entry["kind"], entry["page"]))
        if previous and stamp(previous["sent_utc"]) < stamp(row["sent_utc"]):
            row = previous
        client = normalized_client(client_name(row.get("text") or ""))
        when = stamp(row.get("sent_utc"))
        if not client or when is None:
            continue
        uid = row.get("author_id")
        candidates = names.get(" ".join((row.get("author_name") or "").split()).casefold(), set())
        if uid is None and len(candidates) == 1:
            uid = next(iter(candidates))
        # A display name alone does not establish a person's identity.
        if uid is None:
            continue
        price = parse_price(row.get("text") or "")
        events.append({"chat_id": chat, "team": config["name"], "user_id": int(uid),
            "author_name": row.get("author_name") or str(uid),
            "message_id": row["message_id"], "sent_utc": when.isoformat(),
            "source": row.get("source") or "unknown", "kind": entry["kind"],
            "page": entry["page"], "client": client, "price": str(price) if price is not None else None})
    return events


def measure(member, end):
    start = end - dt.timedelta(days=WINDOW_DAYS - 1)
    cutoff = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, MANILA)
    uid = member.get("user_id")
    grouped = defaultdict(list)
    unique = {(e["chat_id"], e["message_id"]): e for e in member.get("project_history", [])}
    for event in unique.values():
        when = stamp(event.get("sent_utc"))
        if event.get("user_id") != uid or when is None or when >= cutoff:
            continue
        grouped[(event["chat_id"], event["page"], event["client"])].append(event)
    pairs, unresolved, unmatched, ambiguous = [], 0, 0, 0
    for events in grouped.values():
        pending = []
        for event in sorted(events, key=lambda e: (stamp(e["sent_utc"]), e["message_id"])):
            if event["kind"] == "cd":
                pending.append(event)
                continue
            if event["kind"] != "dd":
                continue
            day = stamp(event["sent_utc"]).astimezone(MANILA).date()
            recent = start <= day <= end
            if len(pending) == 1:
                opening = pending[0]
                elapsed = (stamp(event["sent_utc"]) - stamp(opening["sent_utc"])).total_seconds()
                if elapsed > 0 and opening["price"] == event["price"] and opening["price"] is not None:
                    if recent:
                        pairs.append({"cd": opening, "dd": event, "elapsed_hours": elapsed / 3600})
                elif recent:
                    ambiguous += 1
            elif recent:
                if pending:
                    ambiguous += 1
                else:
                    unmatched += 1
            pending = []
        # These are unresolved records, not proof that the client is still open.
        unresolved += len(pending)

    # Cross-team copies of the same overlapping client cannot prove two projects.
    contested = set()
    by_client = defaultdict(list)
    for i, pair in enumerate(pairs):
        by_client[(pair["cd"]["page"], pair["cd"]["client"])].append((i, pair))
    for values in by_client.values():
        for offset, (i, a) in enumerate(values):
            for j, b in values[offset + 1:]:
                if a["cd"]["chat_id"] != b["cd"]["chat_id"] and max(stamp(a["cd"]["sent_utc"]), stamp(b["cd"]["sent_utc"])) < min(stamp(a["dd"]["sent_utc"]), stamp(b["dd"]["sent_utc"])):
                    contested.update((i, j))
    ambiguous += len(contested)
    pairs = [p for i, p in enumerate(pairs) if i not in contested]
    events = defaultdict(int)
    for pair in pairs:
        events[stamp(pair["cd"]["sent_utc"])] += 1
        events[stamp(pair["dd"]["sent_utc"])] -= 1
    busy_seconds, project_seconds, peak, count, previous = 0, 0, 0, 0, None
    for when, delta in sorted(events.items()):
        if previous is not None and count:
            seconds = (when - previous).total_seconds()
            busy_seconds += seconds
            project_seconds += count * seconds
            peak = max(peak, count)
        count += delta
        previous = when
    hours = [p["elapsed_hours"] for p in pairs]
    mean = statistics.mean(hours) if hours else None
    completion_dates = {stamp(p["dd"]["sent_utc"]).astimezone(MANILA).date() for p in pairs}
    supported = len(pairs) >= MIN_PROJECTS and len(completion_dates) >= MIN_PROJECTS
    concurrency = project_seconds / busy_seconds if busy_seconds else None
    rate = concurrency * 24 / mean if supported and mean else None
    daily = {d["date"]: d for d in member.get("daily", []) if str(start) <= d["date"] <= str(end)}
    active = [d for d in daily.values() if d.get("attendance") == "Active"]
    output = [d for d in daily.values() if d.get("done_deals", 0) > 0]
    return {"policy": POLICY, "window_start": str(start), "window_end": str(end),
        "matched_projects": len(pairs), "completion_dates": len(completion_dates),
        "latest_completion_date": str(max(completion_dates)) if completion_dates else None,
        "minimum_projects": MIN_PROJECTS, "minimum_completion_dates": MIN_PROJECTS,
        "average_cd_to_dd_hours": mean, "median_cd_to_dd_hours": statistics.median(hours) if hours else None,
        "average_concurrent_projects": concurrency, "peak_concurrent_projects": peak if pairs else None,
        "matched_work_elapsed_hours": busy_seconds / 3600,
        "estimated_capacity_per_elapsed_day": rate,
        "unresolved_cd_records": unresolved, "unmatched_dd_records": unmatched, "ambiguous_records": ambiguous,
        "coverage": "partial" if unresolved or unmatched or ambiguous else "matched records only",
        "average_dd_per_active_day": statistics.mean(d["done_deals"] for d in active) if active else None,
        "active_dates": len(active), "average_dd_per_output_day": statistics.mean(d["done_deals"] for d in output) if output else None,
        "output_dates": len(output), "evidence": pairs, "maximum_capacity": None,
        "basis": "Time-weighted concurrent matched projects / mean CD-to-DD elapsed days. Excludes gaps with no matched work. Requires 3 matched projects on 3 completion dates.",
        "limitations": "Matched completed records only; unresolved/ambiguous work is excluded, so slow unrecorded outcomes can bias the estimate. Calendar elapsed time includes nights, client waits and posting delays; not hands-on hours, verified completion, a maximum or a sales forecast."}


def render(metric):
    rate = metric.get("estimated_capacity_per_elapsed_day")
    n = metric.get("matched_projects", 0)
    lines = [f"Project-based capacity estimate: {rate:.2f} DD/elapsed day with work available."
        if rate is not None else "Project-based capacity: pending 3 matched projects on 3 completion dates."]
    mean = metric.get("average_cd_to_dd_hours")
    if mean is not None:
        lines.append(f"CD → DD elapsed: avg {mean:.2f}h · median {metric['median_cd_to_dd_hours']:.2f}h ({n} matched projects; latest {metric['latest_completion_date']}).")
        lines.append(f"Concurrent matched projects: time-weighted avg {metric['average_concurrent_projects']:.2f} · observed peak {metric['peak_concurrent_projects']}.")
    active = metric.get("average_dd_per_active_day")
    output = metric.get("average_dd_per_output_day")
    if active is not None:
        lines.append(f"Average DD per Active day: {active:.2f} ({metric['active_dates']} dates, including zero DD).")
    if output is not None:
        lines.append(f"Average DD per output day: {output:.2f} ({metric['output_dates']} dates).")
    if metric.get("unresolved_cd_records") or metric.get("unmatched_dd_records") or metric.get("ambiguous_records"):
        lines.append(f"Unlinked records excluded: {metric['unresolved_cd_records']} unresolved CD · {metric['unmatched_dd_records']} unmatched DD · {metric['ambiguous_records']} ambiguous.")
    return lines

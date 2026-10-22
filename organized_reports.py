"""Short daily reports in each team's manager topic, with live staffing boards."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP

import automation_controls
import concern_reports
import done_deal_summary
import member_capacity
import page_access
import report_topics
import suno_capacity
import recent_capacity
import member_presence
import capacity_defaults
import page_capacity
from cloud_store import (allowed_chat_ids, daily_poll_active_users, daily_poll_counts,
    enqueue_scheduled_action, receipt_messages, request, poll_answers_for_range)
from daily_automation import (AVAILABILITY_GROUPS, DAILY_REPORTS_CHAT_ID, GROUPS, MANILA,
    SUNO_GROUPS, messages_for_day, money, split_message, quota_for)
from trabawho_receipts import receipt_report

VERSION = 7


def sources(approved: set[int]) -> set[int]:
    # Reporting an archived team does not enable its assignment bot.
    return approved.intersection(report_topics.TEAM_THREADS).intersection(AVAILABILITY_GROUPS)


def _read(key: str) -> dict | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"eq.{key}", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else None


def _save(key: str, value: dict, now: dt.datetime) -> dict:
    request("scheduled_actions?on_conflict=dedupe_key", {
        "chat_id": DAILY_REPORTS_CHAT_ID, "action_type": "message", "status": "cancelled",
        "scheduled_for": now.isoformat(), "dedupe_key": key, "payload": value,
    }, prefer="resolution=ignore-duplicates,return=minimal")
    saved = _read(key)
    if saved is None:
        raise RuntimeError("Organized report snapshot was not saved")
    return saved


def _scope(teams: set[int]) -> str:
    return hashlib.sha256(",".join(map(str, sorted(teams))).encode()).hexdigest()[:10]


def replacement_request(day: dt.date) -> dict:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"like.organized-replacement:{day}:*", "order": "id.desc", "limit": 1,
    })) or []
    return rows[0]["payload"] if rows else {}


def request_replacement(day: dt.date, now: dt.datetime, approved: set[int], request_id: str) -> dict:
    if day != now.astimezone(MANILA).date() - dt.timedelta(days=1) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("Only yesterday's reports can be replaced")
    if not report_topics.enabled() or DAILY_REPORTS_CHAT_ID not in approved or not automation_controls.chat_enabled(DAILY_REPORTS_CHAT_ID):
        raise ValueError("Organized reports are not enabled")
    saved = _save(f"organized-replacement:{day}:{request_id}",
        {"request_id": request_id, "date": str(day)}, now)
    return {"ok": True, "date": str(day), "request_id": saved["request_id"], "delivery": "scheduled"}


def replacement_deletions_done(key: str, expected: int) -> bool:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "status", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"like.{key}:remove:*", "limit": 1000,
    })) or []
    return len(rows) == expected and all(row["status"] == "sent" for row in rows)


def previous_messages(day: dt.date, teams: set[int]) -> dict[tuple[int, str], list[int]]:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload,telegram_message_id,dedupe_key", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"like.organized-daily:v*:{day}:{_scope(teams)}:*",
        "status": "in.(sent,pending,processing,failed)", "telegram_message_id": "gt.0", "order": "id.asc", "limit": 1000,
    })) or []
    result = defaultdict(dict)
    for row in rows:
        p = row["payload"]
        if p.get("organized_replacement_delete"):
            continue
        match = re.match(r"organized-daily:v(\d+):", row["dedupe_key"])
        if not match or int(match[1]) > VERSION or ":cleanup:" in row["dedupe_key"]:
            continue
        category = p["report_category"]
        number = 1 if category == "summary" else int(row["dedupe_key"].rsplit(":", 1)[-1])
        result[(int(p.get("report_source_chat_id") or 0), category)][number] = int(row["telegram_message_id"])
    return {key: [parts[number] for number in sorted(parts)] for key, parts in result.items()}


def previous_texts(day: dt.date, teams: set[int]) -> dict[int, str]:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload,telegram_message_id", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"like.organized-daily:v*:{day}:{_scope(teams)}:*", "status": "in.(sent,pending,processing,failed)",
        "telegram_message_id": "gt.0", "order": "id.asc", "limit": 1000,
    })) or []
    return {int(r["telegram_message_id"]): r["payload"].get("text", "") for r in rows
        if not r["payload"].get("organized_replacement_delete")}


def previous_staffing_id(today: dt.date, teams: set[int], chat: int) -> int | None:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "telegram_message_id,payload", "chat_id": f"eq.{DAILY_REPORTS_CHAT_ID}",
        "dedupe_key": f"like.organized-staffing:v*:{today}:{_scope(teams)}:{chat}", "order": "id.desc", "limit": 10,
    })) or []
    for row in rows:
        message_id = row.get("telegram_message_id") or row["payload"].get("edit_message_id")
        if message_id:
            return int(message_id)
    return None


def capacity_key(day: dt.date, teams: set[int]) -> str:
    return f"organized-capacity:v{VERSION}:{day}:{_scope(teams)}"


def load_capacity(day: dt.date, now: dt.datetime, approved: set[int]) -> dict:
    teams = sources(approved)
    key = capacity_key(day, teams)
    saved = _read(key)
    if saved is not None:
        return saved
    veo = teams.intersection(GROUPS)
    start = day - dt.timedelta(days=recent_capacity.WINDOW_DAYS - 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(lambda chat: member_capacity.archive_report(chat, start, day), sorted(veo)))
    combined = member_capacity.combined(groups)["members"] if groups else []
    history = member_capacity.backread(veo, now, end=day)["combined"]["members"] if veo else []
    historical = {int(m["user_id"]): m for m in history if m.get("user_id") is not None}
    for member in combined:
        member["planning"] = recent_capacity.planning(member, day)
        old = historical.get(member.get("user_id"))
        if old:
            peak = old["observed_daily_peak"]
            member["planning"]["historical_daily_peak"] = peak
            member["planning"]["historical_peak_date"] = max((d["date"] for d in old["daily"] if peak and d["done_deals"] == peak), default=None)
    members = {}
    cutoff = start.isoformat()
    for chat in sorted(veo):
        members[str(chat)] = [{k: v for k, v in member.items() if k != "daily"}
            for member in combined if GROUPS[chat]["name"] in member.get("teams", [])
            and (member.get("last_activity_date") or "") >= cutoff]
    for chat in sorted(teams.intersection(SUNO_GROUPS)):
        # Receipt dates and declared attendance only; no song-job/delivery requirement.
        report_time = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 10), tzinfo=MANILA)
        members[str(chat)] = suno_capacity.report(report_time, days=recent_capacity.WINDOW_DAYS, chat_id=chat)["members"]
    return _save(key, {"date": day.isoformat(), "members": members}, now)


def current_capacities(capacities: dict, teams: set[int], now: dt.datetime) -> dict:
    result = {**capacities, "members": {}, "membership": {}}
    budget = {"remaining": 32}
    for chat in sorted(teams):
        current, coverage = member_presence.filter_current(chat, capacities["members"].get(str(chat), []), now, budget=budget)
        result["members"][str(chat)] = current
        result["membership"][str(chat)] = coverage
    return capacity_defaults.enrich(result, teams) if result.get("date") else result


def payroll_count(chat: int, day: dt.date) -> dict | None:
    count = daily_poll_counts({chat}, day).get(chat)
    if count is not None:
        return count
    # Older hourly answers can outlive the poll's registration marker.
    # Recover declared voters, never attendance from sales or display names.
    answers = poll_answers_for_range(chat, day, day)
    people = {}
    for row in answers:
        if row.get("user_id") is None or not isinstance(row.get("active"), bool):
            continue
        person = people.setdefault(int(row["user_id"]), {"user_id": int(row["user_id"]),
            "user_name": row.get("user_name"), "answers": set()})
        person["answers"].add(row["active"])
    if not people or any(len(p["answers"]) > 1 for p in people.values()):
        return None
    active = [p for p in people.values() if p["answers"] == {True}]
    active = page_access.scoped_voters(chat, active, day)
    return {"active_workers": len(active), "recovered_from_answers": True}


def finances(chat: int, day: dt.date) -> dict:
    if chat in SUNO_GROUPS:
        begin = dt.datetime.combine(day, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
        result = receipt_report(receipt_messages(chat, begin, begin + dt.timedelta(days=1)), day, day, chat_id=chat)
        total = result["totals"]
        review = [f"Receipt #{item['message_id']}: {item['url']}" for item in result["review"]]
        images = result["coverage"]["attachment_only_posts"]
        unresolved = len(result["unresolved_topic_messages"])
        if images:
            review.append(f"{images} image-only receipt(s) need readable amounts.")
        if unresolved:
            review.append(f"{unresolved} receipt post(s) need topic verification.")
        workers = [{"name": w["name"], "user_id": w["user_id"], "gross": str(w["gross"]),
                    "salary": str(w["salary"]), "count": w["receipt_posts"]} for w in result["workers"]]
        return {"gross": str(total["gross"]), "salary": str(total["salary"]),
            "net": str(total["company_share"]), "count": total["receipt_posts"], "unit": "receipt posts",
            "workers": workers, "review": review, "partial": bool(review),
            "salary_basis": "Earnings written in receipts; not proof of payout."}
    rows, capped = messages_for_day(chat, day)
    result = done_deal_summary.summarize(rows, GROUPS[chat], historical=True, capped=capped)
    from deal_parser import collect_deals, author_identity
    parsed = collect_deals(rows, GROUPS[chat], historical=True)
    workers = {}
    cd_total = sum(e["count"] for e in parsed["entries"] if e["kind"] == "cd")
    for entry in parsed["entries"]:
        if entry["count"] <= 0:
            continue
        row = entry["row"]
        person = workers.setdefault(author_identity(row), {"name": row.get("author_name") or "Unknown member",
            "user_id": row.get("author_id"), "gross": Decimal(0), "count": 0, "close_deals": 0, "salary": None})
        if entry["kind"] == "dd":
            person["gross"] += entry["gross"]
            person["count"] += entry["count"]
        else:
            person["close_deals"] += entry["count"]
    review = []
    for row in parsed["uncertain_rows"]:
        if row.get("thread_id") in {GROUPS[chat]["done"], GROUPS[chat]["close"]}:
            review.append(f"Review sale #{row['message_id']}: {concern_reports.link(chat, row['message_id'])}")
    if capped:
        review.append("Daily archive limit reached; sales totals may be partial.")
    count = payroll_count(chat, day)
    active, quota, tracked = quota_for(count)
    qualified = tracked and active > 0 and cd_total >= quota and result["count"] >= quota
    from deal_parser import DONE_MARKER_RE
    skipped_done = sum(row.get("thread_id") == GROUPS[chat]["done"] or
        row.get("thread_id") is None and bool(DONE_MARKER_RE.search(row.get("text") or ""))
        for row in parsed["uncertain_rows"])
    skipped_close = len(parsed["uncertain_rows"]) - skipped_done
    tier_uncertain = tracked and active > 0 and not qualified and (
        capped or result["count"] + skipped_done >= quota and (cd_total >= quota or skipped_close > 0))
    config = GROUPS[chat]
    mixed_scope = bool(automation_controls.new_clients_enabled(chat) and config.get("client_page_scope") and str(day) >= config.get("page_scope_start", "9999")
        and any(e["page"] != config["client_page_scope"] for e in parsed["entries"] if e["count"] > 0))
    if mixed_scope:
        tier_uncertain = True
        review.append("Payroll quota needs review: sales include pages outside the scoped availability poll.")
    rate = None if not tracked or tier_uncertain else Decimal("0.40") if qualified else Decimal("0.35")
    for worker in workers.values():
        if worker["gross"] == 0:
            worker["salary"] = "0"
        elif rate is not None:
            worker["salary"] = str((worker["gross"] * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    salary = sum((Decimal(w["salary"]) for w in workers.values()), Decimal(0)) if rate is not None else None
    if result["total"] == 0:
        # Either percentage gives zero on accepted zero sales; the tier can stay unknown.
        salary = Decimal(0)
        for worker in workers.values():
            worker["salary"] = "0"
    reason = ("Both CD and DD quotas met." if qualified else "Both quotas were not met.") if rate is not None else (
        "Availability missing or conflicting; quota is unknown." if not tracked else "Quota tier needs data review.")
    return {"gross": str(result["total"]), "salary": str(salary) if salary is not None else None,
        "net": str(result["total"] - salary) if salary is not None else None, "count": result["count"],
        "unit": "Done Deals", "workers": [{**w, "gross": str(w["gross"])} for w in workers.values()],
        "close_deals": cd_total, "active_members": active if tracked else None, "quota": quota if tracked else None,
        "commission_rate": str(rate) if rate is not None else None, "quota_met": qualified if rate is not None else None,
        "availability_basis": "archived vote answers" if count and count.get("recovered_from_answers") else "tracked poll",
        "review": review, "partial": bool(review) or bool(result["review_count"]),
        "salary_basis": f"{int(rate * 100)}% of recorded DD sales. {reason} Earnings, not proof of payout." if rate is not None else reason}


def capacity_text(chat: int, day: dt.date, members: list[dict]) -> str:
    unit = "receipt posts/day" if chat in SUNO_GROUPS else "Done Deals/day"
    start = day - dt.timedelta(days=recent_capacity.WINDOW_DAYS - 1)
    lines = ["CAPACITY UPDATE", AVAILABILITY_GROUPS[chat]["name"], f"{start} to {day} (PHT) · {unit}"]
    for member in sorted(members, key=lambda m: m["name"].casefold()):
        plan = member.get("planning") or {}
        average, peak = plan.get("output_day_average"), plan.get("observed_daily_peak")
        estimate, safe = plan.get("estimated_capacity_per_day"), plan.get("safe_plan_per_day")
        lines += ["", " ".join(member["name"].split()),
            f"Output-day average: {average:.2f} · Recorded peak: {peak}" if average is not None else "Output average / peak: unknown"]
        if estimate is not None:
            best = ", ".join(str(n) for n in plan["top_three_daily_outputs"])
            safe_text = f"{safe}/day" if safe else "under 1/day"
            lines += [f"Best 3 days: {best}", f"Estimated capacity: {estimate:.2f} · Safe target: {safe_text}"]
            if not plan.get("attendance_confirmed_for_top_three"):
                lines.append("Based on output dates; attendance is unverified.")
        elif plan.get("provisional"):
            safe_text = f"{safe:.2f}/day" if safe is not None else "Unknown"
            lines.append(f"Provisional planning: {plan['planning_average_per_day']:.2f}/day · Safe target: {safe_text}")
            lines.append(f"Personal history: {plan.get('personal_output_dates', 0)}/3 output dates; uses the business default.")
        else:
            label = "Unknown" if chat in SUNO_GROUPS else "pending"
            lines.append(f"Capacity / safe target: {label} ({plan.get('eligible_output_days', 0)}/3 output dates)")
        if plan.get("active_day_average") is not None:
            lines.append(f"Active-day average: {plan['active_day_average']:.2f} ({plan['known_active_days']} dates, including zero output)")
        if plan.get("safe_target_met_active_days") is not None:
            lines.append(f"Safe target recorded on {plan['safe_target_met_active_days']}/{plan['safe_target_active_days']} declared Active dates.")
        if plan.get("historical_daily_peak") is not None and (peak is None or plan["historical_daily_peak"] > peak):
            lines.append(f"Older recorded peak: {plan['historical_daily_peak']}/day ({plan['historical_peak_date']}); outside this planning window.")
        if plan.get("inactive_output_conflicts"):
            lines.append(f"Review: {plan['inactive_output_conflicts']} Inactive/output conflict(s) excluded from the plan.")
    if not members:
        lines += ["", "No verified current members with readable recent history."]
    if chat in SUNO_GROUPS:
        lines += ["", "14 completed days. Under 3 output dates: provisional shared Suno default; replaced by personal history at 3 dates.",
            "Default = median of established peers' Active-day receipt averages, including zeros. No eligible peers: Unknown. No assumed default safety buffer.",
            "Personal estimate = best 3 output dates; personal safe = floor(estimate × 80%). Estimates are not guarantees.",
            "Personal history combines Suno teams and non-bot clients' receipts. Repeat payments may count; receipts are not completed songs."]
    else:
        lines += ["", "14 completed days. Personal estimate = best 3 output dates; personal safe = floor(estimate × 80%).",
            "Under 3 output dates: provisional business default, blended with early outputs. Missing attendance stays unknown.",
            "Defaults use peers' Active-day averages, including zeros. Provisional safe = 80%, keeping decimals.",
            "Safe targets are planning estimates, not guaranteed maximums. Under 1/day does not mean unable to work.",
            "Member history combines VEO teams; team figures may overlap."]
    lines += ["Verified current members only. Readable export/live-bot records; missing or unreadable posts are excluded."]
    return "\n".join(lines)


def daily_sections(chat: int, day: dt.date, finance: dict, members: list[dict], concerns: list[dict], tracked: bool) -> dict[str, str]:
    label = f"{AVAILABILITY_GROUPS[chat]['name']} · {day} (PHT)"
    gross = money(Decimal(finance["gross"]))
    sales = ["SALES REPORT", label, "", "TEAM TOTALS"]
    if chat in GROUPS:
        sales += [f"Close Deals (CD): {finance.get('close_deals', 0)}", f"Done Deals (DD): {finance['count']}"]
    else:
        sales += [f"Readable receipt posts: {finance['count']}"]
    sales += [f"Gross sales: {gross}"]
    rate = Decimal(finance["commission_rate"]) if finance.get("commission_rate") is not None else None
    if chat in GROUPS:
        quota = finance.get("quota")
        status = "met" if finance.get("quota_met") else "not met" if finance.get("quota_met") is False else "pending review"
        sales += ["", "DAILY QUOTA", f"Active members: {finance.get('active_members') if finance.get('active_members') is not None else 'unknown'}",
                  f"Target: {quota} CD + {quota} DD" if quota is not None else "Target: pending availability",
                  f"Status: {status}"]
        if finance.get("availability_basis") == "archived vote answers":
            sales += ["Active count recovered from dated availability answers."]
    sales += ["", "MEMBER SALES"]
    salary = ["SALARY REPORT", label, "", finance["salary_basis"], "", "MEMBER EARNINGS"]
    for worker in sorted(finance["workers"], key=lambda w: w["name"].casefold()):
        name = " ".join(worker["name"].split())
        detail = (f"CD {worker.get('close_deals', 0)} · DD {worker['count']}" if chat in GROUPS else f"{worker['count']} receipts")
        sales.append(f"• {name}: {detail} · {money(Decimal(worker['gross']))}")
        amount = money(Decimal(worker["salary"])) if worker["salary"] is not None else "pending"
        calculation = f"{money(Decimal(worker['gross']))} × {int(rate * 100)}% = " if rate is not None else ""
        if chat in GROUPS:
            at35, at40 = salary_scenarios(worker)
            salary += [f"• {name}", f"  If 35%: {money(at35)} · If 40%: {money(at40)}",
                f"  Applied: {calculation}{amount} total for the day" if worker["salary"] is not None else "  Final: pending quota review"]
        else:
            salary.append(f"• {name}: {calculation}{amount} total for the day")
    salary += ["", "TEAM TOTALS", f"Gross sales: {gross}"]
    if chat in GROUPS:
        at35, at40 = commission_totals(finance["workers"])
        salary += [f"If 35%: salary {money(at35)} · Remaining {money(Decimal(finance['gross']) - at35)}",
                   f"If 40%: salary {money(at40)} · Remaining {money(Decimal(finance['gross']) - at40)}"]
    if finance["salary"] is None:
        salary += ["Total salary: pending", "Sales less salary: pending"]
    else:
        salary += [f"Recorded salary: {money(Decimal(finance['salary']))}",
                   f"Sales less salary: {money(Decimal(finance['net']))} (before other expenses)"]
    salary += ["Calculated earnings are not proof of salary payout."]
    if finance["partial"]:
        sales += ["Some records need review; totals cover readable accepted posts."]
        salary += ["Accepted records only; unresolved amounts are excluded."]
    if not finance["count"]:
        sales += ["No readable sales found; this does not confirm zero actual sales."]
    sales += ["Based on recorded payment posts; not unique clients or completed projects."]
    problems = ["PROBLEM REPORT", label, *finance["review"]]
    for post in concerns:
        problems += [f"• {post['author']}: {post['excerpt'] or 'Attachment — review original.'}", post["url"]]
    if not concerns:
        problems.append("No archived concern posts found." if tracked else "No identified concerns topic; issue coverage is incomplete.")
    if concerns:
        problems.append("Includes follow-ups; resolution is unverified.")
    return {"sales": "\n".join(sales), "salary": "\n".join(salary),
        "capacity": capacity_text(chat, day, members), "problems": "\n".join(problems)}


def salary_scenarios(worker: dict) -> tuple[Decimal, Decimal]:
    """Both VEO alternatives, including when the applied rate is already known."""
    gross = Decimal(worker["gross"])
    return tuple((gross * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                 for rate in (Decimal("0.35"), Decimal("0.40")))


def commission_totals(workers: list[dict]) -> tuple[Decimal, Decimal]:
    values = [salary_scenarios(w) for w in workers]
    return tuple(sum((v[i] for v in values), Decimal(0)) for i in (0, 1))


def member_salary_totals(day: dt.date, financials: dict[int, dict]) -> str:
    people = {}
    for chat, finance in financials.items():
        for worker in finance["workers"]:
            uid = worker.get("user_id")
            key = ("id", uid) if uid is not None else ("name", chat, worker["name"].casefold())
            name = worker["name"] if uid is not None else f"{worker['name']} ({AVAILABILITY_GROUPS[chat]['name']})"
            person = people.setdefault(key, {"name": name, "known": Decimal(0), "at35": Decimal(0), "at40": Decimal(0),
                "fixed": Decimal(0), "veo": False, "pending": []})
            if chat in GROUPS:
                at35, at40 = salary_scenarios(worker)
                person["at35"] += at35
                person["at40"] += at40
                person["veo"] = True
            elif worker["salary"] is not None:
                person["fixed"] += Decimal(worker["salary"])
            if worker["salary"] is None:
                person["pending"].append(AVAILABILITY_GROUPS[chat]["name"])
            else:
                person["known"] += Decimal(worker["salary"])
    lines = ["MEMBER SALARY TOTALS", f"{day} (PHT) · combined across teams", ""]
    for person in sorted(people.values(), key=lambda p: p["name"].casefold()):
        name = " ".join(person["name"].split())
        if person["pending"]:
            lines += [f"• {name}: final pending"]
        else:
            lines += [f"• {name}: {money(person['known'])} total for the day"]
        if person["veo"]:
            lines += [f"  If VEO 35%: {money(person['at35'] + person['fixed'])} · If VEO 40%: {money(person['at40'] + person['fixed'])}"]
        if person["pending"]:
            lines += [f"  Known: {money(person['known'])} · Pending tier: {', '.join(person['pending'])}"]
    if not people:
        lines += ["No readable earnings records for this date."]
    lines += ["", "One total per verified Telegram identity. Name-only records stay separate by team.",
        "Scenarios apply 35% or 40% to all VEO earnings; written Suno earnings stay fixed. Applied totals follow each team's quota.",
        "Pending rates are unconfirmed. Readable accepted records only; calculated earnings are not proof of payout."]
    return "\n".join(lines)


def summary_text(day: dt.date, financials: dict[int, dict]) -> str:
    gross = sum(Decimal(f["gross"]) for f in financials.values())
    known = [f for f in financials.values() if f["salary"] is not None]
    salary = sum(Decimal(f["salary"]) for f in known)
    missing = sum(f["salary"] is None for f in financials.values())
    lines = ["DAILY SALES & SALARY SUMMARY", f"{day} (PHT)", "", "ALL TEAMS", f"Recorded gross: {money(gross)}",
        f"Salary: {money(salary)}" + (f" known · {missing} team(s) pending" if missing else ""),
        f"Sales less salary: {money(gross - salary)} (before expenses)" if not missing else "Sales less salary: pending unresolved salary."]
    if any(chat in GROUPS for chat in financials):
        fixed = sum((Decimal(f["salary"]) for chat, f in financials.items() if chat not in GROUPS and f["salary"] is not None), Decimal(0))
        scenarios = [commission_totals(f["workers"]) for chat, f in financials.items() if chat in GROUPS]
        for i, percent in enumerate((35, 40)):
            amount = fixed + sum((s[i] for s in scenarios), Decimal(0))
            lines.append(f"If VEO {percent}% + Suno earnings: salary {money(amount)} · Remaining {money(gross - amount)}")
    for chat, finance in financials.items():
        lines += ["", AVAILABILITY_GROUPS[chat]["name"], f"Gross: {money(Decimal(finance['gross']))}"]
        if chat in GROUPS:
            lines += [f"CD: {finance.get('close_deals', 0)} · DD: {finance['count']}"]
        salary_text = money(Decimal(finance["salary"])) if finance["salary"] is not None else "pending"
        net_text = money(Decimal(finance["net"])) if finance["net"] is not None else "pending"
        lines += [f"Salary: {salary_text} · Remaining: {net_text}"]
        if chat in GROUPS:
            at35, at40 = commission_totals(finance["workers"])
            lines += [f"If 35%: salary {money(at35)} · Remaining {money(Decimal(finance['gross']) - at35)}",
                      f"If 40%: salary {money(at40)} · Remaining {money(Decimal(finance['gross']) - at40)}"]
    lines += ["", "Before ads/other expenses. Readable accepted records only; unresolved amounts may change totals. Not proof of payout."]
    return "\n".join(lines)


class VerifiedRoster(list):
    def __init__(self, users, unverified=0):
        super().__init__(users)
        self.unverified = unverified


def _rosters(teams: set[int], day: dt.date, now: dt.datetime | None = None) -> dict[int, list[dict] | None]:
    counts = daily_poll_counts(teams, day)
    rosters = {chat: page_access.scoped_voters(chat, daily_poll_active_users(counts[chat]["poll_id"]), day)
            if counts.get(chat, {}).get("poll_id") else None for chat in teams}
    now = now or dt.datetime.now(dt.timezone.utc)
    budget = {"remaining": 16}
    result = {}
    for chat, users in rosters.items():
        if users is None:
            result[chat] = None
            continue
        current, coverage = member_presence.filter_current(chat, users, now, budget=budget)
        result[chat] = VerifiedRoster(current, coverage["unverified"])
    return result


def forecast(roster: list[dict] | None, members: list[dict], default: dict | None = None) -> dict:
    if roster is None:
        return {"active": None, "rated": 0, "provisional": 0, "unknown": None,
            "average": None, "total": None, "conservative": None}
    lookup = {int(m["user_id"]): m for m in members if m.get("user_id") is not None}
    unique = {int(user["user_id"]) for user in roster}
    people = [lookup.get(uid) or {"user_id": uid, "planning": {}} for uid in unique]
    if default is not None:
        people = [capacity_defaults.apply(m, default) for m in people]
    plans = [m.get("planning") or {} for m in people]
    rated = [p for p in plans if p.get("planning_average_per_day") is not None]
    has_total = bool(rated) or not unique
    total = sum(p["planning_average_per_day"] for p in rated) if has_total else None
    provisional = sum(bool(p.get("provisional")) for p in rated)
    return {"active": len(unique), "rated": len(rated) - provisional, "provisional": provisional,
        "unknown": len(unique) - len(rated),
        "average": total / len(rated) if rated else None, "total": total,
        "conservative": sum(p["safe_plan_per_day"] for p in rated)
            if has_total and all(p.get("safe_plan_per_day") is not None for p in rated) else None}


def suno_reference_text(reference: dict, unit: str) -> list[str]:
    value = reference.get("capacity_per_day")
    return [f"Suno new-member default: {value:.2f} {unit} ({reference['peer_members']} current peers; provisional)."
        if value is not None else "Suno new-member default: Unknown (no peers with 3 Active and 3 output dates)."]


def staffing_text(chat: int, today: dt.date, now_roster: list[dict] | None, next_roster: list[dict] | None,
                  members: list[dict], default: dict | None = None, *, page_inputs: dict | None = None,
                  page_rosters: dict | None = None) -> str:
    active = str(len({int(u["user_id"]) for u in now_roster})) if now_roster is not None else "untracked poll"
    next_active = str(len({int(u["user_id"]) for u in next_roster})) if next_roster is not None else "untracked poll"
    tomorrow = today + dt.timedelta(days=1)
    lines = ["STAFFING SUMMARY", AVAILABILITY_GROUPS[chat]["name"], f"Active {today}: {active}",
             f"Active tomorrow ({tomorrow}): {next_active}", "", "TOMORROW CAPACITY BY PAGE"]
    business = "veo" if chat in GROUPS else "suno"
    capacities = {"members": {str(chat): members}, "businesses": {business: members},
        "defaults": {business: default} if default is not None else {}}
    inputs = page_inputs if page_inputs is not None else page_capacity.load({chat})
    page_ids = set(inputs.get(chat, {}).get("pages", {}).values())
    rosters = page_rosters if page_rosters is not None else {chat: next_roster}
    page_teams = {source for source in rosters
        if page_ids.intersection(inputs.get(source, {}).get("pages", {}).values())
        and (source in GROUPS) == (chat in GROUPS)}
    page_teams.add(chat)
    scoped_inputs = {source: {**inputs.get(source, {}), "pages": {
        name: pid for name, pid in inputs.get(source, {}).get("pages", {}).items() if pid in page_ids}}
        for source in page_teams}
    data = page_capacity.build(today, page_teams, rosters, capacities, scoped_inputs)
    lines += page_capacity.render(data, detailed=True)
    if not automation_controls.chat_enabled(chat):
        lines.append("Assignment bot paused; availability reflects recorded votes only.")
    return "\n".join(lines)


def tomorrow_summary(today: dt.date, teams: set[int], rosters: dict, capacities: dict) -> str:
    lines = ["TOMORROW CAPACITY BY PAGE", f"{today + dt.timedelta(days=1)} (PHT)"]
    inputs = capacities.get("page_inputs")
    if inputs is None:
        inputs = page_capacity.load(teams)
    lines += page_capacity.render(page_capacity.build(today, teams, rosters, capacities, inputs))
    return "\n".join(lines)


def queue_automation(now: dt.datetime, approved: set[int]) -> int:
    if not report_topics.enabled() or DAILY_REPORTS_CHAT_ID not in approved or not automation_controls.chat_enabled(DAILY_REPORTS_CHAT_ID):
        return 0
    local = now.astimezone(MANILA)
    if local.hour == 0 and local.minute < 10:
        return 0
    teams = sources(approved)
    if not teams:
        return 0
    day = local.date() - dt.timedelta(days=1)
    capacities = current_capacities(load_capacity(day, now, approved), teams, now)
    # Finish the bounded membership sweep before publishing a partial roster.
    # Fresh API failures remain unknown and are omitted rather than guessed.
    if any(c.get("pending_verification") for c in capacities.get("membership", {}).values()):
        return 0
    replacement = replacement_request(day)
    revision = str(replacement.get("request_id") or "")
    membership_scope = hashlib.sha256(str((capacity_defaults.POLICY,
        sorted((chat, sorted(m["user_id"] for m in capacities["members"].get(str(chat), [])))
        for chat in teams))).encode()).hexdigest()[:10]
    suffix = f":refresh:{revision}" if revision else ""
    key = f"organized-daily:v{VERSION}:{day}:{_scope(teams)}:{membership_scope}{suffix}"
    financial_key = f"organized-daily:v{VERSION}:{day}:{_scope(teams)}:financial{suffix}"
    if _read(key + ":complete") is not None:
        return 0
    saved = _read(key + ":snapshot")
    if saved is None:
        concern_data = concern_reports.snapshot(day, approved, now)
        concerns, tracked = defaultdict(list), set()
        for group in concern_data["groups"]:
            if group.get("verified", True):
                tracked.add(int(group["chat_id"]))
            concerns[int(group["chat_id"])].extend(group["posts"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            financials = dict(zip(sorted(teams), pool.map(lambda chat: finances(chat, day), sorted(teams))))
        sections = {str(chat): daily_sections(chat, day, financials[chat], capacities["members"].get(str(chat), []),
                    concerns[chat], chat in tracked) for chat in sorted(teams)}
        saved = _save(key + ":snapshot", {"sections": sections, "summary": summary_text(day, financials),
            "salary_totals": member_salary_totals(day, financials)}, now)
    queued = 0
    previous = previous_messages(day, teams)
    old_text = previous_texts(day, teams)
    first_publication = revision and _read(financial_key + ":published") is None
    if first_publication:
        # Prepare the full snapshot before removing any bot report. Wait for the
        # scheduler to confirm every deletion before queueing replacement posts.
        removals = _save(financial_key + ":removals", {"message_ids": sorted(old_text)}, now)["message_ids"]
        for message_id in removals:
            queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=now,
                dedupe_key=f"{financial_key}:remove:{message_id}", payload={"delete_message_id": message_id,
                    "organized_replacement_delete": True, "organized_refresh_id": revision,
                    "organized_report_version": VERSION}))
        if removals and not replacement_deletions_done(financial_key, len(removals)):
            return queued
        previous, old_text = {}, {}
    delivery_time = now + dt.timedelta(seconds=1) if revision else now
    for chat in sorted(teams):
        for category, text in saved["sections"][str(chat)].items():
            parts = split_message(text, limit=1700)
            edits = previous.get((chat, category), [])
            for number, part in enumerate(parts, 1):
                if number > 1:
                    part = f"{category.upper()} — {AVAILABILITY_GROUPS[chat]['name']} (continued)\n\n{part}"
                if number <= len(edits) and old_text.get(edits[number - 1]) == part:
                    continue
                queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=delivery_time,
                    dedupe_key=f"{key if category == 'capacity' else financial_key}:{chat}:{category}:{number}", payload={"text": part,
                        "message_thread_id": report_topics.TEAM_THREADS[chat], "disable_notification": True,
                        "organized_report_date": day.isoformat(), "organized_report_version": VERSION,
                        "organized_refresh_id": revision,
                        "report_source_chat_id": chat, "report_category": category,
                        **({"capacity_member_ids": [m["user_id"] for m in capacities["members"].get(str(chat), [])]} if category == "capacity" else {}),
                        **({"edit_message_id": edits[number - 1]} if number <= len(edits) else {})}))
            for old_id in edits[len(parts):]:
                link = concern_reports.link(DAILY_REPORTS_CHAT_ID, edits[0])
                queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=now,
                    dedupe_key=f"{key if category == 'capacity' else financial_key}:{chat}:{category}:cleanup:{old_id}", payload={
                        "text": f"Updated {category} report: {link}", "edit_message_id": old_id, "disable_notification": True,
                        "message_thread_id": report_topics.TEAM_THREADS[chat], "organized_report_date": day.isoformat(),
                        "organized_report_version": VERSION, "report_source_chat_id": chat, "report_category": category}))
    summary_ids = previous.get((0, "summary"), [])
    queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=delivery_time,
            repeat_interval_minutes=5,
            dedupe_key=f"{financial_key}:summary", payload={"text": saved["summary"], "disable_notification": True,
                "financial_summary_text": saved["summary"], "organized_summary_date": day.isoformat(),
                "organized_report_date": day.isoformat(), "organized_report_version": VERSION,
                "organized_refresh_id": revision,
                "report_category": "summary", **({"edit_message_id": summary_ids[0]} if summary_ids else {})}))
    edits = previous.get((0, "salary_totals"), [])
    for number, part in enumerate(split_message(saved["salary_totals"], limit=3500), 1):
        if number > 1:
            part = f"MEMBER SALARY TOTALS (continued)\n{day} (PHT)\n\n{part}"
        if number <= len(edits) and old_text.get(edits[number - 1]) == part:
            continue
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=delivery_time,
            dedupe_key=f"{financial_key}:0:salary_totals:{number}", payload={"text": part, "disable_notification": True,
                "organized_report_date": day.isoformat(), "organized_report_version": VERSION,
                "organized_refresh_id": revision,
                "report_category": "salary_totals", **({"edit_message_id": edits[number - 1]} if number <= len(edits) else {})}))
    for chat in [*sorted(teams), 0]:
        prior_id = previous_staffing_id(local.date(), teams, chat)
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message", scheduled_for=now,
            dedupe_key=f"organized-staffing:v{VERSION}:{local.date()}:{_scope(teams)}:{chat}", repeat_interval_minutes=5,
            payload={"text": "Updating staffing summary…", "disable_notification": True,
                "organized_staffing_date": local.date().isoformat(), "organized_staffing_version": VERSION,
                "report_source_chat_id": chat,
                "report_source_ids": sorted(teams),
                **({"edit_message_id": prior_id} if prior_id else {}),
                **({"message_thread_id": report_topics.TEAM_THREADS[chat]} if chat else {})}))
    _save(key + ":complete", {"queued": True}, now)
    if first_publication:
        _save(financial_key + ":published", {"queued": True}, now)
    return queued


def staffing_context(now: dt.datetime, teams: set[int], cache: dict) -> tuple:
    today = now.astimezone(MANILA).date()
    key = (today, tuple(sorted(teams)))
    if key not in cache:
        raw = _read(capacity_key(today - dt.timedelta(days=1), teams))
        if raw is None:
            raise RuntimeError("Staffing capacity snapshot is not available yet")
        capacities = current_capacities(raw, teams, now)
        capacities["replacement_id"] = replacement_request(today - dt.timedelta(days=1)).get("request_id")
        capacities["page_inputs"] = page_capacity.load(teams)
        cache[key] = capacities, _rosters(teams, today, now), _rosters(teams, today + dt.timedelta(days=1), now)
    return cache[key]


def prepare_delivery(action: dict, now: dt.datetime, cache: dict | None = None) -> bool:
    payload = action["payload"]
    summary = bool(payload.get("organized_summary_date"))
    if not payload.get("organized_staffing_date") and not summary:
        return True
    approved = allowed_chat_ids()
    today = now.astimezone(MANILA).date()
    teams = sources(approved)
    valid = (payload.get("organized_report_version") == VERSION and
             payload["organized_summary_date"] == str(today - dt.timedelta(days=1))) if summary else (
             payload.get("organized_staffing_version", 1) == VERSION and
             payload["organized_staffing_date"] == today.isoformat() and set(payload["report_source_ids"]) == teams)
    if not report_topics.enabled() or int(action["chat_id"]) != DAILY_REPORTS_CHAT_ID or not valid:
        action["repeat_interval_minutes"] = None
        return False
    cache = cache if cache is not None else {}
    capacities, current, upcoming = staffing_context(now, teams, cache)
    if summary and (payload.get("organized_refresh_id") or None) != capacities.get("replacement_id"):
        action["repeat_interval_minutes"] = None
        return False
    chat = int(payload.get("report_source_chat_id") or 0)
    if summary:
        text = payload["financial_summary_text"] + "\n\n" + tomorrow_summary(today, teams, upcoming, capacities)
    elif chat:
        business = "veo" if chat in GROUPS else "suno"
        default = capacities.get("defaults", {}).get(business) or capacity_defaults.baseline(business, [])
        people = capacities.get("businesses", {}).get(business, capacities["members"].get(str(chat), []))
        text = staffing_text(chat, today, current[chat], upcoming[chat], people, default,
            page_inputs=capacities.get("page_inputs"), page_rosters=upcoming)
    else:
        def unique_count(rosters):
            users = {int(u["user_id"]) for roster in rosters.values() for u in roster or []}
            missing = sum(roster is None for roster in rosters.values())
            return f"{len(users)} recorded" + (f" · {missing} untracked poll(s)" if missing else "")
        lines = ["STAFFING SUMMARY", f"{today} (PHT)", f"Active today: {unique_count(current)}",
                 f"Active tomorrow ({today + dt.timedelta(days=1)}): {unique_count(upcoming)}", "",
                 tomorrow_summary(today, teams, upcoming, capacities)]
        text = "\n".join(lines)
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    if action.get("telegram_message_id") and payload.get("_organized_fingerprint") == fingerprint:
        return False
    payload.update(text=text, _organized_fingerprint=fingerprint)
    if action.get("telegram_message_id"):
        payload["edit_message_id"] = int(action["telegram_message_id"])
    return True

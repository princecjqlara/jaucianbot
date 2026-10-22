"""Tomorrow's verified availability joined to explicit page-access answers."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import capacity_defaults
import page_access
from daily_automation import GROUPS, SUNO_GROUPS


def load(teams: set[int]) -> dict[int, dict]:
    def read(chat):
        pages = page_access.page_map(chat)
        ids = list(pages.values())
        valid = set(ids)
        access, answered = defaultdict(set), defaultdict(set)
        for answer in page_access.answer_records(chat):
            if answer.get("user_id") is None or answer.get("answered", True) is False:
                continue
            uid = int(answer["user_id"])
            part = int(answer.get("page_access_part") or 0)
            answered[uid].update(ids[part * 9:(part + 1) * 9])
            access[uid].update(set(answer.get("page_ids") or []).intersection(valid))
        return chat, {"pages": pages, "access": dict(access), "answered": dict(answered)}

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(teams)))) as pool:
        return dict(pool.map(read, sorted(teams)))


def build(today: dt.date, teams: set[int], rosters: dict, capacities: dict, inputs: dict) -> dict:
    from organized_reports import forecast

    businesses = []
    for business, family in (("veo", set(GROUPS)), ("suno", set(SUNO_GROUPS))):
        chats = teams.intersection(family)
        if not chats:
            continue
        members = capacities.get("businesses", {}).get(business)
        if members is None:
            members = capacity_defaults.unique_members(
                [m for chat in sorted(chats) for m in capacities["members"].get(str(chat), [])],
                today - dt.timedelta(days=1), combine_days=business == "suno")
        default = capacities.get("defaults", {}).get(business) or capacity_defaults.baseline(business, members)
        pages, missing_access = {}, set()
        for chat in sorted(chats):
            source = inputs.get(chat, {})
            roster = rosters.get(chat)
            for name, pid in source.get("pages", {}).items():
                page = pages.setdefault(pid, {"page_id": pid, "name": name, "users": {},
                    "unconfirmed": set(), "tracked_polls": 0, "missing_polls": 0})
                if roster is None:
                    page["missing_polls"] += 1
                    continue
                page["tracked_polls"] += 1
                for user in roster:
                    uid = int(user["user_id"])
                    if pid in source.get("access", {}).get(uid, set()):
                        page["users"][uid] = user
                    elif pid not in source.get("answered", {}).get(uid, set()):
                        page["unconfirmed"].add(uid)
        results = []
        for page in sorted(pages.values(), key=lambda p: (p["name"].casefold(), p["page_id"])):
            users = list(page["users"].values()) if page["tracked_polls"] else None
            data = forecast(users, members, default)
            # An average for all Active page members cannot omit unknown rates.
            if data["unknown"]:
                data.update(average=None, total=None, conservative=None)
            unconfirmed = page["unconfirmed"].difference(page["users"])
            missing_access.update(unconfirmed)
            results.append({"page_id": page["page_id"], "name": page["name"], **data,
                "missing_access": len(unconfirmed), "missing_polls": page["missing_polls"]})
        businesses.append({"business": business, "unit": "DD/day" if business == "veo" else "receipts/day",
            "pages": results, "default": default, "missing_access": len(missing_access),
            "missing_polls": sum(rosters.get(chat) is None for chat in chats),
            "unverified_members": sum(getattr(rosters.get(chat), "unverified", 0) for chat in chats)})
    return {"businesses": businesses}


def render(data: dict, *, detailed: bool = False) -> list[str]:
    lines = []
    for business in data["businesses"]:
        lines += ["", f"{business['business'].upper()} · {business['unit']}"]
        if not business["pages"]:
            lines.append("Page list unavailable.")
        for page in business["pages"]:
            if page["active"] is None:
                lines.append(f"{page['name']}: availability pending.")
                continue
            if page["average"] is not None:
                average = f"{page['average']:.2f}"
            elif page["unknown"]:
                average = "Unknown"
            elif page["missing_access"]:
                average = "pending access"
            else:
                average = "n/a"
            suffix = f" · {page['provisional']} provisional" if page["provisional"] else ""
            partial = " (partial)" if page["missing_polls"] or page["missing_access"] else ""
            lines.append(f"{page['name']}: {page['active']} active · Avg {average}{suffix}{partial}")
            if detailed:
                total = f"{page['total']:.2f}" if page["total"] is not None else "Unknown"
                safe = f"{page['conservative']:.2f}" if page["conservative"] is not None else "Unknown"
                lines += [f"Estimated total: {total} · Safe target: {safe}",
                    f"Personal history: {page['rated']} · Provisional: {page['provisional']} · Unknown: {page['unknown']}"]
        if business["missing_access"]:
            lines.append(f"Page-access votes pending: {business['missing_access']} active member(s).")
        if business["missing_polls"]:
            lines.append(f"Availability polls untracked: {business['missing_polls']}; page figures are partial.")
        if business["unverified_members"]:
            lines.append(f"Membership pending: {business['unverified_members']} vote(s); excluded.")
        if detailed:
            default = business["default"]
            value = default.get("capacity_per_day")
            rate = f"{value:.2f}" if value is not None else "Unknown"
            label = "Suno new-member default" if business["business"] == "suno" else "Starting default"
            lines.append(f"{label}: {rate} {business['unit']} (provisional).")
    lines += ["", "Avg = estimated output per active member per day, with confirmed page access.",
        "Shared members count once per page; page totals overlap. Planning estimates, not guarantees."]
    return lines

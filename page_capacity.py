"""Tomorrow's verified availability joined to explicit page-access answers."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import capacity_defaults
import page_access
import veo_forecast
import staff_capacity
from daily_automation import GROUPS, SUNO_GROUPS


def report_page_visible(name: str) -> bool:
    """Manager-retired display page; retain source records, access votes and sales."""
    normalized = "".join(c for c in str(name).casefold() if c.isalnum())
    return normalized not in {"pepfactory", "pepefactory"}


def load(teams: set[int]) -> dict[int, dict]:
    def read(chat):
        catalog = page_access.page_map(chat)
        pages = {name: pid for name, pid in catalog.items() if report_page_visible(name)}
        # Poll parts still follow the unchanged source catalog; hiding a display
        # page must not move another page into an incorrectly answered part.
        ids = list(catalog.values())
        valid = set(pages.values())
        access, answered = defaultdict(set), defaultdict(set)
        for answer in page_access.answer_records(chat):
            if answer.get("user_id") is None or answer.get("answered", True) is False:
                continue
            uid = int(answer["user_id"])
            part = int(answer.get("page_access_part") or 0)
            covered = answer.get("answered_page_ids", ids[part * 9:(part + 1) * 9])
            answered[uid].update(set(covered).intersection(valid))
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
                if not report_page_visible(name):
                    continue
                page = pages.setdefault(pid, {"page_id": pid, "name": name, "users": {},
                    "unconfirmed": set(), "tracked_polls": 0, "missing_polls": 0})
                if roster is None:
                    page["missing_polls"] += 1
                    continue
                page["tracked_polls"] += 1
                for user in roster:
                    uid = int(user["user_id"])
                    if business == "veo" and uid in veo_forecast.EXCLUDED_USER_IDS:
                        continue
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
            if not users and (unconfirmed or page["missing_polls"]):
                # No confirmed page roster is not verified zero capacity.
                data.update(average=None, total=None, conservative=None)
            missing_access.update(unconfirmed)
            results.append({"page_id": page["page_id"], "name": page["name"], **data,
                "forecast_scope": "combined_person_dd" if business == "veo" else "receipt_history",
                **({"page_sales_forecast": None} if business == "veo" else {}),
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
            if page["active"] == 0 and page["missing_access"]:
                lines.append(f"{page['name']}: page access pending for {page['missing_access']} Active member(s).")
            else:
                lines.append(f"{page['name']}: {page['active']} active{partial}" if business["business"] == "veo"
                    else f"{page['name']}: {page['active']} active · Avg {average}{suffix}{partial}")
            if business["business"] == "veo":
                summary = page.get("workload_capacity") or staff_capacity.aggregate([], tracked=False)
                if detailed:
                    lines += staff_capacity.render(summary)
                    lines.append(f"Recent-demand sales forecast: Avg {average}{suffix} DD/day per member.")
                else:
                    reference = summary["selling_day"]
                    value = (f"{reference['known_total']:.2f} ({reference['known_members']}/{summary['active_members']} members)"
                        if reference["known_members"] else "n/a" if summary["active_members"] == 0 else "pending")
                    lines[-1] += f" · Selling-day ref {value} · Sales forecast Avg {average}{suffix}"
                    supplied = summary["supplied_work"]
                    if supplied["known_members"]:
                        lines.append(f"With sufficient work: {supplied['known_total']:.2f} ({supplied['known_members']}/{summary['active_members']} members).")
            if detailed:
                total = f"{page['total']:.2f}" if page["total"] is not None else "Unknown"
                safe = f"{page['conservative']:.2f}" if page["conservative"] is not None else "Unknown"
                buffer_label = "Sales buffer (80%)" if business["business"] == "veo" else "Safe target"
                total_label = "Recent-demand sales total" if business["business"] == "veo" else "Estimated total"
                lines += [f"{total_label}: {total} · {buffer_label}: {safe}",
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
        if business["business"] == "veo":
            correction = business["default"].get("calibration") or {}
            if correction.get("per_person"):
                lines.append("Personal models are chosen from earlier personal errors; each uses its own correction.")
            elif correction.get("available"):
                lines.append(f"Recent forecast correction: x{correction['factor']:.2f} (prior matched Active dates).")
            else:
                lines.append("Recent forecast correction: insufficient history; uncalibrated trial.")
            lines.append("VEO figures describe those people's combined VEO DD, not exclusive page sales. All current human members are eligible.")
            lines.append("Selling-day references are conditional on positive output; they are not expected sales or verified maximum capacity. Ads/workload missing stays unknown.")
            if not detailed:
                lines.append("Capacity needs 3 member-specific sufficient-work Active dates; selling-day references use at least 3 positive Active dates.")
    lines += ["", "Avg = estimated output per active member per day, with confirmed page access.",
        "Shared members count once per page; page totals overlap. Planning estimates, not guarantees."]
    return lines

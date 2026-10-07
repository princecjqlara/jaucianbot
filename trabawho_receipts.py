"""Read-only manager reports for combined Trabawho gross and earnings receipts."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from cloud_store import receipt_messages
from daily_automation import MANILA, TRABAWHO, TRABAWHO_CHAT_ID
from receipt_parser import collect_receipts, receipt_topic_rows


def receipt_report(rows: list[dict], start_date: dt.date, end_date: dt.date) -> dict:
    rows = [r for r in rows if r.get("chat_id", TRABAWHO_CHAT_ID) == TRABAWHO_CHAT_ID]
    parsed = collect_receipts(rows, TRABAWHO["receipts"])
    workers = {}

    def person(row):
        user_id, name = row.get("author_id"), row.get("author_name") or "Unknown employee"
        # Do not merge two Telegram users merely because they share a display name.
        key = ("id", user_id) if user_id is not None else ("name", name.casefold())
        return workers.setdefault(key, {
            "user_id": user_id, "name": name, "gross": Decimal(0), "salary": Decimal(0),
            "labeled_tips": Decimal(0), "tip_salary": Decimal(0), "receipt_posts": 0,
            "sales_days": set(), "review_posts": 0, "receipt_evidence": [],
        })

    def in_period(row):
        sent = dt.datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00"))
        return start_date <= sent.astimezone(MANILA).date() <= end_date

    def evidence(row):
        return {"message_id": row["message_id"], "sent_utc": row["sent_utc"],
                "source": row.get("source"),
                "url": f"https://t.me/c/2894511895/{row['message_id']}"}

    for entry in parsed["entries"]:
        row = entry["row"]
        if not in_period(row):
            continue
        worker = person(row)
        worker["receipt_posts"] += 1
        # A receipt post is not necessarily a unique client or a newly completed order.
        worker["sales_days"].add(dt.datetime.fromisoformat(row["sent_utc"].replace(
            "Z", "+00:00")).astimezone(MANILA).date())
        worker["receipt_evidence"].append(evidence(row))
        for line in entry["lines"]:
            worker["gross"] += line["gross"]
            worker["salary"] += line["salary"]
            if line["kind"] == "tip":
                worker["labeled_tips"] += line["gross"]
                worker["tip_salary"] += line["salary"]
    review = []
    for entry in parsed["review"]:
        if in_period(entry["row"]):
            person(entry["row"])["review_posts"] += 1
            review.append({**evidence(entry["row"]), "issues": entry["issues"],
                           "related_message_ids": entry["related_message_ids"]})
    for worker in workers.values():
        worker["sales_days"] = len(worker["sales_days"])
        worker["non_tip_gross"] = worker["gross"] - worker["labeled_tips"]
        worker["company_share"] = worker["gross"] - worker["salary"]
    ordered = sorted(workers.values(), key=lambda w: (-w["gross"], w["name"].casefold()))
    totals = {metric: sum((w[metric] for w in ordered), Decimal(0)) for metric in (
        "gross", "salary", "labeled_tips", "tip_salary", "non_tip_gross", "company_share")}
    totals["receipt_posts"] = sum(w["receipt_posts"] for w in ordered)
    topic_rows, _ = receipt_topic_rows(rows, TRABAWHO["receipts"])
    period_rows = [r for r in topic_rows if in_period(r)]
    return {
        "team": TRABAWHO["name"], "chat_id": TRABAWHO_CHAT_ID, "receipt_thread_id": 4,
        "start_date": start_date, "end_date": end_date, "totals": totals, "workers": ordered,
        "leaders": {"sales": [{"name": w["name"], "value": w["gross"]} for w in ordered if w["gross"] > 0][:5]},
        "review": review,
        "coverage": {
            "topic_posts": len(period_rows), "sources": sorted({r.get("source", "unknown") for r in period_rows}),
            "earliest_receipt_utc": min((r["sent_utc"] for r in period_rows), default=None),
            "latest_receipt_utc": max((r["sent_utc"] for r in period_rows), default=None),
            "attachment_only_posts": sum(not (r.get("text") or "").strip() and
                                         r.get("content_type") in {"photo", "video", "document"} for r in period_rows),
        },
        "unresolved_topic_messages": [evidence(r) for r in parsed["unresolved_topic_rows"] if in_period(r)],
        "coverage_note": (
            "Recorded earnings, not proof of salary payout. Gross includes every accepted outside amount, "
            "including tips and extras; salary uses each inside amount once. Explicit tips without a "
            "written salary split 50/50. Only labeled tips are separated; an unlabeled 50% line may be "
            "a video, revision, or tip. Challenged or ambiguous posts await review and are excluded. "
            "Image-only receipts cannot be read. Receipt authors receive credit; ID-less export names "
            "cannot establish identity or merge with live Telegram IDs. Receipt posts are not unique clients. "
            "An empty period does not establish zero sales; archive coverage may be incomplete."
        ),
    }


def trabawho_receipt_report(days: int, now: dt.datetime) -> dict:
    end_date = now.astimezone(MANILA).date()
    start_date = end_date - dt.timedelta(days=days - 1)
    start = dt.datetime.combine(start_date, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    return receipt_report(receipt_messages(TRABAWHO_CHAT_ID, start, end), start_date, end_date)

"""Read existing Done Deals posts and summarize them without member actions."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from deal_parser import collect_deals


def summarize(rows: list[dict], config: dict, *, historical: bool = False, capped: bool = False) -> dict:
    # Keep the shared duplicate, attachment, page and ownership safeguards.
    parsed = collect_deals(rows, config, historical=historical)
    entries = [entry for entry in parsed["entries"] if entry["kind"] == "dd" and entry["count"] > 0]
    pages = defaultdict(lambda: {"count": 0, "total": Decimal("0")})
    for entry in entries:
        pages[entry["page"]]["count"] += entry["count"]
        pages[entry["page"]]["total"] += entry["gross"]
    count = sum(entry["count"] for entry in entries)
    total = sum((entry["gross"] for entry in entries), Decimal("0"))
    done_rows = {id(row) for row in rows if row.get("thread_id") == config["done"]}
    if historical:
        from deal_parser import DONE_MARKER_RE
        done_rows.update(id(row) for row in rows if row.get("thread_id") is None and DONE_MARKER_RE.search(row.get("text") or ""))
    return {"count": count, "total": total,
            "average": (total / count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if count else None,
            "pages": dict(pages), "capped": capped,
            "review_count": sum(id(row) in done_rows for row in parsed["uncertain_rows"]),
            "duplicate_count": sum(id(row) in done_rows for row in parsed["duplicate_rows"])}


def render(summary: dict) -> str:
    from daily_automation import money
    label = " (readable posts only)" if summary["review_count"] or summary["capped"] else ""
    lines = ["DONE DEAL SUMMARY — READ ONLY", f"Done deals: {summary['count']}{label}",
             f"Recorded deal amounts: {money(summary['total'])}{label}",
             "Average per done deal: " + (money(summary["average"]) + label if summary["average"] is not None else "not available; no readable deals")]
    for page, values in sorted(summary["pages"].items()):
        lines.append(f"• {page}: {values['count']} deals • {money(values['total'])}")
    if summary["duplicate_count"]:
        lines.append(f"Repeated posts excluded: {summary['duplicate_count']}")
    if summary["review_count"]:
        lines.append(f"Posts needing review: {summary['review_count']} (excluded from totals and average)")
    if summary["capped"]:
        lines.append("Partial coverage: the message limit was reached. These figures are not full-day totals.")
    lines.append("Based on readable posts received by the bot. No earnings or payout calculation; no client-token code required.")
    return "\n".join(lines)

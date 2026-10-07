"""Trabawho receipts: written gross outside parentheses, written earnings inside."""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

from deal_parser import normalized_text


AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
CURRENCY = r"(?:PHP|₱)?\s*"
PAIR = re.compile(
    rf"(?P<label>[A-Za-z .:-]*?){CURRENCY}(?P<gross>{AMOUNT})\s*"
    rf"\(\s*{CURRENCY}(?P<salary>{AMOUNT})\s*\)(?P<suffix>[A-Za-z .:-]*)", re.I,
)
TIP_ONLY = re.compile(rf"(?:tips?\s*[:=-]?\s*{CURRENCY}(?P<before>{AMOUNT})|"
                      rf"{CURRENCY}(?P<after>{AMOUNT})\s*tips?)", re.I)
REVIEW_WORDS = re.compile(
    r"\b(?:refund(?:ed)?|cancel(?:led|ed)?|duplicate|unpaid|not\s+(?:yet\s+)?paid|"
    r"hindi\s+pa\s+paid|pakitanggal|correction|corrected|void|total|subtotal|salary|"
    r"sweldo|sahod|deduction)\b", re.I,
)


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def parse_receipt(text: str | None) -> dict:
    """An ambiguous monetary line holds the entire post for review, never partial payroll."""
    lines = []
    issues = []
    context = ""
    for raw in normalized_text(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if REVIEW_WORDS.search(line):
            issues.append("Correction, unpaid receipt, or summary requires review")
            continue
        match = PAIR.fullmatch(line)
        if match:
            gross, salary = _decimal(match["gross"]), _decimal(match["salary"])
            label = " ".join((context, match["label"], match["suffix"])).strip().casefold()
            kind = "tip" if re.search(r"\btips?\b", label) else "sale_or_extra"
            if gross <= 0 or salary > gross:
                issues.append("Invalid gross or salary amount")
            if kind == "tip" and salary != (gross / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
                issues.append("Written tip share differs from the 50/50 rule")
            lines.append({"gross": gross, "salary": salary, "kind": kind,
                          "salary_basis": "written", "text": line})
            context = ""
            continue
        match = TIP_ONLY.fullmatch(line)
        if not match and re.fullmatch(r"tips?\s*[:.-]?", context, re.I):
            match = TIP_ONLY.fullmatch("Tip " + line)
        if match:
            gross = _decimal(match["before"] or match["after"])
            if gross <= 0:
                issues.append("Invalid tip amount")
            lines.append({"gross": gross, "salary": (gross / 2).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP), "kind": "tip",
                "salary_basis": "50/50 rule", "text": line})
            context = ""
            continue
        if re.search(r"\d", line):
            issues.append("Amount is missing parentheses or has an unsupported format")
        else:
            # A separate TIP/VIDEO/REVISION heading applies to the next amount only.
            context = line
    return {"lines": lines, "issues": list(dict.fromkeys(issues)),
            "readable": bool(lines) and not issues}


def receipt_topic_rows(rows: list[dict], topic: int = 4) -> tuple[list[dict], list[dict]]:
    """Exports omit thread IDs; reconstruct membership only from reply ancestors."""
    by_id = {}
    for row in rows:
        previous = by_id.get(row["message_id"])
        priority = lambda r: (r.get("edited_utc") or r.get("sent_utc") or "",
                              r.get("source") == "bot")
        if previous is None or priority(row) >= priority(previous):
            by_id[row["message_id"]] = row
    selected, unresolved = [], []
    for row in by_id.values():
        current, seen, belongs, unknown, resolved = row, set(), False, False, False
        while current and current["message_id"] not in seen:
            seen.add(current["message_id"])
            if current.get("thread_id") is not None:
                belongs = current["thread_id"] == topic
                resolved = True
                break
            parent = current.get("reply_to_message_id")
            if parent == topic:
                belongs = True
                resolved = True
                break
            if parent is None:
                unknown = True
                break
            current = by_id.get(parent)
            if current is None:
                unknown = True
        if current and current["message_id"] in seen and not resolved:
            unknown = True
        if belongs and row["message_id"] != topic:
            selected.append(row)
        elif unknown and ("(" in (row.get("text") or "") or TIP_ONLY.fullmatch((row.get("text") or "").strip())):
            unresolved.append(row)
    return sorted(selected, key=lambda r: (r["sent_utc"], r["message_id"])), unresolved


def collect_receipts(rows: list[dict], topic: int = 4) -> dict:
    selected, unresolved = receipt_topic_rows(rows, topic)
    disputed = {}
    for row in selected:
        parent = row.get("reply_to_message_id")
        if parent != topic and parent is not None and REVIEW_WORDS.search(row.get("text") or ""):
            disputed.setdefault(parent, []).append(row["message_id"])
    entries, review, attachments = [], [], 0
    for row in selected:
        parsed = parse_receipt(row.get("text"))
        if row["message_id"] in disputed:
            parsed["issues"].append("Receipt challenged by reply; manager review required")
            parsed["readable"] = False
        if parsed["readable"]:
            entries.append({"row": row, "lines": parsed["lines"]})
        elif parsed["issues"] or parsed["lines"]:
            review.append({"row": row, "issues": parsed["issues"],
                           "related_message_ids": disputed.get(row["message_id"], [])})
        elif row.get("content_type") in {"photo", "document", "video"}:
            attachments += 1
    return {"entries": entries, "review": review, "unresolved_topic_rows": unresolved,
            "attachment_only_posts": attachments, "topic_posts": len(selected)}

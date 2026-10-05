"""Shared, conservative interpretation of archived deal posts."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from decimal import Decimal


PAGE_RE = re.compile(r"(?im)^[ \t]*page(?:[ \t]+name)?[ \t]*(?::|[-–]|[ \t])[ \t]*([^\n]+)")
PRICE_RE = re.compile(r"(?im)^[ \t]*(?:price[ \t]*deal|pd)[ \t]*:[ \t]*([^\n]*)")
CLOSE_RE = re.compile(r"(?im)^[ \t]*(?:close[ \t]*deals?|cd)[ \t]*:[ \t]*([^\n]*)")
DONE_MARKER_RE = re.compile(r"(?im)^[ \t]*(?:paid\b|(?:total[ \t]*(?:payment|pay)|tp)[ \t]*:)")
DEPOSIT_RE = re.compile(r"(?im)^[ \t]*(?:dp|down[ \t]*payment)[ \t]*:[ \t]*([^\n]*)")
AMOUNT_RE = re.compile(r"(?:php|₱)?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?![\d.,])", re.I)
DATE_RE = re.compile(r"(?i)^(?:date\s*:\s*)?(?:[a-z]+\.?\s+\d{1,2}\s*,?\s*\d{4}|\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4})$")


def normalized_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")


def normalize_page(text: str, configured: dict[str, str]) -> str | None:
    value = normalized_text(text).split("(", 1)[0].strip(" .:-")
    return configured.get(re.sub(r"[^a-z0-9]", "", value.casefold()))


def parse_page(text: str, configured: dict[str, str]) -> str | None:
    matches = PAGE_RE.findall(normalized_text(text))
    pages = {normalize_page(value, configured) for value in matches}
    return pages.pop() if len(pages) == 1 and None not in pages else None


def _amount_expression(value: str) -> Decimal | None:
    # Notes can describe duration or package size; their numbers are not sales.
    # Numeric additions inside notes are ambiguous and require review.
    value = re.sub(r"\([^)]*(?:revision|\btip\b|\bdp\b)[^)]*\)", "", value, flags=re.I)
    if re.search(r"\([^)]*[+=][^)]*\d", value):
        return None
    value = re.sub(r"\([^)]*(?:\)|$)", "", value).strip()
    # Explicit extra fees are excluded even when placed on the PD line.
    value = re.sub(r"\+\s*(?:php|₱)?\s*[\d,.]+\s*(?:revision(?:\s+fee)?|tip)\b.*$", "", value, flags=re.I)
    if re.search(r"\b(?:revision|tip)\b", value, re.I):
        return None
    value = re.sub(r"\s+(?:poster|upgrade)\s*$", "", value, flags=re.I).strip()
    value = value.rstrip("🙏 ")
    value = re.sub(r"\s*(?:&|\band\b)\s*", "+", value, flags=re.I)
    sides = value.split("=")
    if len(sides) > 2:
        return None

    def total(expression: str) -> Decimal | None:
        amounts = []
        for term in expression.split("+"):
            product = re.split(r"\s*[x×*]\s*", term.strip(), flags=re.I)
            if len(product) > 2:
                return None
            match = AMOUNT_RE.fullmatch(product[0])
            if not match:
                return None
            amount = Decimal(match.group(1).replace(",", ""))
            if len(product) == 2:
                if not re.fullmatch(r"[1-9]\d*", product[1]):
                    return None
                amount *= int(product[1])
            amounts.append(amount)
        return sum(amounts, Decimal("0"))

    result = total(sides[0])
    if len(sides) == 2 and (result is None or total(sides[1]) != result):
        return None
    return result


def parse_price(text: str) -> Decimal | None:
    values = PRICE_RE.findall(normalized_text(text))
    if not values:
        return None
    amounts = [_amount_expression(value) for value in values]
    # Conflicting repeated Price Deal fields must never silently use the first.
    return amounts[0] if amounts[0] is not None and len(set(amounts)) == 1 else None


def client_name(text: str) -> str | None:
    for raw in normalized_text(text).splitlines():
        line = " ".join(raw.split()).strip()
        if not line or DATE_RE.fullmatch(line) or re.match(r"(?i)^paid\b", line):
            continue
        if re.match(r"(?i)^(?:client(?:\s+name)?|customer(?:\s+name)?)\s*:", line):
            line = line.split(":", 1)[1].strip()
        elif ":" in line or PAGE_RE.match(line):
            continue
        if re.fullmatch(r"(?i)(?:done|close)\s*deals?", line):
            continue
        if line.casefold() in {"client", "client name", "customer", "customer name", "unknown"}:
            return None
        return line.casefold() if line else None
    return None


def parse_close_count(text: str) -> int | None:
    text = normalized_text(text)
    counts = CLOSE_RE.findall(text)
    if counts:
        if all(re.fullmatch(r"\d+", value.strip()) for value in counts):
            return int(counts[0]) if len(set(value.strip() for value in counts)) == 1 else None
        return None
    price = parse_price(text)
    if price is not None and price > 0:
        return 1
    # A named client with a positive deposit is a CD even if PD is still blank.
    # An invalid or zero price is not silently accepted as a blank price.
    prices = PRICE_RE.findall(text)
    if prices and all(value.strip() in {"", "-", "—"} for value in prices) and client_name(text):
        deposits = DEPOSIT_RE.findall(text)
        if len(deposits) == 1:
            deposit = _amount_expression(deposits[0])
            if deposit is not None and deposit > 0:
                return 1
    return None


def author_identity(row: dict) -> tuple[str, object]:
    if row.get("author_id") is not None:
        return "id", int(row["author_id"])
    return "name", " ".join((row.get("author_name") or "Unknown employee").split()).casefold()


def looks_like_deal(row: dict) -> bool:
    text = normalized_text(row.get("text") or "")
    return bool(PAGE_RE.search(text) or PRICE_RE.search(text) or CLOSE_RE.search(text)
                or DONE_MARKER_RE.search(text) or row.get("content_type") in {"photo", "video", "document"})


def _day(row: dict) -> str:
    sent = row.get("sent_utc")
    return dt.datetime.fromisoformat(sent.replace("Z", "+00:00")).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).date().isoformat() if sent else ""


def collect_deals(rows: list[dict], config: dict, *, historical: bool = False) -> dict:
    """Parse once for reports, reminders, and coaching; retain review evidence."""
    entries, uncertain, duplicates = [], [], []
    seen = set()
    for row in sorted(rows, key=lambda r: (r.get("sent_utc") or "", r.get("message_id") or 0), reverse=True):
        text = normalized_text(row.get("text") or "")
        thread = row.get("thread_id")
        kind = "dd" if thread == config["done"] else "cd" if thread == config["close"] else None
        if historical and thread is None and PAGE_RE.search(text) and PRICE_RE.search(text):
            kind = "dd" if DONE_MARKER_RE.search(text) else "cd"
        if kind is None:
            continue
        page = parse_page(text, config["pages"])
        price = parse_price(text) if kind == "dd" else Decimal("0")
        count = 1 if kind == "dd" else parse_close_count(text)
        if page is None or price is None or (kind == "dd" and price <= 0) or count is None:
            if looks_like_deal(row):
                uncertain.append(row)
            continue
        summary = kind == "cd" and bool(CLOSE_RE.search(text))
        client = client_name(text)
        if summary:
            key = (kind, _day(row), author_identity(row), page, "summary")
        elif client:
            # Same client, author, page, day, and amount is a repost, regardless
            # of payment-note changes. Different clients remain separate deals.
            key = (kind, _day(row), author_identity(row), page, client,
                   price if kind == "dd" else parse_price(text))
        else:
            # Generic templates without a client cannot safely be deduplicated.
            key = None
        if key is not None and key in seen:
            duplicates.append(row)
            continue
        if key is not None:
            seen.add(key)
        entries.append({"row": row, "kind": kind, "page": page, "count": count,
                        "gross": price, "summary": summary})

    # Cross-author reposts may be transfers. Do not award two sales or guess
    # which employee should receive pay until ownership is resolved.
    owners = {}
    for entry in entries:
        client = client_name(entry["row"].get("text") or "")
        if not client or entry["summary"]:
            continue
        key = (entry["kind"], _day(entry["row"]), entry["page"], client,
               entry["gross"] if entry["kind"] == "dd" else parse_price(entry["row"].get("text") or ""))
        owners.setdefault(key, []).append(entry)
    contested = {id(entry) for group in owners.values()
                 if len({author_identity(entry["row"]) for entry in group}) > 1 for entry in group}
    conflicts = [entry["row"] for entry in entries if id(entry) in contested]
    uncertain.extend(conflicts)
    entries = [entry for entry in entries if id(entry) not in contested]

    # A daily CD summary and individual entries describe the same pipeline.
    # Use the larger recorded count rather than adding the summary again.
    individuals = {}
    for entry in entries:
        if entry["kind"] == "cd" and not entry["summary"]:
            row = entry["row"]
            key = (_day(row), author_identity(row), entry["page"])
            individuals[key] = individuals.get(key, 0) + entry["count"]
    for entry in entries:
        if entry["summary"]:
            row = entry["row"]
            key = (_day(row), author_identity(row), entry["page"])
            entry["count"] = max(entry["count"] - individuals.get(key, 0), 0)

    # A captionless photo next to its same-author caption is supporting media.
    # Keep unmatched attachments as review items; no image content is inferred.
    supporting = []
    caption_rows = [entry["row"] for entry in entries] + duplicates + conflicts
    for row in uncertain[:]:
        if (row.get("text") or "").strip() or not row.get("sent_utc"):
            continue
        for other in caption_rows:
            if not other.get("sent_utc") or other.get("thread_id") != row.get("thread_id"):
                continue
            if author_identity(other) != author_identity(row) or _day(other) != _day(row):
                continue
            delta = abs((dt.datetime.fromisoformat(other["sent_utc"].replace("Z", "+00:00"))
                         - dt.datetime.fromisoformat(row["sent_utc"].replace("Z", "+00:00"))).total_seconds())
            if (row.get("content_type") == other.get("content_type")
                    and row.get("content_type") in {"photo", "video", "document"}
                    and delta <= 2
                    and abs((row.get("message_id") or 0) - (other.get("message_id") or 0)) == 1):
                supporting.append(row)
                uncertain.remove(row)
                break
    return {"entries": entries, "uncertain_rows": uncertain, "duplicate_rows": duplicates,
            "supporting_rows": supporting, "ownership_conflicts": conflicts}

"""Import Telegram Desktop HTML group exports into the insights archive."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
from html.parser import HTMLParser
from pathlib import Path

from telegram_insights import ROOT, connect


BREAK = "\ue000"
MESSAGE_ID = re.compile(r"^message(-?\d+)$")
REPLY_ID = re.compile(r"#go_to_message(-?\d+)$")
PAGE_TITLE = re.compile(r'<div class="page_header">.*?<div class="text bold">\s*(.*?)\s*</div>', re.S)


def clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value.replace(BREAK, "\n")


class ExportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.messages: list[dict] = []
        self.skipped: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            classes = set((attributes.get("class") or "").split())
            frame = {"classes": classes, "field": None, "parts": [], "message": False}
            if "message" in classes and "default" in classes:
                match = MESSAGE_ID.match(attributes.get("id") or "")
                if match:
                    self.current = {
                        "message_id": int(match.group(1)), "author_name": None,
                        "sent_utc": None, "text": None, "content_type": "text",
                        "reply_to_message_id": None,
                    }
                    frame["message"] = True
            if self.current is not None:
                if "from_name" in classes:
                    frame["field"] = "author_name"
                elif "text" in classes:
                    frame["field"] = "text"
                elif "date" in classes and "details" in classes:
                    raw = attributes.get("title")
                    if raw:
                        try:
                            when = dt.datetime.strptime(raw, "%d.%m.%Y %H:%M:%S UTC%z")
                            self.current["sent_utc"] = when.astimezone(dt.timezone.utc).isoformat()
                        except ValueError:
                            self.current["unparsed_date"] = raw
                for item in classes:
                    if item.startswith("media_"):
                        self.current["content_type"] = item.removeprefix("media_")
            self.stack.append(frame)
        elif tag == "br" and self.current is not None:
            self.add_text(BREAK)
        elif tag == "a" and self.current is not None:
            if any("reply_to" in frame["classes"] for frame in self.stack):
                match = REPLY_ID.search(attributes.get("href") or "")
                if match:
                    self.current["reply_to_message_id"] = int(match.group(1))

    def handle_endtag(self, tag: str) -> None:
        if tag != "div" or not self.stack:
            return
        frame = self.stack.pop()
        if self.current is not None and frame["field"]:
            value = clean("".join(frame["parts"]))
            if value:
                self.current[frame["field"]] = value
        if frame["message"]:
            if self.current and self.current["sent_utc"]:
                self.messages.append(self.current)
            elif self.current:
                self.skipped.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.add_text(data)

    def add_text(self, value: str) -> None:
        for frame in reversed(self.stack):
            if frame["field"]:
                frame["parts"].append(value)
                break


def export_files(folder: Path) -> list[Path]:
    def number(path: Path) -> int:
        match = re.fullmatch(r"messages(\d*)\.html", path.name)
        return int(match.group(1) or "1") if match else 10**9
    return sorted(folder.glob("messages*.html"), key=number)


def import_folder(db, folder: Path) -> dict:
    files = export_files(folder)
    if not files:
        raise ValueError(f"No Telegram messages HTML files in {folder}")
    first = files[0].read_text(encoding="utf-8")
    title_match = PAGE_TITLE.search(first)
    if not title_match:
        raise ValueError(f"Could not read chat title from {files[0]}")
    title = html.unescape(title_match.group(1)).strip()
    chats = db.execute("SELECT chat_id, approved FROM chats WHERE title=?", (title,)).fetchall()
    if len(chats) != 1 or not chats[0]["approved"]:
        raise ValueError(f"No unique approved bot group matches exported title: {title}")
    chat_id = chats[0]["chat_id"]

    count = 0
    last_author = None
    min_date = None
    max_date = None
    for path in files:
        parser = ExportParser()
        parser.feed(first if path == files[0] else path.read_text(encoding="utf-8"))
        with db:
            for message in parser.messages:
                if message["author_name"]:
                    last_author = message["author_name"]
                else:
                    message["author_name"] = last_author
                source_file = str(path.relative_to(ROOT))
                cursor = db.execute(
                    """
                    INSERT INTO messages(
                        chat_id, message_id, sent_utc, author_name, text,
                        content_type, reply_to_message_id, source, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'export', ?)
                    ON CONFLICT(chat_id, message_id) DO NOTHING
                    """,
                    (
                        chat_id, message["message_id"], message["sent_utc"],
                        message["author_name"], message["text"], message["content_type"],
                        message["reply_to_message_id"], source_file,
                    ),
                )
                count += cursor.rowcount
                date = message["sent_utc"]
                min_date = min(min_date, date) if min_date else date
                max_date = max(max_date, date) if max_date else date
    return {"group": title, "chat_id": chat_id, "files": len(files), "imported_messages": count, "first_utc": min_date, "last_utc": max_date}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Root directory of extracted Telegram HTML exports")
    args = parser.parse_args()
    root = args.folder.resolve()
    db = connect()
    try:
        for folder in sorted(path for path in root.iterdir() if path.is_dir()):
            if export_files(folder):
                print(import_folder(db, folder), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()

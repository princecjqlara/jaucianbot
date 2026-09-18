"""Write a local, Git-ignored environment file without echoing secret input."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / ".env.local"


def validate_key(value: str, role: str, project_ref: str) -> None:
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.split(".")[1] + "==="))
    except (IndexError, ValueError, binascii.Error) as error:
        raise SystemExit(f"Invalid {role} key format") from error
    if payload.get("iss") != "supabase" or payload.get("ref") != project_ref or payload.get("role") != role:
        raise SystemExit(f"{role} key claims do not match this Supabase project")


def replace_service_key() -> None:
    if not TARGET.exists():
        raise SystemExit(".env.local does not exist")
    lines = TARGET.read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    service_key = getpass.getpass("Correct Supabase service role key: ").strip()
    validate_key(service_key, "service_role", urlsplit(values["SUPABASE_URL"]).hostname.split(".")[0])
    updated = [f"SUPABASE_SERVICE_ROLE_KEY={service_key}" if line.startswith("SUPABASE_SERVICE_ROLE_KEY=") else line for line in lines]
    temporary = TARGET.with_name(".env.local.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(updated) + "\n")
    os.replace(temporary, TARGET)
    print("Updated the local service role key.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace-service-key", action="store_true")
    args = parser.parse_args()
    if args.replace_service_key:
        replace_service_key()
        return
    if TARGET.exists():
        raise SystemExit(".env.local already exists; refusing to overwrite it")
    bot_token = getpass.getpass("Telegram bot token: ").strip()
    supabase_url = input("Supabase project URL: ").strip().rstrip("/")
    anon_key = getpass.getpass("Supabase anon key: ").strip()
    service_key = getpass.getpass("Supabase service role key: ").strip()
    parsed = urlsplit(supabase_url)
    if not bot_token or not anon_key or not service_key or parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit("Missing credentials or invalid Supabase URL")
    if any("\n" in value or "\r" in value for value in (bot_token, supabase_url, anon_key, service_key)):
        raise SystemExit("Environment values must each be a single line")
    project_ref = parsed.hostname.split(".")[0]
    validate_key(anon_key, "anon", project_ref)
    validate_key(service_key, "service_role", project_ref)
    db_path = ROOT / "telegram_insights.sqlite3"
    with sqlite3.connect(db_path) as db:
        approved = [str(row[0]) for row in db.execute("SELECT chat_id FROM chats WHERE approved=1 ORDER BY chat_id")]
    values = {
        "TELEGRAM_BOT_TOKEN": bot_token,
        "SUPABASE_URL": supabase_url,
        "SUPABASE_ANON_KEY": anon_key,
        "SUPABASE_SERVICE_ROLE_KEY": service_key,
        "ALLOWED_CHAT_IDS": ",".join(approved),
        "TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "INSIGHTS_API_KEY": secrets.token_urlsafe(32),
    }
    descriptor = os.open(TARGET, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")
    print(f"Created {TARGET.name} with {len(approved)} approved chat IDs and two generated secrets.")


if __name__ == "__main__":
    main()

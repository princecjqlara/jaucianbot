"""Check that the Supabase archive schema is installed and reachable."""

from __future__ import annotations

import sys

from cloud_store import SupabaseError, allowed_chat_ids, archive_status
from local_env import load_local_env


if __name__ == "__main__":
    load_local_env()
    try:
        groups = archive_status(allowed_chat_ids())
    except SupabaseError as error:
        print(f"Supabase archive is not ready: {error}. Run supabase/schema.sql in the SQL Editor.", file=sys.stderr)
        sys.exit(1)
    print(f"Supabase archive ready; {len(groups)} approved groups currently stored.")

"""Check that the Supabase archive schema is installed and reachable."""

from __future__ import annotations

import sys

from cloud_store import SupabaseError, allowed_chat_ids, archive_status
from local_env import load_local_env


def main() -> int:
    load_local_env()
    try:
        groups = archive_status(allowed_chat_ids())
    except SupabaseError as error:
        if error.restriction == "exceed_egress_quota":
            remedy = (
                "The archive project's data-transfer quota is exhausted. "
                "The owner must review usage and billing in the Supabase dashboard. "
                "Immediate recovery requires a plan upgrade or disabling the spend cap "
                "(additional charges may apply); otherwise wait for the billing-cycle quota reset. "
                "Reinstalling the schema will not fix this restriction."
            )
        elif error.http_status == 402:
            remedy = "Review the project's service restriction in the Supabase billing dashboard."
        elif error.api_code in {"PGRST202", "PGRST205", "42883", "42P01"}:
            remedy = "Check the missing archive tables/functions against supabase/schema.sql in the SQL Editor."
        else:
            remedy = "Check database connectivity and project configuration."
        print(f"Supabase archive is not ready: {error}. {remedy}", file=sys.stderr)
        return 1
    print(f"Supabase archive ready; {len(groups)} approved groups currently stored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

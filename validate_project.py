"""Block deployments with failing tests; run checks without production credentials."""

from pathlib import Path
import os
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    if not list((root / "tests").glob("test_*.py")):
        print("Validation failed: regression tests are missing.", file=sys.stderr)
        return 1
    prefixes = ("TELEGRAM_", "SUPABASE_", "CRM_", "SUNO_", "INSIGHTS_", "HOURLY_", "NEW_CLIENT_", "ARCHIVE_", "DONE_DEAL_")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(prefixes) and key not in {"ALLOWED_CHAT_IDS", "CRONJOB_API_KEY"}}
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                          cwd=root, env=environment).returncode


if __name__ == "__main__":
    sys.exit(main())

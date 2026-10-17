"""Block deployments with failing tests; run checks without production credentials."""

from pathlib import Path
import os
import subprocess
import sys


# These suites must travel with every deployment, even when other tests remain.
REQUIRED_TEST_FILES = (
    "test_poll_ready_recovery.py", "test_queue_fairness_recovery.py",
    "test_ready_handoffs.py", "test_new_client_automation.py",
    "test_hourly_assignments.py", "test_database_scheduler.py",
    "test_validation_gate.py",
)


def main() -> int:
    root = Path(__file__).resolve().parent
    if not list((root / "tests").glob("test_*.py")):
        print("Validation failed: regression tests are missing.", file=sys.stderr)
        return 1
    missing = [name for name in REQUIRED_TEST_FILES
               if not (root / "tests" / name).is_file() or not (root / "tests" / name).stat().st_size]
    if missing:
        print("Validation failed: required regression suites are missing or empty: " + ", ".join(missing),
              file=sys.stderr)
        return 1
    prefixes = ("TELEGRAM_", "SUPABASE_", "CRM_", "SUNO_", "INSIGHTS_", "HOURLY_", "NEW_CLIENT_", "ARCHIVE_", "DONE_DEAL_")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(prefixes) and key not in {"ALLOWED_CHAT_IDS", "CRONJOB_API_KEY"}}
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                          cwd=root, env=environment).returncode


if __name__ == "__main__":
    sys.exit(main())

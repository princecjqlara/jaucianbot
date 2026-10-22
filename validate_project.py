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
    "test_veo_handovers_disabled.py",
    "test_suno_supply.py",
    "test_automation_controls.py",
    "test_veo_ready_policy.py",
    "test_page_access.py", "test_member_capacity.py",
    "test_page_access_reminders.py",
    "test_page_capacity_reports.py",
    "test_nightly_reports.py",
    "test_capacity_daily_reports.py",
    "test_suno_capacity.py", "test_suno_team_policy.py",
    "test_tomorrow_capacity.py",
    "test_makata_automation.py",
    "test_full_lists_and_sama_scope.py",
    "test_organized_reports.py",
    "test_recent_capacity.py",
    "test_member_presence_and_salary_totals.py",
    "test_veo_client_feature_switch.py",
    "test_capacity_defaults.py",
    "test_availability_reminders.py",
    "test_suno_reference_defaults.py",
    "test_report_replacement.py",
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
    prefixes = ("TELEGRAM_", "SUPABASE_", "CRM_", "SUNO_", "INSIGHTS_", "HOURLY_", "NEW_CLIENT_", "VEO_", "ARCHIVE_", "DONE_DEAL_", "AUTOMATION_", "MEMBER_CAPACITY_", "ORGANIZED_", "AVAILABILITY_", "PAGE_ACCESS_")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(prefixes) and key not in {"ALLOWED_CHAT_IDS", "CRONJOB_API_KEY"}}
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                          cwd=root, env=environment).returncode


if __name__ == "__main__":
    sys.exit(main())

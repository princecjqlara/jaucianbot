import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import validate_project


class ValidationGateTests(unittest.TestCase):
    def test_deployment_with_other_tests_but_missing_queue_regressions_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            (root / "tests" / "test_other.py").write_text("import unittest\n", encoding="utf-8")
            with patch.object(validate_project, "__file__", str(root / "validate_project.py")), \
                    patch.object(validate_project.subprocess, "run") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                run.return_value.returncode = 0
                self.assertEqual(validate_project.main(), 1)
            run.assert_not_called()

    def test_empty_critical_suite_is_blocked_even_if_other_suites_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            for name in validate_project.REQUIRED_TEST_FILES:
                (root / "tests" / name).write_text("import unittest\n", encoding="utf-8")
            (root / "tests" / "test_poll_ready_recovery.py").write_text("", encoding="utf-8")
            with patch.object(validate_project, "__file__", str(root / "validate_project.py")), \
                    patch.object(validate_project.subprocess, "run") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(validate_project.main(), 1)
            run.assert_not_called()

    def test_complete_artifact_runs_gate_without_production_credentials_and_preserves_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            for name in validate_project.REQUIRED_TEST_FILES:
                (root / "tests" / name).write_text("import unittest\n", encoding="utf-8")
            for code in (0, 1):
                with self.subTest(code=code), \
                        patch.object(validate_project, "__file__", str(root / "validate_project.py")), \
                        patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-private", "CRM_SUPABASE_SERVICE_ROLE_KEY": "test-private", "ALLOWED_CHAT_IDS": "-100123"}), \
                        patch.object(validate_project.subprocess, "run") as run:
                    run.return_value.returncode = code
                    self.assertEqual(validate_project.main(), code)
                    args, kwargs = run.call_args
                    self.assertEqual(args[0][-3:], ["-s", "tests", "-q"])
                    self.assertIn("discover", args[0])
                    self.assertNotIn("TELEGRAM_BOT_TOKEN", kwargs["env"])
                    self.assertNotIn("CRM_SUPABASE_SERVICE_ROLE_KEY", kwargs["env"])
                    self.assertNotIn("ALLOWED_CHAT_IDS", kwargs["env"])

import contextlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from cloud_store import SupabaseError, request
from verify_supabase import main


class SupabaseRestrictionTests(unittest.TestCase):
    def test_quota_restriction_is_reported_without_raw_response_details(self):
        body = json.dumps({
            "message": "Service restricted: exceed_egress_quota. private-value",
            "secret": "private-value",
        }).encode()
        error = urllib.error.HTTPError("https://example.test", 402, "restricted", {}, io.BytesIO(body))
        with patch("cloud_store.credentials", return_value=("https://example.test", "test-key")), patch(
            "cloud_store.urllib.request.urlopen", side_effect=error
        ), self.assertRaises(SupabaseError) as raised:
            request("rpc/insights_status")
        self.assertEqual(raised.exception.http_status, 402)
        self.assertEqual(raised.exception.restriction, "exceed_egress_quota")
        self.assertNotIn("private-value", str(raised.exception))

    def test_non_json_error_keeps_http_status(self):
        error = urllib.error.HTTPError("https://example.test", 502, "bad gateway", {}, io.BytesIO(b"private-value"))
        with patch("cloud_store.credentials", return_value=("https://example.test", "test-key")), patch(
            "cloud_store.urllib.request.urlopen", side_effect=error
        ), self.assertRaises(SupabaseError) as raised:
            request("rpc/insights_status")
        self.assertEqual(raised.exception.http_status, 502)
        self.assertIsNone(raised.exception.restriction)
        self.assertNotIn("private-value", str(raised.exception))

    def test_verification_distinguishes_quota_from_missing_schema(self):
        for error, expected in (
            (SupabaseError("quota", http_status=402, restriction="exceed_egress_quota"), "quota reset"),
            (SupabaseError("missing function", http_status=404, api_code="PGRST202"), "missing archive tables/functions"),
        ):
            with self.subTest(expected=expected), patch("verify_supabase.load_local_env"), patch(
                "verify_supabase.allowed_chat_ids", return_value={-1004461399292}
            ), patch("verify_supabase.archive_status", side_effect=error), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(main(), 1)
                self.assertIn(expected, output.getvalue())


if __name__ == "__main__":
    unittest.main()

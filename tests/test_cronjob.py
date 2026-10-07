import unittest
from unittest.mock import patch

from configure_cronjob import JOB_TITLE, configure, dispatch_job_payload, job_payload


class CronJobTests(unittest.TestCase):
    def test_payload_checks_authenticated_status_every_fifteen_minutes(self):
        job = job_payload("https://example.vercel.app/", "secret")
        self.assertEqual(job["title"], JOB_TITLE)
        self.assertEqual(job["url"], "https://example.vercel.app/api/status")
        self.assertEqual(job["schedule"]["minutes"], [0, 15, 30, 45])
        self.assertEqual(job["extendedData"]["headers"]["Authorization"], "Bearer secret")
        self.assertFalse(job["saveResponses"])

    @patch("configure_cronjob.api_request")
    def test_updates_existing_job(self, request):
        request.side_effect = [{"jobs": [{"jobId": 42, "title": JOB_TITLE}]}, {}]
        result = configure("cron-key", "https://example.vercel.app", "insights-key")
        self.assertEqual(result, ("updated", 42))
        self.assertEqual(request.call_args_list[1].args[:3], ("cron-key", "PATCH", "/jobs/42"))

    @patch("configure_cronjob.api_request")
    def test_creates_missing_job(self, request):
        request.side_effect = [{"jobs": []}, {"jobId": 99}]
        result = configure("cron-key", "https://example.vercel.app", "insights-key")
        self.assertEqual(result, ("created", 99))
        self.assertEqual(request.call_args_list[1].args[:3], ("cron-key", "PUT", "/jobs"))

    def test_dispatch_payload_runs_every_minute_without_archive_credentials(self):
        job = dispatch_job_payload("https://example.vercel.app/")
        self.assertEqual(job["url"], "https://example.vercel.app/api/cron/dispatch")
        self.assertEqual(job["schedule"]["minutes"], [-1])
        self.assertEqual(job["schedule"]["hours"], [-1])
        self.assertEqual(job["extendedData"]["headers"], {})
        self.assertTrue(job["enabled"])

    @patch("configure_cronjob.api_request")
    def test_dispatch_updates_matching_url_and_preserves_health_job(self, request):
        request.side_effect = [{"jobs": [
            {"jobId": 42, "title": JOB_TITLE, "url": "https://example.vercel.app/api/status"},
            {"jobId": 77, "title": "Custom dispatch name", "url": "https://example.vercel.app/api/cron/dispatch"},
        ]}, {}]
        result = configure("cron-key", "https://example.vercel.app", "", dispatch=True)
        self.assertEqual(result, ("updated", 77))
        self.assertEqual(request.call_args.args[:3], ("cron-key", "PATCH", "/jobs/77"))


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from configure_cronjob import JOB_TITLE, configure, job_payload


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


if __name__ == "__main__":
    unittest.main()

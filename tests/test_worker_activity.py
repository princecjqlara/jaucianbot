import datetime as dt
import unittest
from unittest.mock import patch

from worker_activity import worker_activity_report


CHAT = -1003647732254
NOW = dt.datetime(2026, 9, 22, 4, tzinfo=dt.timezone.utc)


class WorkerActivityTests(unittest.TestCase):
    def test_builds_transparent_rankings_hours_and_coaching(self):
        messages = [
            {"message_id": 1, "sent_utc": "2026-09-21T02:00:00+00:00", "author_id": 1,
             "author_name": "Alex", "thread_id": 1135, "content_type": "text",
             "text": "Page: Azshinari\nPrice Deal: 1,000"},
            {"message_id": 2, "sent_utc": "2026-09-22T02:00:00+00:00", "author_id": 1,
             "author_name": "Alex", "thread_id": 1135, "content_type": "text",
             "text": "Page: Azshinari\nPrice Deal: 800"},
            {"message_id": 3, "sent_utc": "2026-09-21T11:00:00+00:00", "author_id": 1,
             "author_name": "Alex", "thread_id": 1132, "content_type": "text",
             "text": "Page: Azshinari\nClose Deal: 1"},
            {"message_id": 4, "sent_utc": "2026-09-21T12:00:00+00:00", "author_id": 2,
             "author_name": "Bea", "thread_id": 1132, "content_type": "text",
             "text": "Page: Azshinari\nClose Deal: 1"},
        ]
        answers = [
            {"user_id": 1, "user_name": "Alex", "active": True,
             "daily_polls": {"work_date": "2026-09-21"}},
            {"user_id": 1, "user_name": "Alex", "active": True,
             "daily_polls": {"work_date": "2026-09-22"}},
            {"user_id": 2, "user_name": "Bea", "active": True,
             "daily_polls": {"work_date": "2026-09-21"}},
            {"user_id": 2, "user_name": "Bea", "active": True,
             "daily_polls": {"work_date": "2026-09-22"}},
            {"user_id": 3, "user_name": "Casey", "active": False,
             "daily_polls": {"work_date": "2026-09-21"}},
        ]
        freebies = [{
            "payload": {
                "freebie_token": "ABCDEF12", "freebie_assignee_id": 1,
                "freebie_assignee_name": "Alex", "freebie_completed_at": "2026-09-21T05:00:00+00:00",
            }
        }]
        new_clients = [{
            "payload": {
                "new_client_token": "1234ABCD", "new_client_assignee_id": 1,
                "new_client_assignee_name": "Alex", "new_client_acknowledged_at": "2026-09-21T06:00:00+00:00",
            }
        }]
        with patch("worker_activity.activity_messages", return_value=messages), patch(
            "worker_activity.poll_answers_for_range", return_value=answers
        ), patch("worker_activity.freebie_actions", return_value=freebies), patch(
            "worker_activity.new_client_actions", return_value=new_clients
        ):
            report = worker_activity_report(CHAT, 2, NOW)

        self.assertEqual(report["leaders"]["overall_activity"][0], {"name": "Alex", "value": 8})
        self.assertEqual(report["leaders"]["confirmed_freebies"][0], {"name": "Alex", "value": 1})
        self.assertEqual(report["leaders"]["acknowledged_new_clients"][0], {"name": "Alex", "value": 1})
        alex = next(worker for worker in report["workers"] if worker["name"] == "Alex")
        bea = next(worker for worker in report["workers"] if worker["name"] == "Bea")
        casey = next(worker for worker in report["workers"] if worker["name"] == "Casey")
        self.assertEqual((alex["dd"], alex["cd"], alex["gross"], alex["freebies"], alex["new_clients"]), (2, 1, 1800, 1, 1))
        self.assertEqual(alex["consistency_percent"], 100.0)
        self.assertEqual(alex["best_hour_pht"], 10)
        self.assertEqual(bea["active_days_without_deals"], 1)
        self.assertTrue(any("no readable CD or DD" in item for item in bea["recommendations"]))
        self.assertEqual(casey["inactive_days"], 1)
        self.assertEqual(casey["active_days"], 0)


if __name__ == "__main__":
    unittest.main()

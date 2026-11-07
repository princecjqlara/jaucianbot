import datetime as dt
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import veo_workforce as workforce
from daily_automation import GROUPS

CHAT = next(iter(GROUPS))
TENANT = "00000000-0000-4000-8000-000000000001"


class WorkforceTests(unittest.TestCase):
    def test_current_poll_metadata_distinguishes_unanswered_from_a_withdrawn_vote(self):
        tomorrow = str(dt.datetime.now(workforce.MANILA).date() + dt.timedelta(days=1))
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        polls = [{"chat_id": CHAT, "poll_id": "current", "work_date": tomorrow, "created_at": now, "telegram_message_id": 99}]
        with patch("telegram_sender.telegram_call", return_value={"status": "member", "user": {"id": 42, "is_bot": False}}):
            missing = workforce.inspect(self.data()["accounts"][0], {CHAT}, [], [], [], set(), "2026-11-05T00:00:00Z", polls)
            voted = workforce.inspect(self.data()["accounts"][0], {CHAT}, [{"poll_id": "current", "user_id": 42, "active": False, "updated_at": now, "daily_polls": {"chat_id": CHAT, "work_date": tomorrow}}], [], [], set(), "2026-11-05T00:00:00Z", polls)
        self.assertFalse(missing["availability"]["has_vote"])
        self.assertTrue(voted["availability"]["has_vote"])
        self.assertFalse(voted["attendance"]["active"])
        self.assertEqual(voted["availability"]["poll_id"], "current")

    def data(self):
        return {"tenant_id": TENANT, "launch_at": "2026-11-05T00:00:00Z", "pages": [{"id": "allowed-page"}],
                "accounts": [{"user_id": "worker", "role": "member", "telegram_chat_id": CHAT, "telegram_user_id": 42},
                             {"user_id": "unmapped", "role": "manager"}, {"user_id": "owner", "role": "owner"}]}

    def test_snapshot_uses_only_explicit_mappings_and_preserves_unknown_lookup_failures(self):
        with patch.object(workforce, "workspace_directory", return_value=self.data()), patch.object(workforce, "teams", return_value={CHAT}), \
                patch.object(workforce, "request", return_value=[]), patch("telegram_sender.telegram_call", side_effect=RuntimeError("network")):
            data = workforce.snapshot(TENANT)
        self.assertEqual(data["unmapped"], 1)
        self.assertEqual(len(data["workers"]), 1)
        self.assertEqual(data["workers"][0]["membership"], "unknown")
        self.assertFalse(data["workers"][0]["membership_confirmed"])

    def test_positive_human_membership_and_tomorrow_votes_are_joined_by_chat_and_numeric_identity(self):
        tomorrow = str(dt.datetime.now(workforce.MANILA).date() + dt.timedelta(days=1))
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        answers = [{"user_id": 42, "active": True, "updated_at": timestamp, "daily_polls": {"chat_id": CHAT, "work_date": tomorrow}},
                   {"user_id": 42, "active": False, "updated_at": timestamp, "daily_polls": {"chat_id": -999, "work_date": tomorrow}}]
        with patch("telegram_sender.telegram_call", return_value={"status": "member", "user": {"id": 42, "is_bot": False}}):
            row = workforce.inspect(self.data()["accounts"][0], {CHAT}, answers, [], [], {"allowed-page"}, "2026-11-05T00:00:00Z")
        self.assertTrue(row["attendance"]["active"])
        self.assertTrue(row["membership_confirmed"])

    def test_left_is_explicit_and_unexpected_identity_is_unknown(self):
        for response, expected in (({"status": "left", "user": {"id": 42, "is_bot": False}}, "left"),
                                   ({"status": "member", "user": {"id": 43, "is_bot": False}}, "unknown"),
                                   ({"status": "member", "user": {"id": 42, "is_bot": True}}, "unknown")):
            with self.subTest(response=response), patch("telegram_sender.telegram_call", return_value=response):
                row = workforce.inspect(self.data()["accounts"][0], {CHAT}, [], [], [], set(), "2026-11-05T00:00:00Z")
            self.assertEqual(row["membership"], expected)

    def test_withdrawal_overrides_an_older_vote_without_becoming_not_active(self):
        tomorrow = str(dt.datetime.now(workforce.MANILA).date() + dt.timedelta(days=1))
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        answers = [{"user_id": 42, "active": True, "updated_at": old, "daily_polls": {"chat_id": CHAT, "work_date": tomorrow}}]
        votes = [{"chat_id": CHAT, "payload": {"user_id": 42, "active": None, "work_date": tomorrow, "updated_at": now}}]
        with patch("telegram_sender.telegram_call", return_value={"status": "member", "user": {"id": 42, "is_bot": False}}):
            row = workforce.inspect(self.data()["accounts"][0], {CHAT}, answers, votes, [], set(), "2026-11-05T00:00:00Z")
        self.assertIsNone(row["attendance"]["active"])

    def test_page_declarations_are_filtered_and_never_written_as_application_grants(self):
        pages = [{"chat_id": CHAT, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "payload": {"user_id": 42, "page_ids": ["allowed-page", "foreign-page"]}}]
        with patch("telegram_sender.telegram_call", return_value={"status": "member", "user": {"id": 42, "is_bot": False}}), patch.object(workforce, "push_snapshot") as push:
            row = workforce.inspect(self.data()["accounts"][0], {CHAT}, [], [], pages, {"allowed-page"}, "2026-11-05T00:00:00Z")
        self.assertEqual(row["page_access"]["page_ids"], ["allowed-page"])
        push.assert_not_called()

    def test_delayed_legacy_poll_write_cannot_overwrite_the_newer_update_id_fenced_vote(self):
        tomorrow = str(dt.datetime.now(workforce.MANILA).date() + dt.timedelta(days=1))
        now = dt.datetime.now(dt.timezone.utc)
        answers = [{"poll_id": "poll", "user_id": 42, "active": False, "updated_at": now.isoformat(), "daily_polls": {"chat_id": CHAT, "work_date": tomorrow}}]
        votes = [{"chat_id": CHAT, "payload": {"poll_id": "poll", "user_id": 42, "active": True, "work_date": tomorrow, "updated_at": (now-dt.timedelta(seconds=1)).isoformat()}}]
        with patch("telegram_sender.telegram_call", return_value={"status": "member", "user": {"id": 42, "is_bot": False}}):
            row = workforce.inspect(self.data()["accounts"][0], {CHAT}, answers, votes, [], set(), "2026-11-05T00:00:00Z")
        self.assertTrue(row["attendance"]["active"])

    def test_vote_during_registration_outage_is_recovered_from_acknowledged_outbox(self):
        tomorrow = str(dt.datetime.now(workforce.MANILA).date() + dt.timedelta(days=1))
        update = {"update_id": 10, "poll_answer": {"poll_id": "poll", "user": {"id": 42, "is_bot": False}, "option_ids": []}}
        calls = []
        def request(path, payload=None, **kwargs):
            calls.append((path, payload))
            if path.startswith("daily_polls?"):
                return []
            if "_veo_poll_id" in parse_qs(urlsplit("https://db/" + path).query).get("select", [""])[0]:
                return []
            if path.startswith("scheduled_actions?") and payload is None:
                return [{"chat_id": CHAT, "payload": {"_veo_poll_id": "poll", "veo_availability_date": tomorrow}}]
            return [{"id": 1}]
        with patch.dict(os.environ, {"VEO_WORKSPACE_TENANT_ID": TENANT}), patch.object(workforce, "request", side_effect=request):
            workforce.observe_vote(update, {CHAT})
        saved = [p for _, p in calls if p is not None][0]
        self.assertIsNone(saved["payload"]["active"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertTrue(saved["dedupe_key"].startswith("workspace-vote:"))

    def test_snapshot_bounds_queries_and_paginates_the_mapping_roster(self):
        data = self.data()
        data["accounts"] = [{"user_id": str(i).zfill(3), "role": "member", "telegram_user_id": i+10, "telegram_chat_id": CHAT} for i in range(10)]
        with patch.object(workforce, "workspace_directory", return_value=data), patch.object(workforce, "teams", return_value={CHAT}), patch.object(workforce, "request", return_value=[]), \
                patch.object(workforce, "inspect", side_effect=lambda p, *args: {"user_id": p["user_id"]}):
            first = workforce.snapshot(TENANT, limit=8)
            last = workforce.snapshot(TENANT, offset=8, limit=8)
        self.assertEqual(first["next_offset"], 8)
        self.assertEqual(len(last["workers"]), 2)
        self.assertEqual(last["next_offset"], 0)

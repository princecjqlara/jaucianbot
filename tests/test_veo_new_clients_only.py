import datetime as dt
import io
import json
import os
import unittest
from unittest.mock import patch, MagicMock
import veo_mode
import veo_workspace as workspace
import veo_workforce
import telegram_sender

ENV = {"VEO_WORKSPACE_ENABLED": "true", "VEO_NEW_CLIENTS_ONLY": "true", "VEO_JAUCIAN_SECRET": "secret"}


class NewClientsOnlyTests(unittest.TestCase):
    def test_receipt_handoff_is_refused_before_reading_or_queueing_a_sale(self):
        with patch.dict(os.environ, ENV), patch.object(workspace, "request") as db, patch.object(workspace, "authoritative") as source:
            start = MagicMock()
            result = workspace.receive({"HTTP_AUTHORIZATION": "Bearer secret"}, start,
                                       lambda start, status, body: (status, body))
            self.assertEqual(result[0], 410)
            source.assert_not_called()
            db.assert_not_called()

    def test_delivery_cancels_non_intake_before_any_telegram_attempt(self):
        actions = [{"id": i, "chat_id": -123, "payload": {key: "value"}} for i, key in enumerate(
            ("veo_submission_id", "veo_report_date", "veo_availability_date"), 1)]
        actions.append({"id": 4, "chat_id": -123, "payload": {"veo_intake_help": True, "veo_submission_id": "mixed"}})
        with patch.dict(os.environ, ENV), patch("cloud_store.claim_scheduled_actions", return_value=actions), \
                patch("cloud_store._update_claim", return_value=True) as cancel, patch.object(workspace, "send") as send, \
                patch("veo_intake.send") as intake:
            self.assertEqual(workspace.deliver({-123}), (4, 0, 0))
            self.assertEqual(cancel.call_count, 4)
            self.assertTrue(all(c.args[1]["status"] == "cancelled" for c in cancel.call_args_list))
            send.assert_not_called()
            intake.assert_not_called()

    def test_new_client_messages_keep_the_normal_delivery_checkpoint(self):
        action = {"id": 1, "chat_id": -123, "payload": {"veo_intake_request_id": "request"}}
        with patch.dict(os.environ, ENV), patch("cloud_store.claim_scheduled_actions", return_value=[action]), \
                patch("cloud_store._update_claim", return_value=True), patch("cloud_store.finish_scheduled_action", return_value=True) as finish, \
                patch("veo_intake.send", return_value={"message_id": 77}) as send:
            self.assertEqual(workspace.deliver({-123}), (1, 1, 0))
            send.assert_called_once()
            self.assertEqual(finish.call_args.kwargs["telegram_message_id"], 77)

    def test_reports_polls_and_direct_sender_paths_are_disabled_but_default_is_compatible(self):
        with patch.dict(os.environ, ENV), patch("veo_shift_reports.queue") as queue, patch.object(telegram_sender, "telegram_call") as telegram:
            self.assertEqual(workspace.queue_reports(dt.datetime.now(dt.timezone.utc), {-123}), 0)
            queue.assert_not_called()
            for helper in (workspace.send, workspace.send_report, workspace.send_receipt_details):
                with self.assertRaises(ValueError):
                    helper({})
            with self.assertRaises(telegram_sender.TelegramError):
                telegram_sender.send_scheduled_action({"chat_id": -123, "payload": {"veo_submission_id": "sale"}})
            telegram.assert_not_called()
        with patch.dict(os.environ, {**ENV, "VEO_NEW_CLIENTS_ONLY": "false"}):
            self.assertFalse(veo_mode.new_clients_only())

    def test_old_attendance_poll_votes_do_not_start_workforce_processing(self):
        with patch.dict(os.environ, ENV), patch.object(veo_workforce, "request") as db:
            veo_workforce.observe_vote({"poll_answer": {"user": {"id": 5, "is_bot": False}, "option_ids": [0]}}, {-123})
            db.assert_not_called()

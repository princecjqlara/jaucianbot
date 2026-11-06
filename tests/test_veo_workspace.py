import datetime as dt
import io
import json
import os
import unittest
from unittest.mock import patch, MagicMock

import veo_workspace as workspace
from daily_automation import GROUPS, SUNO_GROUPS
import automation_controls
import app


def fixture():
    row = {key: "00000000-0000-4000-8000-000000000001" for key in
           ("submission_id", "tenant_id", "team_id", "seller_id", "page_id", "contact_id", "order_id")}
    row.update(telegram_chat_id=next(iter(GROUPS)), telegram_user_id=10, total_minor=10000, paid_minor=10000,
               closed_at="2026-11-01T01:00:00Z", done_at="2026-11-01T02:00:00Z",
               expires_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(),
               currency="PHP", seller_name="Worker", contact_name="Client", description="Video", photo_url="https://example.org/private")
    return row


class WorkspaceTests(unittest.TestCase):
    def test_health_fails_for_an_unacknowledged_delivery_hold(self):
        statuses = []
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true"}), patch.object(workspace, "review_required", return_value=True):
            body = app.app({"PATH_INFO": "/api/health", "REQUEST_METHOD": "GET"}, lambda status, headers: statuses.append(status))
        self.assertTrue(statuses[0].startswith("503"))
        self.assertEqual(json.loads(b"".join(body))["reason"], "veo_delivery_reconciliation_required")

    def test_reupload_recovers_a_failed_outbox_without_losing_an_acknowledgment(self):
        row = fixture()
        raw = json.dumps(row).encode()
        def environ():
            return {"HTTP_AUTHORIZATION": "Bearer secret", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        failed = {"id": 1, "chat_id": row["telegram_chat_id"], "status": "failed", "attempts": 5,
            "payload": {"veo_submission_id": "old-submission", "_veo_sending": False}, "telegram_message_id": None}
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true", "VEO_JAUCIAN_SECRET": "secret"}), \
                patch.object(workspace, "validate"), patch.object(workspace, "authoritative", return_value=row), \
                patch.object(workspace, "request", return_value=[failed]) as store, patch.object(workspace, "enqueue_scheduled_action") as enqueue:
            result = workspace.receive(environ(), None, lambda _, status, payload: payload)
        enqueue.assert_not_called()
        update = store.call_args
        self.assertEqual(update.args[1]["attempts"], 0)
        self.assertEqual(update.args[1]["status"], "pending")
        self.assertEqual(update.args[1]["payload"]["veo_submission_id"], row["submission_id"])
        self.assertIn("_veo_message_id", update.args[0])
        self.assertIn("is.null", update.args[0])
        self.assertEqual(result["status"], "pending")

    def test_known_telegram_acknowledgment_finishes_without_replacing_or_resending(self):
        row = fixture()
        raw = json.dumps(row).encode()
        environ = {"HTTP_AUTHORIZATION": "Bearer secret", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        acknowledged = {"id": 1, "status": "failed", "payload": {"veo_submission_id": "old-photo", "_veo_message_id": 99}, "telegram_message_id": None}
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true", "VEO_JAUCIAN_SECRET": "secret"}), \
                patch.object(workspace, "validate"), patch.object(workspace, "authoritative", return_value=row), \
                patch.object(workspace, "request", return_value=[acknowledged]) as store, patch.object(workspace, "enqueue_scheduled_action") as enqueue:
            result = workspace.receive(environ, None, lambda _, status, payload: payload)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(store.call_args.args[1]["telegram_message_id"], 99)
        self.assertNotIn("payload", store.call_args.args[1])
        enqueue.assert_not_called()

    def test_handoff_rejects_another_tenant_before_delivery(self):
        with patch.dict(os.environ, {"VEO_WORKSPACE_TENANT_ID": "00000000-0000-4000-8000-000000000099"}):
            with self.assertRaisesRegex(ValueError, "another tenant"):
                workspace.validate(fixture())

    def test_incomplete_telegram_response_is_not_safe_to_resend(self):
        from telegram_sender import telegram_call, TelegramError
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok":true,')
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), patch("telegram_sender.chat_enabled", return_value=True), patch("telegram_sender.urllib.request.urlopen", return_value=response):
            with self.assertRaises(TelegramError) as caught:
                telegram_call("sendPhoto", {"chat_id": 1, "photo": "test"})
        self.assertTrue(caught.exception.delivery_uncertain)

    def test_success_without_a_message_id_requires_reconciliation(self):
        from telegram_sender import telegram_call, TelegramError
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok":true,"result":{}}')
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), patch("telegram_sender.chat_enabled", return_value=True), patch("telegram_sender.urllib.request.urlopen", return_value=response):
            with self.assertRaises(TelegramError) as caught:
                telegram_call("sendPhoto", {"chat_id": 1, "photo": "test"})
        self.assertTrue(caught.exception.delivery_uncertain)

    def test_suno_disabled_even_if_previously_approved(self):
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true"}, clear=True):
            active = automation_controls.enabled_chat_ids(set(GROUPS) | set(SUNO_GROUPS))
        self.assertEqual(active, set(GROUPS))
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true"}, clear=True), patch("makata_automation.enabled", return_value=True), patch("makata_automation.private_enabled", return_value=True):
            self.assertFalse(automation_controls.chat_enabled(99))

    def test_receiver_requires_authentication_and_never_sends_directly(self):
        raw = json.dumps(fixture()).encode()
        environ = {"PATH_INFO": "/api/veo/handoff", "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        statuses = []
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true", "VEO_JAUCIAN_SECRET": "secret"}), patch("telegram_sender.telegram_call") as send:
            app.app(environ, lambda status, headers: statuses.append(status))
            self.assertTrue(statuses[0].startswith("401"))
            send.assert_not_called()

    def test_duplicate_handoff_returns_saved_delivery(self):
        row = fixture()
        raw = json.dumps(row).encode()
        environ = {"HTTP_AUTHORIZATION": "Bearer secret", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        saved = {"id": 1, "status": "sent", "payload": {"veo_submission_id": row["submission_id"]}, "telegram_message_id": 99}
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true", "VEO_JAUCIAN_SECRET": "secret"}), \
                patch.object(workspace, "validate"), patch.object(workspace, "authoritative", return_value=row), \
                patch.object(workspace, "request", return_value=[saved]), patch.object(workspace, "enqueue_scheduled_action") as enqueue:
            result = workspace.receive(environ, None, lambda _, status, payload: (status, payload))
        self.assertEqual(result[1]["status"], "sent")
        enqueue.assert_not_called()

    def test_delivery_rechecks_settlement_and_uses_temporary_photo(self):
        row = fixture()
        action = {"id": 1, "chat_id": row["telegram_chat_id"], "payload": {"veo_submission_id": row["submission_id"], "veo_order_id": row["order_id"], "veo_tenant_id": row["tenant_id"]}}
        with patch.dict(os.environ, {"VEO_WORKSPACE_ENABLED": "true"}), \
                patch.object(workspace, "authoritative", return_value=row) as read, patch("telegram_sender.telegram_call", return_value={"message_id": 8}) as send:
            workspace.send(action)
            read.assert_called_once()
            self.assertEqual(send.call_args.args[0], "sendPhoto")
            self.assertEqual(send.call_args.args[1]["photo"], row["photo_url"])
        row["paid_minor"] = 9999
        with patch.object(workspace, "allowed_chat_ids", return_value=set(GROUPS)), patch.object(workspace, "chat_enabled", return_value=True):
            with self.assertRaises(ValueError): workspace.validate(row)

    def test_retry_reuses_acknowledged_message_and_archives_old_jobs(self):
        actions = [{"id": 1, "chat_id": 1, "payload": {"veo_submission_id": "x", "_veo_message_id": 99}},
                   {"id": 2, "chat_id": 1, "payload": {"text": "old receipt summary"}}]
        with patch("cloud_store.claim_scheduled_actions", return_value=actions), patch("cloud_store.finish_scheduled_action", return_value=True) as finish, \
                patch("cloud_store._update_claim", return_value=True) as update, patch.object(workspace, "send") as send:
            counts = workspace.deliver({1})
        self.assertEqual(counts, (2, 1, 0))
        send.assert_not_called()
        self.assertEqual(finish.call_args.kwargs["telegram_message_id"], 99)
        self.assertEqual(update.call_args.args[1]["status"], "cancelled")

    def test_guard_failure_does_not_block_due_delivery(self):
        with patch("database_scheduler.ensure_scheduler", side_effect=RuntimeError), patch.object(workspace, "queue_reports", return_value=0), patch.object(workspace, "request"), \
                patch.object(workspace, "allowed_chat_ids", return_value=set(GROUPS)), patch.object(workspace, "deliver", return_value=(1, 1, 0)) as deliver:
            result = workspace.dispatch({}, None, lambda _, status, payload: payload)
        deliver.assert_called_once()
        self.assertEqual(result["sent"], 1)
        self.assertTrue(result["scheduler_guard_failed"])

    def test_uncertain_delivery_is_held_instead_of_duplicated(self):
        failure = RuntimeError("No acknowledgment")
        failure.delivery_uncertain = True
        actions = [{"id": 1, "chat_id": 1, "payload": {"veo_submission_id": "x"}}]
        with patch("cloud_store.claim_scheduled_actions", return_value=actions), patch("cloud_store._update_claim", return_value=True) as update, \
                patch.object(workspace, "send", side_effect=failure), patch("cloud_store.finish_scheduled_action") as finish:
            workspace.deliver({1})
        self.assertEqual(update.call_args.args[1]["status"], "cancelled")
        finish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

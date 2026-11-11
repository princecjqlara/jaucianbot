import io
import json
import os
import unittest
from unittest.mock import patch, MagicMock
import veo_workspace as workspace
import telegram_sender
import veo_mode
from test_veo_workspace import fixture

ENV = {"VEO_WORKSPACE_ENABLED": "true", "VEO_NEW_CLIENTS_ONLY": "true", "VEO_APPROVED_DEALS_ENABLED": "true", "VEO_JAUCIAN_SECRET": "secret"}


def reviewed():
    row = fixture()
    row.update(deal_review_id="00000000-0000-4000-8000-000000000099", reviewed_by="00000000-0000-4000-8000-000000000098",
               review_status="approved", reviewed_at="2026-11-11T15:00:00Z", reviewer_name="Supervisor", page_name="My Page", receipt_kind="done",
               done_amount_minor=6000, closed_amount_minor=4000, tip_minor=500, total_received_minor=10500,
               evidence_photos=[{"slot": slot, "photo_url": "https://example.org/" + slot} for slot in ("stage", "payment", "order")])
    return row


def action(row):
    return {"id": 1, "chat_id": row["telegram_chat_id"], "payload": {"veo_submission_id": row["submission_id"],
        "veo_order_id": row["order_id"], "veo_tenant_id": row["tenant_id"], "veo_receipt_kind": row["receipt_kind"],
        "veo_closed_deal_id": row.get("closed_deal_id"), "veo_deal_review_id": row["deal_review_id"]}}


class ReviewedDealTests(unittest.TestCase):
    def test_mode_accepts_only_pure_intake_or_reviewed_receipts(self):
        with patch.dict(os.environ, ENV):
            self.assertEqual(veo_mode.mode_name(), "new_clients_and_reviewed_deals")
            a = action(reviewed())
            self.assertTrue(veo_mode.reviewed_action(a))
            a["payload"]["veo_report_date"] = "2026-11-11"
            self.assertFalse(veo_mode.reviewed_action(a))

    def test_album_includes_three_photos_and_all_financial_fields_without_archiving_urls(self):
        row = reviewed()
        with patch.dict(os.environ, ENV), patch.object(workspace, "authoritative", return_value=row), patch("veo_directory.verify_identity"), patch.object(telegram_sender, "telegram_call", return_value={"message_id": 1}) as send:
            a = action(row)
            workspace.send(a)
        method, payload = send.call_args.args
        self.assertEqual(method, "sendMediaGroup")
        self.assertEqual(len(payload["media"]), 3)
        caption = payload["media"][0]["caption"]
        for text in ("Client: Client", "Page: My Page", "Approved by: Supervisor", "Deal price: 100.00 PHP", "Close Deal amount: 40.00 PHP", "Done Deal received now: 60.00 PHP", "Tip received now: 5.00 PHP", "Total received incl. tips: 105.00 PHP"):
            self.assertIn(text, caption)
        self.assertLessEqual(len(caption), 1024)
        self.assertNotIn("https:", json.dumps(a))

    def test_rechecks_approval_and_all_photos_before_sending(self):
        for change in ({"review_status": "pending"}, {"reviewed_by": fixture()["seller_id"]}, {"evidence_photos": []}, {"deal_review_id": fixture()["submission_id"]}):
            row = reviewed()
            a = action(row)
            with patch.dict(os.environ, ENV), patch.object(workspace, "authoritative", return_value={**row, **change}), patch("veo_directory.verify_identity"), patch.object(telegram_sender, "telegram_call") as send:
                with self.assertRaises(ValueError):
                    workspace.send(a)
                send.assert_not_called()

    def test_album_acknowledgment_requires_all_three_distinct_message_ids(self):
        for messages, valid in (([{"message_id": 1}, {"message_id": 2}, {"message_id": 3}], True), ([{"message_id": 1}], False), ([{"message_id": 1}] * 3, False), ([{"message_id": True}] * 3, False)):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({"ok": True, "result": messages}).encode()
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test"}), patch.object(telegram_sender, "chat_enabled", return_value=True), patch.object(telegram_sender.urllib.request, "urlopen", return_value=response):
                if valid:
                    self.assertEqual(telegram_sender.telegram_call("sendMediaGroup", {"chat_id": -1, "media": [{}, {}, {}]})["message_ids"], [1, 2, 3])
                else:
                    with self.assertRaises(telegram_sender.TelegramError) as e:
                        telegram_sender.telegram_call("sendMediaGroup", {"chat_id": -1, "media": [{}, {}, {}]})
                    self.assertTrue(e.exception.delivery_uncertain)

    def test_delivery_checkpoints_the_entire_album_and_retry_never_resends(self):
        a = action(reviewed())
        with patch.dict(os.environ, ENV), patch("cloud_store.claim_scheduled_actions", return_value=[a]), patch("cloud_store._update_claim", return_value=True), patch("cloud_store.finish_scheduled_action", return_value=True), patch.object(workspace, "send", return_value={"message_id": 11, "message_ids": [11, 12, 13]}) as send:
            self.assertEqual(workspace.deliver({a["chat_id"]}), (1, 1, 0))
            self.assertEqual(a["payload"]["_veo_album_message_ids"], [11, 12, 13])
            self.assertEqual(workspace.deliver({a["chat_id"]}), (1, 1, 0))
            send.assert_called_once()

    def test_handoff_requires_current_approved_evidence_even_when_snapshot_claims_approval(self):
        row = reviewed()
        raw = json.dumps(row).encode()
        with patch.dict(os.environ, ENV), patch.object(workspace, "validate"), patch.object(workspace, "authoritative", return_value=fixture()), patch.object(workspace, "request") as db:
            result = workspace.receive({"HTTP_AUTHORIZATION": "Bearer secret", "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}, MagicMock(), lambda start, status, body: (status, body))
            self.assertEqual(result[0], 409)
            db.assert_not_called()

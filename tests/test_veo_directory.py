import datetime as dt
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import veo_directory as directory
from daily_automation import GROUPS, SUNO_GROUPS

TENANT = "00000000-0000-4000-8000-000000000001"
CHAT = next(iter(GROUPS))
ENV = {"VEO_WORKSPACE_ENABLED": "true", "VEO_WORKSPACE_TENANT_ID": TENANT,
       "VEO_JAUCIAN_SECRET": "test-key", "ALLOWED_CHAT_IDS": str(CHAT), "AUTOMATION_CHAT_IDS": str(CHAT)}


class DirectoryTests(unittest.TestCase):
    def call(self, query, secret="test-key", method="GET"):
        statuses = []
        body = app.app({"PATH_INFO": "/api/veo/workspace", "REQUEST_METHOD": method,
            "QUERY_STRING": query, "HTTP_AUTHORIZATION": "Bearer " + secret},
            lambda status, headers: statuses.append(status))
        return int(statuses[0].split()[0]), json.loads(b"".join(body))

    def test_rejects_auth_tenant_and_method_before_database_or_telegram(self):
        with patch.dict(os.environ, ENV), patch.object(directory, "request") as read, patch("telegram_sender.telegram_call") as telegram:
            self.assertEqual(self.call("tenant_id=" + TENANT, secret="wrong")[0], 401)
            self.assertEqual(self.call("tenant_id=00000000-0000-4000-8000-000000000002")[0], 403)
            self.assertEqual(self.call("tenant_id=" + TENANT, method="POST")[0], 405)
            self.assertEqual(self.call("tenant_id=invalid")[0], 400)
        read.assert_not_called()
        telegram.assert_not_called()

    def test_only_approved_active_veo_groups_and_safe_delivery_metadata_cross_the_bridge(self):
        def read(path):
            params = parse_qs(urlsplit("https://db/" + path).query)
            if path.startswith("scheduled_actions?") and params.get("dedupe_key", [""])[0].startswith("like.veo-"):
                self.assertEqual(params["dedupe_key"], [f"like.veo-*:{TENANT}:*"])
                self.assertEqual(params["chat_id"], [f"in.({CHAT})"])
                return [{"id": 1, "chat_id": CHAT, "status": "cancelled", "payload": {
                    "text": "private message", "photo_url": "private signed URL", "_veo_sending": True,
                    "veo_order_id": "order", "veo_tenant_id": TENANT, "veo_receipt_kind": "closed", "veo_closed_deal_id": "closed-deal"}}]
            return []
        suno = next(iter(SUNO_GROUPS))
        with patch.dict(os.environ, {**ENV, "ALLOWED_CHAT_IDS": f"{CHAT},{suno}", "AUTOMATION_CHAT_IDS": f"{CHAT},{suno}"}), \
                patch.object(directory, "request", side_effect=read), \
                patch.object(directory, "workspace_connection", return_value={"ok": True}) as reverse:
            status, body = self.call("tenant_id=" + TENANT)
        self.assertEqual(status, 200)
        self.assertEqual([g["chat_id"] for g in body["groups"]], [CHAT])
        self.assertTrue(body["recent_deliveries"][0]["review_required"])
        self.assertEqual(body["recent_deliveries"][0]["veo_receipt_kind"], "closed")
        self.assertEqual(body["recent_deliveries"][0]["veo_closed_deal_id"], "closed-deal")
        self.assertNotIn("private", json.dumps(body))
        reverse.assert_called_once_with(TENANT)

    def test_observed_identities_are_bounded_not_claimed_as_current_and_bots_are_removed(self):
        def read(path):
            if path.startswith("messages?"):
                self.assertIn("limit=1000", path)
                return [{"author_id": 10, "author_name": "Human", "sent_utc": "2026-11-01T00:00:00Z"},
                        {"author_id": 11, "author_name": "Bot"}]
            return [{"payload": {"user_id": 11, "is_bot": True}},
                    {"payload": {"user_id": 12, "user_name": "Former", "status": "left", "expires_at": "2020-01-01T00:00:00Z"}}]
        with patch.object(directory, "request", side_effect=read):
            group = directory.group_directory(CHAT)
        self.assertFalse(group["identity_coverage"]["complete_member_list"])
        self.assertEqual({p["user_id"] for p in group["identities"]}, {10, 12})
        self.assertEqual(next(p for p in group["identities"] if p["user_id"] == 10)["membership"], "unverified")

    def test_identity_check_uses_existing_bot_and_never_sends_a_message(self):
        with patch.dict(os.environ, ENV), patch("telegram_sender.telegram_call", return_value={
                "status": "member", "user": {"id": 42, "is_bot": False, "first_name": "Worker", "username": "worker"}}) as telegram:
            status, body = self.call(f"resource=identity&tenant_id={TENANT}&chat_id={CHAT}&user_id=42")
        self.assertEqual(status, 200)
        self.assertTrue(body["verified"])
        telegram.assert_called_once_with("getChatMember", {"chat_id": CHAT, "user_id": 42}, timeout=8)

    def test_bot_former_unknown_or_mismatched_identity_cannot_be_mapped(self):
        for member in ({"status": "left", "user": {"id": 42, "is_bot": False}},
                       {"status": "member", "user": {"id": 42, "is_bot": True}},
                       {"status": "member", "user": {"id": 43, "is_bot": False}},
                       {"status": "restricted", "is_member": False, "user": {"id": 42, "is_bot": False}}):
            with self.subTest(member=member), patch.dict(os.environ, ENV), patch("telegram_sender.telegram_call", return_value=member):
                self.assertEqual(self.call(f"resource=identity&tenant_id={TENANT}&chat_id={CHAT}&user_id=42")[0], 400)
        with patch.dict(os.environ, ENV), patch("telegram_sender.telegram_call") as telegram:
            self.assertEqual(self.call(f"resource=identity&tenant_id={TENANT}&chat_id={next(iter(SUNO_GROUPS))}&user_id=42")[0], 400)
        telegram.assert_not_called()

    def test_outages_return_generic_error_without_exposing_credentials(self):
        with patch.dict(os.environ, ENV), patch.object(directory, "directory", side_effect=RuntimeError("private credentials")):
            status, body = self.call("tenant_id=" + TENANT)
        self.assertEqual(status, 503)
        self.assertNotIn("private", json.dumps(body))

    def test_membership_network_failure_is_retryable_not_an_uncertain_message_send(self):
        error = RuntimeError("network failure")
        error.delivery_uncertain = True
        with patch.dict(os.environ, ENV), patch("telegram_sender.telegram_call", side_effect=error):
            with self.assertRaises(RuntimeError) as failure:
                directory.verify_identity(CHAT, 42)
        self.assertFalse(getattr(failure.exception, "delivery_uncertain", False))

    def test_reverse_connection_reads_veo_directory_with_tenant_bound_authentication(self):
        data = {"ok": True, "read_only": True, "tenant_id": TENANT, "source": "VEO database",
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "accounts": [
                    {"role": "member", "telegram_user_id": None}, {"role": "owner"}],
                "teams": [{"telegram_chat_id": CHAT}, {"telegram_chat_id": None}],
                "commission_rule": {"qualified_rate": 40, "default_rate": 35, "team_override": False}}
        with patch.dict(os.environ, {**ENV, "VEO_WORKSPACE_URL": "https://veo.example"}), \
                patch.object(directory.urllib.request, "urlopen") as http:
            http.return_value.__enter__.return_value = io.BytesIO(json.dumps(data).encode())
            connection = directory.workspace_connection(TENANT)
            req = http.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(parse_qs(urlsplit(req.full_url).query), {"resource": ["directory"], "tenant_id": [TENANT]})
            self.assertEqual(connection["unmapped_teams"], 1)
            self.assertEqual(connection["unmapped_workers"], 1)
            self.assertEqual(connection["commission_rule"]["default_rate"], 35)
            data["tenant_id"] = "another-tenant"
            http.return_value.__enter__.return_value = io.BytesIO(json.dumps(data).encode())
            with self.assertRaises(ValueError):
                directory.workspace_connection(TENANT)

    def test_reverse_outage_is_visible_without_hiding_available_jaucian_directory(self):
        with patch.dict(os.environ, ENV), patch.object(directory, "request", return_value=[]), \
                patch.object(directory, "workspace_connection", side_effect=RuntimeError("unavailable")):
            data = directory.directory(TENANT)
        self.assertFalse(data["veo_connection"]["ok"])
        self.assertEqual(len(data["groups"]), 1)

    def test_reverse_readiness_counts_team_leaders_as_selling_staff(self):
        data = {"source": "VEO database", "checked_at": "2026-11-11T00:00:00Z", "accounts": [
            {"role": "owner"}, {"role": "team_leader", "telegram_user_id": None},
            {"role": "team_leader", "telegram_user_id": 42}, {"role": "member", "telegram_user_id": None}],
            "teams": [], "commission_rule": {}}
        with patch.object(directory, "workspace_directory", return_value=data), patch.object(directory, "teams", return_value={CHAT}):
            self.assertEqual(directory.workspace_connection(TENANT)["unmapped_workers"], 2)

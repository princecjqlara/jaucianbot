import os
import unittest
from unittest.mock import patch

from suno_store import completed_suno_contacts, suno_supply_status, suno_chatbot_status
from test_suno_store import SETTINGS
from test_wsgi import call_app


MAPPING = {
    **SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
    "SUNO_MIN_DETAILS_PERCENT": "26", "SUNO_CLIENT_IDENTITY_COLUMN": "contacts.psid",
    "SUNO_CLIENT_DATE_COLUMN": "contacts.last_interaction_at",
}


def state(identifier, count, *, stage="qualified"):
    return {"id": identifier, "name": "PRIVATE CLIENT NAME", "status": "stopped",
            "stop_reason": "qualified", "details": {f"field-{i}": "PRIVATE ANSWER" for i in range(count)},
            "missing_details": [f"field-{i}" for i in range(count, 20)],
            "contacts": {"psid": "PRIVATE IDENTITY", "pipeline_stage": stage,
                         "last_interaction_at": "2026-10-17T18:00:00+00:00"},
            "pages": {"name": "Hiraya Studio"}}


class SunoSupplyTests(unittest.TestCase):
    def test_diagnostics_explain_exclusions_and_do_not_change_eligibility(self):
        rows = [state("ready", 6), state("short", 5), state("paid", 7, stage="paid")]
        diagnostics = {}
        with patch.dict(os.environ, MAPPING, clear=True), patch("suno_store.suno_request", return_value=rows):
            result = completed_suno_contacts(diagnostics=diagnostics)
            self.assertEqual(result, completed_suno_contacts())
        self.assertEqual([row["id"] for row in result], ["ready"])
        stats = diagnostics["Hiraya Studio"]
        self.assertEqual(stats["eligibility"], {"eligible": 1, "below_details_threshold": 1, "blocked_outcome": 1})
        self.assertEqual(stats["states_scanned"], 3)
        self.assertEqual(stats["excluded_progress"], {"5/20": 1})
        self.assertEqual(stats["latest_interaction_utc"], "2026-10-17T18:00:00+00:00")
        self.assertNotIn("PRIVATE", str(diagnostics))

    def test_supply_compares_contact_activity_with_brief_activity(self):
        with patch.dict(os.environ, MAPPING, clear=True), patch("suno_store.suno_request", side_effect=[
            [state("ready", 6)],
            [{"id": "ready", "pipeline_stage": "qualified", "last_interaction_at": "2026-10-17T18:00:00+00:00"},
             {"id": "new", "pipeline_stage": "new", "last_interaction_at": "2026-10-17T19:00:00+00:00"}],
        ]):
            result = suno_supply_status()
        self.assertEqual(result["eligible_contacts"], 1)
        self.assertEqual(result["total_contacts"], 2)
        self.assertEqual(result["contact_pipeline_stages"], {"qualified": 1, "new": 1})
        self.assertEqual(result["latest_contact_interaction_utc"], "2026-10-17T19:00:00+00:00")
        self.assertNotIn("PRIVATE", str(result))

    def test_contact_inventory_paginates(self):
        contacts = [{"id": str(i), "pipeline_stage": "new"} for i in range(1000)]
        with patch.dict(os.environ, MAPPING, clear=True), patch("suno_store.suno_request", side_effect=[
            [], contacts, [{"id": "last", "pipeline_stage": "new"}],
        ]) as request:
            result = suno_supply_status()
        self.assertEqual(result["total_contacts"], 1001)
        self.assertIn("offset=1000", request.call_args.args[0])

    def test_route_requires_authentication_and_group_approval(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"}, clear=True), patch(
            "suno_store.suno_supply_status"
        ) as query:
            self.assertEqual(call_app("/api/new-clients/suno-supply")[0], 401)
            self.assertEqual(call_app("/api/new-clients/suno-supply", headers={"Authorization": "Bearer correct"})[0], 404)
        query.assert_not_called()

    def test_route_is_read_only_and_sanitizes_errors(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-1002894511895"}, clear=True), patch(
            "suno_store.suno_supply_status", side_effect=RuntimeError("PRIVATE DETAILS")
        ) as query:
            status, payload = call_app("/api/new-clients/suno-supply", headers={"Authorization": "Bearer correct"})
            self.assertEqual(status, 503)
            self.assertNotIn("PRIVATE", str(payload))
            self.assertEqual(call_app("/api/new-clients/suno-supply", method="POST")[0], 405)
        query.assert_called_once_with()

    def test_route_returns_aggregate_supply(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-1002894511895"}, clear=True), patch(
            "suno_store.suno_supply_status", return_value={"eligible_contacts": 12}
        ):
            self.assertEqual(call_app("/api/new-clients/suno-supply", headers={"Authorization": "Bearer correct"}),
                             (200, {"ok": True, "supply": {"eligible_contacts": 12}}))

    def test_chatbot_audit_omits_details_and_discloses_capped_reads(self):
        with patch.dict(os.environ, MAPPING, clear=True), patch("suno_store.suno_request", side_effect=[
            {"definitions": {table: {"properties": dict.fromkeys(fields.split(","), {})} for table, fields in (
                ("pages", "id,name"), ("chatbot_configs", "page_id,enabled,details_to_collect"),
                ("chatbot_reply_events", "page_id,status,created_at"),
                ("contact_interactions", "page_id,is_from_contact,interaction_at"),
                ("chatbot_contact_states", "page_id,status,last_inbound_at,last_bot_reply_at"),
            )}},
            [{"id": "p1", "name": "Hiraya Studio"}],
            [{"page_id": "p1", "enabled": True,
              "details_to_collect": ["PRIVATE FIELD"], "instructions": "PRIVATE INSTRUCTIONS"}],
            [{"page_id": "p1", "status": "failed", "created_at": "2026-10-17T18:00:00+00:00"}] * 1000,
            [{"page_id": "p1", "is_from_contact": True, "interaction_at": "2026-10-17T18:00:00+00:00"}],
            [{"page_id": "p1", "status": "active", "last_inbound_at": "2026-10-17T18:00:00+00:00",
              "last_bot_reply_at": "2026-10-17T18:00:05+00:00"}],
        ]):
            result = suno_chatbot_status()
        page = result["pages"][0]
        self.assertEqual(page["required_details_count"], 1)
        self.assertEqual(page["reply_events"]["statuses"], {"failed": 1000})
        self.assertTrue(page["reply_events"]["sample_capped"])
        self.assertFalse(page["interactions"]["sample_capped"])
        self.assertEqual(page["states"]["latest_bot_reply_utc"], "2026-10-17T18:00:05+00:00")
        self.assertNotIn("PRIVATE", str(result))

    def test_missing_diagnostic_sources_report_gaps_without_appearing_empty(self):
        with patch.dict(os.environ, MAPPING, clear=True), patch("suno_store.suno_request", return_value={"definitions": {}}):
            result = suno_chatbot_status()
        self.assertEqual(result["pages"], [])
        self.assertIn("chatbot_reply_events", result["coverage_errors"])

    def test_source_errors_are_sanitized_and_do_not_hide_other_sources(self):
        schema = {"definitions": {"pages": {"properties": {"id": {}, "name": {}}}}}
        with patch.dict(os.environ, MAPPING, clear=True), patch("suno_store.suno_request", side_effect=[
            schema, RuntimeError("PRIVATE CREDENTIAL"),
        ]):
            result = suno_chatbot_status()
        self.assertEqual(result["coverage_errors"]["pages"], "diagnostic_read_failed")
        self.assertNotIn("PRIVATE", str(result))

    def test_chatbot_route_skips_inventory_reads(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-1002894511895"}, clear=True), patch(
            "suno_store.suno_chatbot_status", return_value={"pages": []}
        ) as audit, patch("suno_store.suno_supply_status") as supply:
            self.assertEqual(call_app("/api/new-clients/suno-supply", query="chatbot=1",
                                     headers={"Authorization": "Bearer correct"}),
                             (200, {"ok": True, "chatbot": {"pages": []}}))
        audit.assert_called_once_with()
        supply.assert_not_called()

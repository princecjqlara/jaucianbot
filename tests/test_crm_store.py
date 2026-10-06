import unittest
from unittest.mock import patch

import crm_store


class CrmStoreTests(unittest.TestCase):
    def test_completed_detail_contacts_uses_chatbot_completion_state(self):
        rows = [{
            "contact_id": "contact-1",
            "page_id": "page-1",
            "status": "stopped",
            "stop_reason": "details_collected",
            "collected_details": {"name": "Cj"},
            "missing_details": [],
            "last_inbound_at": "2026-09-25T10:00:00+00:00",
            "last_bot_reply_at": "2026-09-25T10:01:00+00:00",
            "contacts": {
                "id": "contact-1",
                "page_id": "page-1",
                "name": " Cj Lara ",
                "psid": "psid-1",
                "last_interaction_at": "2026-09-25T10:00:00+00:00",
                "pipeline_stage": "qualified",
            },
        }]
        with patch.object(crm_store, "crm_request", return_value=rows) as request:
            result = crm_store.completed_detail_contacts("page-1")

        self.assertEqual(result[0]["name"], "Cj Lara")
        self.assertEqual(result[0]["page_id"], "page-1")
        self.assertEqual(result[0]["psid"], "psid-1")
        self.assertEqual(result[0]["pipeline_stage"], "qualified")
        self.assertEqual(result[0]["stop_reason"], "details_collected")
        query = request.call_args.args[0]
        self.assertIn("chatbot_contact_states", query)
        self.assertIn("stop_reason=eq.details_collected", query)
        self.assertIn("page_id=eq.page-1", query)

    def test_completed_detail_contacts_skips_missing_contact_rows(self):
        with patch.object(crm_store, "crm_request", return_value=[
            {"contacts": {"id": "contact-1", "page_id": "other", "name": "Name", "psid": "p"}},
        ]):
            self.assertEqual(crm_store.completed_detail_contacts("page-1"), [])

    def test_completed_detail_contacts_requires_collected_details(self):
        contact = {
            "id": "contact-1", "page_id": "page-1", "name": "Client", "psid": "p",
        }
        rows = [
            {"contacts": contact, "collected_details": {}, "missing_details": []},
        ]
        with patch.object(crm_store, "crm_request", return_value=rows):
            self.assertEqual(crm_store.completed_detail_contacts("page-1"), [])

    def test_completed_outcome_wins_over_stale_missing_details(self):
        row = {
            "contact_id": "contact-1",
            "page_id": "page-1",
            "status": "stopped",
            "stop_reason": "details_collected",
            "collected_details": {"name": "Client", "business": "Studio"},
            "missing_details": ["outdated synonymous prompt"],
            "contacts": {
                "id": "contact-1", "page_id": "page-1", "name": "Client", "psid": "p",
            },
        }
        with patch.object(crm_store, "crm_request", return_value=[row]):
            result = crm_store.completed_detail_contacts("page-1")
        self.assertEqual([contact["id"] for contact in result], ["contact-1"])
        self.assertEqual(result[0]["missing_details"], ["outdated synonymous prompt"])

    def test_contact_identity_map_reads_page_scoped_psids(self):
        rows = [{"id": "contact-1", "page_id": "page-1", "psid": "psid-1"}]
        with patch.object(crm_store, "crm_request", return_value=rows) as request:
            result = crm_store.contact_identity_map({"contact-1"})
        self.assertEqual(result, {"contact-1": ("page-1", "psid-1")})
        self.assertIn("contacts?", request.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

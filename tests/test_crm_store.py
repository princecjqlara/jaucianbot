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


if __name__ == "__main__":
    unittest.main()

import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from suno_store import SOURCE_ID, completed_suno_contacts, details_progress, suno_assignment_matches_project, suno_configured, suno_request


SETTINGS = {
    "SUNO_ASSIGNMENTS_ENABLED": "true",
    "SUNO_SUPABASE_URL": "https://cxgynadprukyeuqbchbs.supabase.co",
    "SUNO_SUPABASE_SERVICE_ROLE_KEY": "test-suno-key",
    "SUNO_CLIENTS_TABLE": "clients",
    "SUNO_CLIENT_ID_COLUMN": "id",
    "SUNO_CLIENT_NAME_COLUMN": "name",
    "SUNO_CLIENT_DETAILS_COLUMN": "details",
    "SUNO_COMPLETION_COLUMN": "status",
    "SUNO_COMPLETION_VALUE": "complete",
}


def client(client_id="1", **values):
    return {"id": client_id, "name": " Client ", "details": {"song": "Birthday"},
            "status": "complete", **values}


class SunoStoreTests(unittest.TestCase):
    def test_assignment_project_namespace_rejects_veo_and_old_suno_database(self):
        with patch.dict("os.environ", SETTINGS, clear=True):
            for page, expected in [
                (SOURCE_ID, True), (SOURCE_ID + ":hiraya", True),
                (SOURCE_ID + "-other:hiraya", False),
                ("suno:pnhzpeyzpwsmwcuafgpw", False), ("veo-page", False), (None, False),
            ]:
                self.assertEqual(suno_assignment_matches_project({"payload": {"new_client_contact_page_id": page}}), expected)

    def test_reader_uses_verified_mapping_and_rejects_partial_or_empty_details(self):
        rows = [client(), client("2", status="collecting"), client("3", details={}),
                client("4", details=[]), client("5", name=""), client(None)]
        with patch.dict("os.environ", SETTINGS, clear=True), patch("suno_store.suno_request", return_value=rows) as request:
            result = completed_suno_contacts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Client")
        self.assertEqual((result[0]["page_id"], result[0]["psid"]), (SOURCE_ID, "1"))
        self.assertIsNone(result[0]["page_name"])
        query = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(query["status"], ["eq.complete"])
        self.assertEqual(query["order"], ["id.asc"])

    def test_missing_mapping_stays_disabled_and_never_queries_a_guessed_table(self):
        with patch.dict("os.environ", {"SUNO_SUPABASE_URL": "https://suno.example",
                                       "SUNO_SUPABASE_SERVICE_ROLE_KEY": "test"}, clear=True), patch("suno_store.suno_request") as request:
            self.assertFalse(suno_configured())
            with self.assertRaisesRegex(RuntimeError, "mapping are not configured"):
                completed_suno_contacts()
        request.assert_not_called()

    def test_boolean_completion_and_stable_customer_identity(self):
        settings = {**SETTINGS, "SUNO_COMPLETION_COLUMN": "ready", "SUNO_COMPLETION_VALUE": "true",
                    "SUNO_CLIENT_IDENTITY_COLUMN": "customer_id", "SUNO_CLIENT_DATE_COLUMN": "created_at"}
        row = client(ready=True, customer_id="customer-7", created_at="2026-10-08T00:00:00Z")
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=[row]):
            self.assertTrue(suno_configured())
            result = completed_suno_contacts()
        self.assertEqual(result[0]["psid"], "customer-7")
        self.assertEqual(result[0]["last_interaction_at"], row["created_at"])

    def test_verified_project_guard_rejects_a_different_database(self):
        settings = {**SETTINGS, "SUNO_EXPECTED_PROJECT_REF": "cxgynadprukyeuqbchbs",
                    "SUNO_SUPABASE_URL": "https://wrong-project.supabase.co"}
        with patch.dict("os.environ", settings, clear=True), patch("urllib.request.urlopen") as opened:
            self.assertFalse(suno_configured())
            with self.assertRaisesRegex(RuntimeError, "verified project"):
                suno_request("pages?select=name")
        opened.assert_not_called()

    def test_contact_identity_is_scoped_to_database_and_actual_page(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states"}
        rows = [client(page_id="hiraya", missing_details=[]),
                client("partial", page_id="hiraya", missing_details=["song occasion"])]
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=rows):
            result = completed_suno_contacts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["page_id"], "suno:cxgynadprukyeuqbchbs:hiraya")

    def test_verified_chatbot_state_relation_mapping_reads_contact_identity_and_name(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_CLIENT_ID_COLUMN": "contact_id", "SUNO_CLIENT_NAME_COLUMN": "contacts.name",
                    "SUNO_CLIENT_DETAILS_COLUMN": "collected_details", "SUNO_COMPLETION_COLUMN": "stop_reason",
                    "SUNO_COMPLETION_VALUE": "details_collected", "SUNO_CLIENT_IDENTITY_COLUMN": "contacts.psid",
                    "SUNO_CLIENT_DATE_COLUMN": "contacts.last_interaction_at"}
        row = {"contact_id": "contact-7", "collected_details": {"song": "Birthday"},
               "stop_reason": "details_collected",
               "contacts": {"name": "Client", "psid": "customer-7", "last_interaction_at": "2026-10-08T00:00:00Z"},
               "pages": {"name": "Maico Foods"}}
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=[row]) as request:
            result = completed_suno_contacts()
        self.assertEqual((result[0]["id"], result[0]["name"], result[0]["psid"]),
                         ("contact-7", "Client", "customer-7"))
        self.assertEqual(result[0]["page_name"], "Maico Foods")
        query = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertIn("contacts!inner(last_interaction_at,name,psid)", query["select"][0])
        self.assertIn("pages!inner(name)", query["select"][0])

    def test_reader_paginates_past_one_thousand_completed_clients(self):
        with patch.dict("os.environ", SETTINGS, clear=True), patch("suno_store.suno_request", side_effect=[
            [client(str(i)) for i in range(1000)], [client("1000")],
        ]) as request:
            self.assertEqual(len(completed_suno_contacts()), 1001)
        query = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(query["offset"], ["1000"])

    def test_percentage_handoff_is_explicit_and_preserves_missing_fields(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_CLIENT_ID_COLUMN": "contact_id", "SUNO_CLIENT_NAME_COLUMN": "contacts.name",
                    "SUNO_CLIENT_DETAILS_COLUMN": "collected_details", "SUNO_COMPLETION_COLUMN": "stop_reason",
                    "SUNO_COMPLETION_VALUE": "details_collected", "SUNO_CLIENT_IDENTITY_COLUMN": "contacts.psid"}
        row = {"contact_id": "qualified", "status": "active", "stop_reason": None, "page_id": "hiraya",
               "collected_details": {"Customer name": "Client", "Song purpose": "Business jingle"},
               "missing_details": ["Agreed package"],
               "contacts": {"name": "Client", "psid": "123", "pipeline_stage": "qualified"},
               "pages": {"name": "Hiraya Studio"}}
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=[row]):
            self.assertEqual(completed_suno_contacts(), [])
        with patch.dict("os.environ", {**settings, "SUNO_MIN_DETAILS_PERCENT": "26"}, clear=True), patch(
            "suno_store.suno_request", return_value=[row]
        ) as request:
            result = completed_suno_contacts()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["details_complete"])
        self.assertIsNone(result[0]["stop_reason"])
        self.assertEqual(result[0]["missing_details"], ["Agreed package"])
        self.assertEqual(result[0]["page_name"], "Hiraya Studio")
        self.assertEqual(result[0]["details_percent"], 66.7)
        query = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertNotIn("stop_reason", query)
        self.assertIn("pipeline_stage", query["select"][0])

    def test_percentage_mode_accepts_collecting_but_excludes_refused_opted_out_and_low_progress(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
                    "SUNO_MIN_DETAILS_PERCENT": "26"}
        def state(i, **changes):
            return {"id": str(i), "name": "Client", "details": {"Customer name": "Client", "song": "Birthday"},
                    "status": "active", "stop_reason": None, "missing_details": ["package", "mood", "language", "duration"],
                    "contacts": {"pipeline_stage": "qualified"}, **changes}
        rows = [state(1), state(2, contacts={"pipeline_stage": "collecting_details"}),
                state(3, status="stopped", stop_reason="refusal"),
                state(4, status="stopped", stop_reason="opt_out"),
                state(5, details={"Customer name": "Client"}, missing_details=[f"missing-{i}" for i in range(19)]),
                state(6, details={"Customer name": "Client", "song": " "}, missing_details=[f"missing-{i}" for i in range(18)]),
                state(7, contacts={"pipeline_stage": "opted_out"})]
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=rows):
            self.assertEqual([row["id"] for row in completed_suno_contacts()], ["1", "2"])

    def test_26_percent_boundary_uses_exact_counts_not_rounded_percentage(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
                    "SUNO_MIN_DETAILS_PERCENT": "26"}
        def state(i, collected, total):
            return {"id": str(i), "name": "Client", "status": "active", "stop_reason": None,
                    "details": {f"field-{n}": "answer" for n in range(collected)},
                    "missing_details": [f"field-{n}" for n in range(collected, total)],
                    "contacts": {"pipeline_stage": "collecting_details"}}
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=[
            state(1, 5, 20), state(2, 6, 20), state(3, 13, 50), state(4, 13, 51),
        ]):
            self.assertEqual([row["id"] for row in completed_suno_contacts()], ["2", "3"])

    def test_stopped_handoffs_meeting_threshold_keep_missing_fields(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
                    "SUNO_MIN_DETAILS_PERCENT": "26"}
        rows = [
            {"id": "lopez", "name": "Xī Nà Lopez", "status": "stopped", "stop_reason": "details_collected",
             "details": {f"field-{n}": "answer" for n in range(7)},
             "missing_details": [f"field-{n}" for n in range(7, 20)],
             "contacts": {"pipeline_stage": "qualified"}},
            {"id": "qualified", "name": "Qualified client", "status": "stopped", "stop_reason": "qualified",
             "details": {f"field-{n}": "answer" for n in range(6)},
             "missing_details": [f"field-{n}" for n in range(6, 20)],
             "contacts": {"pipeline_stage": "qualified"}},
        ]
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=rows):
            contacts = completed_suno_contacts()
        self.assertEqual([c["id"] for c in contacts], ["lopez", "qualified"])
        self.assertEqual(contacts[0]["details_percent"], 35)
        self.assertFalse(contacts[0]["details_complete"])
        self.assertEqual(len(contacts[0]["missing_details"]), 13)
        self.assertEqual(contacts[0]["stop_reason"], "details_collected")

    def test_partial_handoff_requires_threshold_and_excludes_refused_clients(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
                    "SUNO_MIN_DETAILS_PERCENT": "26"}
        rows = []
        for index, (reason, stage, count) in enumerate([
            ("qualified", "qualified", 5), ("details_collected", "qualified", 5),
            ("refusal", "qualified", 7), ("opt_out", "qualified", 7),
            ("qualified", "not_qualified", 7), ("details_collected", "opted_out", 7),
        ]):
            rows.append({"id": str(index), "name": "Client", "status": "stopped", "stop_reason": reason,
                         "details": {f"field-{n}": "answer" for n in range(count)},
                         "missing_details": [f"field-{n}" for n in range(count, 20)],
                         "contacts": {"pipeline_stage": stage}})
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=rows):
            self.assertEqual(completed_suno_contacts(), [])

    def test_26_percent_rule_survives_changes_in_chatbot_handoff_labels(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
                    "SUNO_MIN_DETAILS_PERCENT": "26"}
        rows = []
        for index, (status, reason) in enumerate([
            ("active", None), ("stopped", "details_collected"), ("stopped", "qualified"),
            ("paused", "handoff_requested"), ("completed", "ready_for_editor"),
        ]):
            rows.append({"id": str(index), "name": "Client", "status": status, "stop_reason": reason,
                         "details": {f"field-{n}": "answer" for n in range(6)},
                         "missing_details": [f"field-{n}" for n in range(6, 20)],
                         "contacts": {"pipeline_stage": "qualified"}})
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=rows):
            result = completed_suno_contacts()
        self.assertEqual([r["id"] for r in result], [str(i) for i in range(5)])
        self.assertTrue(all(r["details_percent"] == 30 and not r["details_complete"] for r in result))

    def test_explicit_refusal_cannot_be_overridden_by_complete_details(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected",
                    "SUNO_MIN_DETAILS_PERCENT": "26"}
        row = {"id": "excluded", "name": "Client", "status": "stopped", "stop_reason": "details_collected",
               "details": {"name": "Client"}, "missing_details": [],
               "contacts": {"pipeline_stage": "opted_out"}}
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=[row]):
            self.assertEqual(completed_suno_contacts(), [])

    def test_stopped_partial_handoffs_do_not_change_default_complete_only_mode(self):
        settings = {**SETTINGS, "SUNO_CLIENTS_TABLE": "chatbot_contact_states",
                    "SUNO_COMPLETION_COLUMN": "stop_reason", "SUNO_COMPLETION_VALUE": "details_collected"}
        rows = [{"id": "lopez", "name": "Xī Nà Lopez", "status": "stopped",
                 "stop_reason": "details_collected", "details": {"name": "Xī Nà Lopez"},
                 "missing_details": ["package", "language", "duration"], "contacts": {"pipeline_stage": "qualified"}}]
        with patch.dict("os.environ", settings, clear=True), patch("suno_store.suno_request", return_value=rows):
            self.assertEqual(completed_suno_contacts(), [])

    def test_progress_dedupes_names_and_does_not_count_blank_or_still_missing_values(self):
        self.assertEqual(details_progress({"Customer name": "A", "LANGUAGE": "Tagalog", " mood ": " ",
                                           "Package": "old", "language": "Tagalog"}, [" package ", "Mood", "Duration"]), (2, 5))

    def test_invalid_threshold_fails_before_query(self):
        for value in ("0", "101", "invalid"):
            with patch.dict("os.environ", {**SETTINGS, "SUNO_MIN_DETAILS_PERCENT": value}, clear=True), patch(
                "suno_store.suno_request"
            ) as query:
                with self.assertRaisesRegex(RuntimeError, "Invalid Suno details percentage"):
                    completed_suno_contacts()
            query.assert_not_called()

    def test_invalid_mapping_is_rejected_before_request(self):
        with patch.dict("os.environ", {**SETTINGS, "SUNO_CLIENTS_TABLE": "clients?select=*"}, clear=True), patch("suno_store.suno_request") as request:
            with self.assertRaisesRegex(RuntimeError, "Invalid Suno"):
                completed_suno_contacts()
        request.assert_not_called()

    def test_http_error_does_not_expose_credentials_or_response_body(self):
        error = urllib.error.HTTPError("https://suno.example", 403, "Private response", {}, None)
        self.addCleanup(error.close)
        with patch.dict("os.environ", SETTINGS, clear=True), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^Suno Data API HTTP 403$"):
                suno_request("clients")


if __name__ == "__main__":
    unittest.main()

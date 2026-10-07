import datetime as dt
import unittest

from postgres_archive import compile_request, _normalize


class PostgresArchiveTests(unittest.TestCase):
    def test_query_keeps_both_date_bounds_and_parameterizes_user_text(self):
        statement, values, shape = compile_request(
            "messages?select=message_id,text&chat_id=eq.-100123&sent_utc=gte.2026-10-01T00:00:00Z"
            "&sent_utc=lt.2026-10-02T00:00:00Z&text=ilike.*working*&order=sent_utc.asc,message_id.asc&limit=501&offset=501"
        )
        query = statement.as_string()
        self.assertIn('"a"."sent_utc" >= %s', query)
        self.assertIn('"a"."sent_utc" < %s', query)
        self.assertNotIn("working", query)
        self.assertEqual(values[-3:], ["%working%", 501, 501])
        self.assertIsInstance(values[1], dt.datetime)
        self.assertEqual(shape, "rows")

    def test_json_projection_and_revision_guard_are_preserved(self):
        statement, values, _ = compile_request(
            "scheduled_actions?select=id&status=in.(pending,processing,failed)"
            "&id=eq.1&payload-%3E%3Enew_client_acknowledged_at=is.null&updated_at=eq.2026-10-07T00:00:00Z",
            {"payload": {"text": "Client"}, "status": "cancelled"}, method="PATCH", prefer="return=representation",
        )
        query = statement.as_string()
        self.assertIn('"a"."payload" ->> \'new_client_acknowledged_at\' IS NULL', query)
        self.assertIn('"a"."updated_at" = %s', query)
        self.assertIn('RETURNING "a"."id"', query)
        self.assertNotIn("Client", query)

    def test_unknown_tables_rpcs_and_unfiltered_updates_are_rejected(self):
        for path, payload, method in (
            ("auth.users", None, "GET"),
            ("rpc/arbitrary_function", {}, "POST"),
            ("scheduled_actions", {"status": "cancelled"}, "PATCH"),
            ("scheduled_actions?order=id.desc;drop%20table%20messages", None, "GET"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                compile_request(path, payload, method=method)

    def test_named_rpc_arguments_have_explicit_types(self):
        query, values, shape = compile_request("rpc/insights_status", {"p_allowed_ids": [-100123]})
        self.assertIn('"p_allowed_ids" => %s::bigint[]', query.as_string())
        self.assertEqual(values, [[-100123]])
        self.assertEqual(shape, "rows")

    def test_poll_history_join_preserves_relationship_and_bounds(self):
        query, values, _ = compile_request(
            "daily_poll_answers?select=user_id,user_name,active,daily_polls!inner(work_date,chat_id)"
            "&daily_polls.chat_id=eq.-100123&daily_polls.work_date=gte.2026-10-01"
            "&daily_polls.work_date=lte.2026-10-07&order=poll_id.asc,user_id.asc"
        )
        self.assertIn("JOIN public.daily_polls", query.as_string())
        self.assertEqual(values[:3], [-100123, dt.date(2026, 10, 1), dt.date(2026, 10, 7)])

    def test_conflicting_marker_is_ignored_and_only_returns_id(self):
        query, values, shape = compile_request(
            "scheduled_actions?on_conflict=dedupe_key&select=id",
            {"chat_id": -100123, "payload": {}, "status": "cancelled", "dedupe_key": "marker"},
            prefer="resolution=ignore-duplicates,return=representation",
        )
        self.assertIn('ON CONFLICT ("dedupe_key") DO NOTHING', query.as_string())
        self.assertIn('RETURNING "a"."id"', query.as_string())
        self.assertNotIn("marker", query.as_string())
        self.assertEqual(shape, "rows")

    def test_dates_match_http_response_shape(self):
        value = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)
        self.assertEqual(_normalize([{"sent_at": value}]), [{"sent_at": value.isoformat()}])


if __name__ == "__main__":
    unittest.main()

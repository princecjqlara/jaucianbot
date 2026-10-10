import datetime as dt
import json
import unittest
from unittest.mock import MagicMock, patch

import database_scheduler as scheduler
from test_wsgi import call_app


class SchedulerRouteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"})
        env.start()
        self.addCleanup(env.stop)

    def test_read_and_repair_require_authentication(self):
        with patch("database_scheduler.scheduler_status") as read, patch("database_scheduler.configure_scheduler") as repair:
            for method in ("GET", "POST"):
                status, body = call_app("/api/scheduler", method=method)
                self.assertEqual(status, 401)
            read.assert_not_called()
            repair.assert_not_called()

    def test_read_does_not_install_or_run_dispatcher(self):
        with patch("database_scheduler.scheduler_status", return_value={"configured": False}) as read, \
                patch("database_scheduler.configure_scheduler") as repair, patch("app.deliver_due_actions") as deliver:
            status, body = call_app("/api/scheduler", headers={"Authorization": "Bearer correct"})
        self.assertEqual(status, 200)
        self.assertFalse(body["scheduler"]["configured"])
        repair.assert_not_called()
        deliver.assert_not_called()

    def test_repair_uses_approved_groups_without_immediate_delivery(self):
        with patch("database_scheduler.configure_scheduler", return_value={"configured": True}) as repair, \
                patch("app.dispatch_route") as dispatch, patch("app.deliver_due_actions") as deliver:
            status, body = call_app("/api/scheduler", method="POST", headers={"Authorization": "Bearer correct"})
        self.assertEqual(status, 200)
        repair.assert_called_once_with({-100123}, verify_recovery=False)
        dispatch.assert_not_called()
        deliver.assert_not_called()

    def test_unexpected_failure_does_not_expose_credentials(self):
        with patch("database_scheduler.scheduler_status", side_effect=RuntimeError("postgres://password@host")):
            status, body = call_app("/api/scheduler", headers={"Authorization": "Bearer correct"})
        self.assertEqual(status, 503)
        self.assertNotIn("password", json.dumps(body))

    def test_delete_cannot_remove_database_jobs(self):
        status, body = call_app("/api/scheduler", method="DELETE", headers={"Authorization": "Bearer correct"})
        self.assertEqual(status, 405)

    def test_recovery_verification_is_explicit_and_never_dispatches(self):
        with patch("database_scheduler.configure_scheduler", return_value={"recovery_verified": True}) as repair, \
                patch("app.deliver_due_actions") as deliver:
            status, body = call_app("/api/scheduler", method="POST", body=b'{"verify_recovery":true}',
                                    headers={"Authorization": "Bearer correct"})
        self.assertEqual(status, 200)
        repair.assert_called_once_with({-100123}, verify_recovery=True)
        deliver.assert_not_called()

    def test_invalid_verification_flag_cannot_change_jobs(self):
        with patch("database_scheduler.configure_scheduler") as repair:
            status, body = call_app("/api/scheduler", method="POST", body=b'{"verify_recovery":"yes"}',
                                    headers={"Authorization": "Bearer correct"})
        self.assertEqual(status, 400)
        repair.assert_not_called()


class SchedulerSnapshotTests(unittest.TestCase):
    def connection(self, results):
        conn = MagicMock()
        cursors = []
        for rows, is_many in results:
            cursor = MagicMock()
            (cursor.fetchall if is_many else cursor.fetchone).return_value = rows
            cursors.append(cursor)
        conn.execute.side_effect = cursors
        return conn

    def test_installed_extensions_without_a_job_are_not_configured(self):
        conn = self.connection([([("pg_cron", "1.6"), ("pg_net", "0.14")], True), (None, False)])
        self.assertFalse(scheduler._snapshot(conn)["configured"])

    def test_inactive_job_is_not_healthy(self):
        conn = self.connection([([("pg_cron", "1.6"), ("pg_net", "0.14")], True),
                                ((7, "* * * * *", False), False), (None, False), (None, False), (None, False)])
        self.assertFalse(scheduler._snapshot(conn)["configured"])

    def test_status_uses_only_its_request_ids_and_excludes_unrelated_response_fields(self):
        conn = self.connection([([("pg_cron", "1.6"), ("pg_net", "0.14")], True),
                                ((7, "* * * * *", True), False), (None, False), (("succeeded", "start", "end"), False),
                                (({"request_id": 11, "previous_request_id": 10, "requested_at": "now"},), False),
                                ((11, 200, False, False, json.dumps({"ok": True, "sent": 2, "token": "private"}), "now"), False)])
        result = scheduler._snapshot(conn)
        self.assertTrue(result["configured"])
        self.assertEqual(result["last_delivery"]["result"], {"ok": True, "sent": 2})
        self.assertEqual(conn.execute.call_args.args[1], ([11, 10],))
        self.assertNotIn("private", json.dumps(result))

    def test_no_approved_groups_cannot_create_a_job(self):
        with patch.object(scheduler, "_connect") as connect, self.assertRaises(scheduler.SchedulerError):
            scheduler.configure_scheduler(set())
        connect.assert_not_called()


class SchedulerHealthTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 11, 3, tzinfo=dt.timezone.utc)

    def healthy_state(self):
        return {"configured": True, "watchdog": {"active": True, "schedule": "*/2 * * * *"},
                "last_watchdog_run": {"status": "succeeded", "started_at": self.NOW.isoformat()},
                "last_requested_at": self.NOW.isoformat(),
                "last_delivery": {"received_at": (self.NOW - dt.timedelta(seconds=65)).isoformat(),
                                  "http_status": 200, "timed_out": False, "transport_error": False,
                                  "result": {"ok": True}},
                "queue": {"overdue_over_five_minutes": 0, "failed": 0, "stale_claims": 0}}

    def test_latest_request_in_flight_keeps_recent_success_healthy(self):
        self.assertEqual(scheduler.health_reasons(self.healthy_state(), self.NOW), [])

    def test_installed_jobs_with_old_success_do_not_hide_stopped_dispatcher(self):
        state = self.healthy_state()
        state["last_requested_at"] = (self.NOW - dt.timedelta(hours=1)).isoformat()
        state["last_delivery"]["received_at"] = state["last_requested_at"]
        reasons = scheduler.health_reasons(state, self.NOW)
        self.assertIn("dispatcher_missing_runs", reasons)
        self.assertIn("dispatcher_response_missing", reasons)

    def test_successful_http_with_application_failure_is_unhealthy(self):
        state = self.healthy_state()
        state["last_delivery"]["result"] = {"ok": False, "queue_errors": ["new_client"]}
        self.assertIn("dispatcher_delivery_error", scheduler.health_reasons(state, self.NOW))

    def test_failed_watchdog_does_not_hide_behind_healthy_dispatcher(self):
        state = self.healthy_state()
        state["last_watchdog_run"]["status"] = "failed"
        self.assertIn("watchdog_error", scheduler.health_reasons(state, self.NOW))
        state["health_reasons"] = ["watchdog_error"]
        with patch.object(scheduler, "scheduler_status", return_value=state), \
                patch.object(scheduler, "configure_scheduler") as repair:
            scheduler.ensure_scheduler({-100123})
        repair.assert_called_once_with({-100123})

    def test_backlog_failed_delivery_and_stale_claim_are_all_visible(self):
        state = self.healthy_state()
        state["queue"] = {"overdue_over_five_minutes": 1, "failed": 1, "stale_claims": 1}
        self.assertEqual(set(scheduler.health_reasons(state, self.NOW)),
                         {"delivery_backlog", "failed_deliveries", "stale_delivery_claims"})

    def test_missing_watchdog_is_repaired_using_the_existing_named_jobs(self):
        state = self.healthy_state()
        state["watchdog"] = None
        with patch.object(scheduler, "scheduler_status", return_value=state), \
                patch.object(scheduler, "configure_scheduler") as repair:
            scheduler.ensure_scheduler({-100123})
        repair.assert_called_once_with({-100123})

    def test_healthy_scheduler_does_not_get_reconfigured_on_every_guard_run(self):
        with patch.object(scheduler, "scheduler_status", return_value=self.healthy_state()), \
                patch.object(scheduler, "configure_scheduler") as repair:
            scheduler.ensure_scheduler({-100123})
        repair.assert_not_called()

    def test_public_health_fails_without_exposing_private_scheduler_state(self):
        with patch.dict("os.environ", {"SUPABASE_DB_URL": "configured", "ALLOWED_CHAT_IDS": "-100123"}), \
                patch("app.archive_status", return_value=[]), \
                patch.object(scheduler, "scheduler_status", return_value={"healthy": False, "private": "secret"}):
            status, body = call_app("/api/health")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"ok": False, "error": "automation_unhealthy"})

    def test_scheduler_guard_failure_does_not_block_due_deliveries(self):
        with patch.dict("os.environ", {"SUPABASE_DB_URL": "configured", "ALLOWED_CHAT_IDS": "-100123",
                                       "CRM_SUPABASE_SERVICE_ROLE_KEY": ""}), \
                patch("app.claim_automation_slot", return_value=True), \
                patch.object(scheduler, "ensure_scheduler", side_effect=scheduler.SchedulerError("unavailable")), \
                patch("app.referral_incentives.queue_automation", return_value=0), \
                patch("app.run_due_daily_automation", return_value=0), \
                patch("app.deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["queue_errors"], ["scheduler_guard"])
        self.assertEqual(body["sent"], 1)
        deliver.assert_called_once()

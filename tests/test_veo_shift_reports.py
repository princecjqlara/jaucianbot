import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import veo_shift_reports as shifts
import veo_workspace as workspace
from daily_automation import GROUPS

CHAT = next(iter(GROUPS))
TENANT = "00000000-0000-4000-8000-000000000001"
NOW = dt.datetime(2026, 11, 6, 8, 0, tzinfo=dt.timezone.utc)


def report():
    return {"tenant_id": TENANT, "work_date": "2026-11-06", "report_version": 2, "generated_at": NOW.isoformat(), "teams": [{"id": "team", "name": "VEO Team", "chat_id": CHAT, "thread_id": 5,
        "access_signature": "allowed", "capacity": {"closed": 2, "done": 1, "capacity": 3, "average_worker_capacity": 1.5, "quota": {"closed": 3, "done": 3}, "tomorrow": {"active": 1, "capacity": 1, "unknown": 1}},
        "workers": [{"id": "worker", "name": "Worker", "salary": {"closed": 1, "done": 1, "target": 1, "totals": [{"commission_minor": 40000, "gross_minor": 100000, "currency": "PHP"}]},
            "pages": [{"name": "VEO Page"}], "clients": [{"name": "Client", "page_name": "VEO Page", "waiting_payment": True, "work_status": "ready_waiting_payment", "overdue": False}]}]}]}


class ShiftTests(unittest.TestCase):
    def test_three_philippine_slots_and_midnight_closeout_date(self):
        zone = ZoneInfo("Asia/Manila")
        for hour, minute, expected, day in [(0, 9, None, None), (0, 10, "24", "2026-11-05"), (7, 59, "24", "2026-11-05"), (8, 0, "08", "2026-11-06"), (16, 0, "16", "2026-11-06"), (23, 59, "16", "2026-11-06")]:
            result = shifts.period(dt.datetime(2026,11,6,hour,minute,tzinfo=zone))
            if expected is None:
                self.assertIsNone(result)
            else:
                self.assertEqual((str(result[0]), result[1]), (day, expected))

    def test_report_lists_clients_waiting_balances_and_shared_targets_without_salary_or_ads(self):
        data = report()
        text = "\n".join(shifts.lines(data, data["teams"][0], "16"))
        self.assertIn("CLOSED: 2/3", text)
        self.assertIn("Client / VEO Page: finished", text)
        self.assertIn("Staff projects overdue: 0", text)
        self.assertNotIn("400.00", text)
        self.assertNotIn("commission_minor", text)
        self.assertNotIn("PHP", text)

    def test_parts_fit_telegram_utf16_limits_including_emoji_and_long_client_names(self):
        parts = shifts.split_lines(["😀"*3000, "Second client", "x"*8000])
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(p.encode("utf-16-le"))//2 <= 3500 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), "😀"*3000 + "Second client" + "x"*8000)

    def test_retry_uses_the_original_manifest_so_new_clients_do_not_add_duplicate_snapshot_parts(self):
        stored = {"parts": ["original one", "original two"], "signature": "original-access"}
        with patch.object(shifts, "request", return_value=[{"payload": stored}]):
            result = shifts.manifest(report(), report()["teams"][0], NOW.date(), "16", NOW)
        self.assertEqual(result, stored)

    def test_permission_changes_block_a_queued_snapshot_before_sending(self):
        data = report()
        action = {"chat_id": CHAT, "payload": {"veo_report_date": "2026-11-06", "veo_team_id": "team", "veo_report_signature": "old-access", "veo_report_text": "old private clients", "veo_report_part": 0, "veo_report_parts": 1}}
        with patch.object(workspace, "reporting", return_value=data), patch("telegram_sender.telegram_call") as send:
            with self.assertRaises(shifts.ReportScopeChanged):
                shifts.send(action)
        send.assert_not_called()

    def test_successful_part_uses_existing_bot_and_current_mapped_topic(self):
        data = report()
        action = {"chat_id": CHAT, "payload": {"veo_report_date": "2026-11-06", "veo_team_id": "team", "veo_report_signature": "allowed", "veo_report_text": "snapshot", "veo_report_part": 1, "veo_report_parts": 2}}
        with patch.object(workspace, "reporting", return_value=data), patch("telegram_sender.telegram_call", return_value={"message_id": 12}) as send:
            shifts.send(action)
        self.assertEqual(send.call_args.args[0], "sendMessage")
        self.assertEqual(send.call_args.args[1]["message_thread_id"], 5)
        self.assertIn("Part 2/2", send.call_args.args[1]["text"])

    def test_poll_registration_retry_reuses_acknowledged_message_without_another_poll(self):
        action = {"id": 1, "chat_id": CHAT, "payload": {"veo_availability_date": "2026-11-07", "_veo_message_id": 123, "_veo_poll_id": "poll"}}
        with patch("cloud_store.claim_scheduled_actions", return_value=[action]), patch("cloud_store.finish_scheduled_action", return_value=True), \
                patch.object(shifts, "register_availability") as register, patch.object(shifts, "send_availability") as send:
            self.assertEqual(workspace.deliver({CHAT}), (1,1,0))
        register.assert_called_once()
        send.assert_not_called()

    def test_scope_change_is_cancelled_without_a_blind_retry(self):
        action = {"id": 1, "chat_id": CHAT, "payload": {"veo_report_date": "2026-11-06", "veo_report_slot": "16"}}
        with patch("cloud_store.claim_scheduled_actions", return_value=[action]), patch("cloud_store._update_claim", return_value=True) as update, \
                patch.object(workspace, "send_report", side_effect=shifts.ReportScopeChanged()), patch("cloud_store.finish_scheduled_action") as finish:
            workspace.deliver({CHAT})
        self.assertTrue(any(c.args[1].get("status") == "cancelled" for c in update.call_args_list))
        finish.assert_not_called()

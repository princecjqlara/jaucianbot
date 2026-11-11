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
    def test_same_team_in_two_groups_uses_distinct_manifests_and_current_destination(self):
        import copy
        other_chat = next(chat for chat in GROUPS if chat != CHAT)
        data = report()
        first = data['teams'][0]
        first.update(delivery_key=f'team:{CHAT}', group_report=True)
        second = copy.deepcopy(first)
        second.update(chat_id=other_chat, delivery_key=f'team:{other_chat}', access_signature='other-scope', thread_id=777)
        data['teams'].append(second)
        with patch.object(shifts, 'request', return_value=[{'payload': {'parts': ['snapshot'], 'signature': 'allowed'}}]) as read:
            for team in data['teams']:
                shifts.manifest(data, team, NOW.date(), '16', NOW)
        self.assertNotEqual(read.call_args_list[0].args[0], read.call_args_list[1].args[0])
        action = {'chat_id': other_chat, 'payload': {'veo_report_date': '2026-11-06', 'veo_team_id': 'team', 'veo_report_signature': 'other-scope', 'veo_report_text': 'Page-scoped snapshot', 'veo_report_part': 0, 'veo_report_parts': 1}}
        with patch.object(workspace, 'reporting', return_value=data), patch('telegram_sender.telegram_call', return_value={'message_id': 11}) as send:
            shifts.send(action)
        self.assertEqual(send.call_args.args[1]['message_thread_id'], 777)
        self.assertEqual(send.call_args.args[1]['chat_id'], other_chat)
        text = '\n'.join(shifts.lines(data, first, '16'))
        self.assertIn("this group's routed Pages", text)
        self.assertIn('Whole-team capacity across all Pages:', text)

    def test_availability_accepts_secondary_current_group_but_rejects_removed_links(self):
        tomorrow = str(dt.datetime.now(ZoneInfo('Asia/Manila')).date() + dt.timedelta(days=1))
        action = {'chat_id': CHAT, 'payload': {'veo_tenant_id': TENANT, 'veo_availability_date': tomorrow}}
        data = {'teams': [{'telegram_groups_managed': True, 'telegram_chat_id': -999, 'telegram_groups': [{'chat_id': CHAT}]}]}
        with patch('veo_directory.workspace_directory', return_value=data), patch('veo_directory.teams', return_value={CHAT}), patch('telegram_sender.telegram_call', return_value={'message_id': 1}) as send:
            shifts.send_availability(action)
            self.assertEqual(send.call_args.args[1]['message_thread_id'], GROUPS[CHAT]['active'])
            data['teams'][0]['telegram_groups'] = []
            with self.assertRaises(shifts.ReportScopeChanged): shifts.send_availability(action)
            self.assertEqual(send.call_count, 1)

    def test_quota_reminders_show_team_and_personal_remaining_targets_and_authoritative_rate(self):
        data = report(); worker = data["teams"][0]["workers"][0]
        worker["salary"].update(closed=1, done=0, target=2, rate=35, provisional=True)
        text = "\n".join(shifts.lines(data, data["teams"][0], "16"))
        self.assertIn("Team quota reminder: 1 more Closed and 2 more Done", text)
        self.assertIn("Quota reminder: 1 more Closed and 2 more Done", text)
        self.assertIn("35% (provisional until Philippine midnight)", text)
        worker["salary"].update(closed=2, done=2, rate=40, provisional=False)
        text = "\n".join(shifts.lines(data, data["teams"][0], "24"))
        self.assertIn("Quota reached: both personal", text)
        self.assertIn("40% (final for this work date)", text)

    def test_no_ad_zero_operating_quota_does_not_invent_zero_target_commission(self):
        data = report(); worker = data["teams"][0]["workers"][0]
        worker["salary"].update(closed=0, done=0, target=1, operating_quota=0, production_capacity=1, rate=35, provisional=True)
        text = "\n".join(shifts.lines(data, data["teams"][0], "16"))
        self.assertIn("Quota reminder: 1 more Closed and 1 more Done", text)
        self.assertIn("35%", text); self.assertNotIn("Quota reached:", text)

    def test_report_includes_canonical_poll_and_mapping_warnings_without_publishing_salary(self):
        data = report()
        data["teams"][0]["workers"][0]["warnings"] = [
            {"kind": "availability_unanswered", "severity": "attention", "work_date": "2026-11-07", "poll_url": "https://t.me/c/123/99"},
            {"kind": "telegram_mapping_missing", "severity": "attention"}]
        text = "\n".join(shifts.lines(data, data["teams"][0], "16"))
        self.assertIn("WARNINGS / ATTENTION: 2", text)
        self.assertIn("active-for-tomorrow", text)
        self.assertIn("https://t.me/c/123/99", text)
        self.assertIn("Telegram identity or team mapping is incomplete", text)
        self.assertNotIn("400.00", text)

    def test_overdue_reason_is_preserved_instead_of_claiming_it_was_not_recorded(self):
        data = report()
        client = data["teams"][0]["workers"][0]["clients"][0]
        client.update(overdue=True, overdue_hours=4, work_status="working", work_reason="Client requested a revised script")
        text = "\n".join(shifts.lines(data, data["teams"][0], "16"))
        self.assertIn("OVERDUE 4.0h", text)
        self.assertIn("Delay explanation: Client requested a revised script", text)
        self.assertNotIn("WARNING: add", text)
        for placeholder in ("", "N/A", "unknown", "none"):
            client["work_reason"] = placeholder
            self.assertIn("WARNING: add", "\n".join(shifts.lines(data, data["teams"][0], "16")))

    def test_report_without_custom_topic_uses_the_existing_announcements_topic(self):
        data = report(); data["teams"][0]["thread_id"] = None
        action = {"chat_id": CHAT, "payload": {"veo_report_date": "2026-11-06", "veo_team_id": "team", "veo_report_signature": "allowed", "veo_report_text": "snapshot", "veo_report_part": 0, "veo_report_parts": 1}}
        with patch.object(workspace, "reporting", return_value=data), patch("telegram_sender.telegram_call", return_value={"message_id": 12}) as send:
            shifts.send(action)
        self.assertEqual(send.call_args.args[1]["message_thread_id"], GROUPS[CHAT]["announcements"])

    def test_held_receipts_prompt_missing_workflow_steps_without_reporting_unearned_salary(self):
        data = report()
        data["teams"][0]["workers"][0]["receipt_holds"] = [{"contact_name": "Held client", "reasons": ["converted_lead_required", "create_and_link_order", "verify_full_payment"]}]
        text = "\n".join(shifts.lines(data, data["teams"][0], "16"))
        self.assertIn("SALARY ON HOLD", text)
        self.assertIn("Closed Deal: Qualified/Converted lead + create/link the Facebook order", text)
        self.assertIn("Done Deal: separately verify payment and mark paid", text)
        self.assertNotIn("Closed Deals do not require an order", text)
        self.assertIn("Held client: set lead to Converted, create/link the order, verify the full payment", text)
        self.assertIn("Recheck", text)
        self.assertNotIn("400.00", text)

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

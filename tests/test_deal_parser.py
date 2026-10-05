import unittest
from decimal import Decimal

from daily_automation import GROUPS
from deal_parser import collect_deals, parse_close_count, parse_page, parse_price


CONFIG = GROUPS[-1003962888977]


def post(mid, text, *, author_id=1, thread=7, sent="2026-10-05T00:00:00+00:00", media="text"):
    return {"message_id": mid, "text": text, "author_id": author_id, "author_name": "Alex",
            "thread_id": thread, "sent_utc": sent, "content_type": media}


class DealParserTests(unittest.TestCase):
    def test_price_arithmetic_is_complete_and_verifies_written_total(self):
        for value, expected in [
            ("650 + 550 (two videos) =1,200", "1200"), ("1,299+100", "1399"),
            ("899 x 3 = 2,697", "2697"), ("600×2 (2PROJECTS)", "1200"),
            ("1,499 & 699", "2198"), ("1099 and 1099 (2 videos)", "2198"),
            ("PHP 979.30 (Old Promo)", "979.30"), ("100 poster", "100"),
            ("1,499 UPGRADE", "1499"), ("1499 (", "1499"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(parse_price("PD: " + value), Decimal(expected))
        self.assertIsNone(parse_price("PD: 650 + 550 = 1,300"))

    def test_extra_fees_and_notes_do_not_inflate_sales(self):
        self.assertEqual(parse_price("PD: 650\nDP: 100\nTip: 50\nRevision: 300\nTP: 1,100"), Decimal("650"))
        self.assertEqual(parse_price("PD: 1,100 + 100 revision"), Decimal("1100"))
        self.assertEqual(parse_price("PD: 899 (+100 revision)"), Decimal("899"))
        self.assertEqual(parse_price("PD: 899 (24 seconds)"), Decimal("899"))
        self.assertIsNone(parse_price("PD: 400 revision"))

    def test_malformed_or_ambiguous_values_never_use_a_numeric_prefix(self):
        for value in ("-100", "1,20", "1.299", "650/900", "650 + pending", "3 x 30 seconds",
                      "650(+100, +10seconds)", "__import__('os')", "650 = 900"):
            with self.subTest(value=value):
                self.assertIsNone(parse_price("Price Deal: " + value))
        self.assertIsNone(parse_price("PD: 650\nPD: 900"))
        self.assertEqual(parse_price("PD: 650\nPD: 650"), Decimal("650"))

    def test_blank_fields_cannot_consume_the_next_line(self):
        self.assertIsNone(parse_price("PD:\n650"))
        self.assertIsNone(parse_page("Page:\nManawari Studios", CONFIG["pages"]))
        self.assertIsNone(parse_close_count("Close Deal:\n2"))

    def test_page_without_colon_and_unicode_space(self):
        self.assertEqual(parse_page("Page Manawari Studios", CONFIG["pages"]), "Manawari Studios")
        self.assertEqual(parse_price("PD:\u00a0₱1,200"), Decimal("1200"))
        self.assertIsNone(parse_page("Page: Manawari Studios\nPage: Unrelated", CONFIG["pages"]))

    def test_named_deposit_is_a_cd_without_a_fixed_price(self):
        text = "October 5, 2026\nClient A\nDP: 100\nPD: -\nPage: Manawari Studios"
        self.assertEqual(parse_close_count(text), 1)
        self.assertIsNone(parse_close_count(text.replace("DP: 100", "DP: 0")))
        self.assertIsNone(parse_close_count(text.replace("PD: -", "PD: 0")))
        self.assertIsNone(parse_close_count(text.replace("PD: -", "PD: pending")))

    def test_zero_summary_is_valid_and_invalid_summary_does_not_fall_back_to_price(self):
        self.assertEqual(parse_close_count("Close Deal: 0"), 0)
        self.assertIsNone(parse_close_count("Close Deal: pending\nPD: 650"))
        self.assertIsNone(parse_close_count("Close Deal: 2.5\nPD: 650"))

    def test_reposts_are_counted_once_but_distinct_clients_and_days_are_preserved(self):
        text = "October 5, 2026\nClient A\nPAID\nPD: 650\nPage: Manawari Studios"
        rows = [post(1, text), post(2, text), post(3, text.replace("Client A", "Client B")),
                post(4, text, sent="2026-10-06T00:00:00+00:00")]
        parsed = collect_deals(rows, CONFIG)
        self.assertEqual(sum(e["count"] for e in parsed["entries"]), 3)
        self.assertEqual(sum(e["gross"] for e in parsed["entries"]), Decimal("1950"))
        self.assertEqual([r["message_id"] for r in parsed["duplicate_rows"]], [1])

    def test_different_employees_with_same_name_keep_separate_summaries(self):
        text = "Page: Manawari Studios\nClose Deal: 2"
        parsed = collect_deals([post(1, text, thread=6), post(2, text, thread=6, author_id=2)], CONFIG)
        self.assertEqual(sum(e["count"] for e in parsed["entries"]), 4)

    def test_latest_summary_and_individuals_are_not_added_twice(self):
        rows = [post(1, "Client A\nPD: 650\nPage: Manawari Studios", thread=6),
                post(2, "Page: Manawari Studios\nClose Deal: 1", thread=6),
                post(3, "Page: Manawari Studios\nClose Deal: 3", thread=6)]
        for inputs in (rows, list(reversed(rows))):
            parsed = collect_deals(inputs, CONFIG)
            self.assertEqual(sum(e["count"] for e in parsed["entries"]), 3)
        rows.append(post(4, "Page: Manawari Studios\nClose Deal: 0", thread=6))
        self.assertEqual(sum(e["count"] for e in collect_deals(rows, CONFIG)["entries"]), 1)

    def test_cross_author_client_reposts_require_ownership_review(self):
        text = "Client A\nPD: 650\nPage: Manawari Studios"
        parsed = collect_deals([post(1, text), post(2, text, author_id=2)], CONFIG)
        self.assertEqual(parsed["entries"], [])
        self.assertEqual(len(parsed["ownership_conflicts"]), 2)
        self.assertEqual(len(parsed["uncertain_rows"]), 2)

    def test_supporting_photo_of_a_duplicate_is_not_a_new_unreadable_sale(self):
        text = "Client A\nPD: 650\nPage: Manawari Studios"
        rows = [post(1, text, media="photo"), post(2, None, media="photo"),
                post(3, text, media="photo", sent="2026-10-05T00:01:00+00:00"),
                post(4, None, media="photo", sent="2026-10-05T00:01:01+00:00")]
        parsed = collect_deals(rows, CONFIG)
        self.assertEqual(len(parsed["entries"]), 1)
        self.assertEqual(len(parsed["supporting_rows"]), 2)
        self.assertEqual(parsed["uncertain_rows"], [])

    def test_unmatched_media_and_zero_summaries_remain_distinct(self):
        rows = [post(1, None, media="photo"), post(2, "Page: Manawari Studios\nClose Deal: 0", thread=6)]
        parsed = collect_deals(rows, CONFIG)
        self.assertEqual([r["message_id"] for r in parsed["uncertain_rows"]], [1])
        self.assertEqual(parsed["entries"][0]["count"], 0)


if __name__ == "__main__":
    unittest.main()

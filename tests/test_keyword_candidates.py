"""Real SQLite candidate proof, overlap, bounded retry and delivery isolation."""

import asyncio
import json
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from test_search_controls import DatabaseFixture

import alert_delivery
import ebay_store
import keyword_candidates as candidates
import search_settings


class CandidateTests(DatabaseFixture, unittest.TestCase):
    def test_rollout_read_distinguishes_old_unproven_rows_without_changing_queue(self):
        self.enable()
        with closing(search_settings.connection()) as conn, conn:
            for number, title in (
                (9001, "Zara skinny jeans"),
                (9002, "Zara jeans"),
                (9003, "Zara wide-leg jeans"),
                (9004, "Zara jeans"),
            ):
                conn.execute(
                    """INSERT INTO alert_outbox
                    (item_id,query_id,search_name,content,url,title,price,currency,found_at,platform)
                    VALUES (?,1,'TEST','TEST',? ,?,'10.00','GBP',1020,'vinted')""",
                    (str(number), f"https://www.vinted.co.uk/items/{number}", title),
                )
            for number, text in ((9002, "skinny fit"), (9003, "wide legs")):
                conn.execute(
                    "INSERT INTO vinted_alert_details VALUES (?,?)",
                    (
                        str(number),
                        json.dumps({"description": text, "description_complete": True}),
                    ),
                )
            before = list(conn.iterdump())
            counts = candidates.pending_boundary(conn)
            self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(counts["vinted"], {"pending": 4, "unverified": 2})
        self.assertEqual(counts["ebay"], {"pending": 0, "unverified": 0})

    def enable(self, query_id=1, keywords=None, platform="vinted"):
        config = dict(
            ebay_store.DEFAULTS,
            shared_alert_version=1,
            shared_keywords=keywords or ["skinny"],
            max_price=2500,
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                """INSERT OR REPLACE INTO search_platforms VALUES (?,?,?,?,?)""",
                (
                    query_id,
                    int(platform == "vinted"),
                    int(platform == "ebay"),
                    json.dumps(config),
                    0,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO vinted_search_budgets VALUES (?,2500,220)",
                (query_id,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('ebay_live_ids','[1,2]')"
            )
        return search_settings.get_search(query_id)

    def item(self, platform="vinted", title="Zara jeans", number=9001):
        return {
            "item_id": str(number) if platform == "vinted" else f"ebay:{number}",
            "title": title,
            "price": "10.00" if platform == "vinted" else 1000,
            "currency": "GBP",
            "photo_url": None,
            "url": (
                f"https://www.vinted.co.uk/items/{number}"
                if platform == "vinted"
                else f"https://www.ebay.co.uk/itm/{number}"
            ),
            "timestamp": 1010,
            "estimated_total": 1400,
        }

    def alert(self, platform="vinted"):
        return {
            "content": "Zara jeans",
            "search_name": "TEST",
            "reference_id": None,
            ("vinted_details" if platform == "vinted" else "ebay_details"): {
                "photos": [],
                "single_message": True,
                "photo_card": True,
                "shared_alert_version": 1,
                "shared_keywords": ["skinny"],
            },
        }

    def stage(self, search=None, platform="vinted", **changes):
        item = self.item(platform, **changes)
        if platform == "ebay":
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO ebay_item_owners VALUES (?,?,?)",
                    (
                        f"v1|{item['item_id'].removeprefix('ebay:')}|0",
                        item["item_id"],
                        "known-seller",
                    ),
                )
        candidates.stage(
            platform,
            item,
            search or self.enable(platform=platform),
            self.alert(platform),
            now=1020,
        )
        return item

    def rows(self, table):
        with closing(search_settings.connection()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM " + table)]

    def details(self, description="Beautiful skinny fit", **changes):
        return dict(
            {
                "description": description,
                "keyword_description": description,
                "description_complete": True,
                "available": True,
                "photos": [],
            },
            **changes,
        )

    def test_staging_consumes_no_global_seen_or_outbox_and_is_idempotent(self):
        search = self.enable()
        item = self.stage(search)
        candidates.stage("vinted", item, search, self.alert(), now=1030)
        self.assertEqual(len(self.rows("keyword_candidates")), 1)
        self.assertEqual(len(self.rows("items")), 1)
        self.assertEqual(self.rows("alert_outbox"), [])
        self.assertEqual(self.rows("keyword_candidates")[0]["retry_until"], 1920)

    def test_bound_description_match_atomically_registers_once(self):
        self.stage()
        with patch.object(
            candidates, "fetch_candidate_details", return_value=self.details()
        ) as fetch:
            self.assertTrue(candidates.process_one("vinted", now=1021))
            self.assertFalse(candidates.process_one("vinted", now=1052))
        fetch.assert_called_once()
        self.assertEqual(self.rows("alert_outbox")[0]["item_id"], "9001")
        self.assertEqual(len(self.rows("items")), 2)
        details = json.loads(self.rows("vinted_alert_details")[0]["payload"])
        self.assertTrue(details["keyword_verified"])
        self.assertTrue(details["description_complete"])
        self.assertEqual(
            details["keyword_evidence"],
            {"state": "matched", "keyword": "skinny", "field": "description"},
        )

    def test_overlap_first_query_mismatch_does_not_consume_second_query(self):
        self.stage(self.enable(1, ["skinny"]))
        self.stage(self.enable(2, ["bootcut"]))
        with patch.object(
            candidates,
            "fetch_candidate_details",
            return_value=self.details("Classic bootcut legs"),
        ) as fetch:
            candidates.process_one("vinted", now=1021)
        fetch.assert_called_once()
        self.assertEqual(self.rows("alert_outbox")[0]["query_id"], 2)
        self.assertEqual(
            [r["status"] for r in self.rows("keyword_candidates")],
            ["rejected", "accepted"],
        )

    def test_transient_unknown_is_retained_and_recovery_does_not_need_fresh_poll(self):
        self.stage()
        with patch.object(
            candidates, "fetch_candidate_details", side_effect=[{}, self.details()]
        ) as fetch:
            candidates.process_one("vinted", now=1021)
            self.assertEqual(self.rows("alert_outbox"), [])
            self.assertEqual(self.rows("keyword_candidates")[0]["status"], "pending")
            self.assertFalse(candidates.process_one("vinted", now=1040))
            self.assertTrue(candidates.process_one("vinted", now=1052))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(self.rows("alert_outbox")), 1)

    def test_retries_stop_unknown_without_calling_it_a_match_or_mismatch(self):
        self.stage()
        with patch.object(
            candidates, "fetch_candidate_details", return_value={}
        ) as fetch:
            for now in (1021, 1052, 1083, 1114):
                candidates.process_one("vinted", now=now)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.rows("keyword_candidates")[0]["status"], "unverified")
        self.assertEqual(self.rows("alert_outbox"), [])

    def test_shared_api_deferral_respects_returned_cooldown_and_does_not_spend_attempt(
        self,
    ):
        self.stage()
        with patch.object(
            candidates, "fetch_candidate_details", return_value={"retry_after": 1800}
        ) as fetch:
            candidates.process_one("vinted", now=1021)
            self.assertFalse(candidates.process_one("vinted", now=1799))
        fetch.assert_called_once()
        self.assertEqual(self.rows("keyword_description_cache")[0]["attempts"], 0)

    def test_network_read_runs_outside_sqlite_write_transaction(self):
        self.stage()

        def fetch(*_):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
            return self.details()

        with patch.object(candidates, "fetch_candidate_details", side_effect=fetch):
            candidates.process_one("vinted", now=1021)
        self.assertEqual(len(self.rows("alert_outbox")), 1)

    def test_saved_edit_during_get_cannot_enqueue_or_cache_old_result(self):
        self.stage()

        def fetch(*_):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE search_dashboard SET revision=revision+1 WHERE query_id=1"
                )
                # Fixture has no dashboard row until an edit creates one.
                conn.execute(
                    "INSERT OR IGNORE INTO search_dashboard(query_id,revision) VALUES (1,1)"
                )
            return self.details()

        with patch.object(candidates, "fetch_candidate_details", side_effect=fetch):
            candidates.process_one("vinted", now=1021)
        self.assertEqual(self.rows("alert_outbox"), [])
        self.assertEqual(self.rows("keyword_candidates")[0]["status"], "cancelled")
        self.assertEqual(self.rows("keyword_description_cache"), [])

    def test_current_title_exclusion_and_budget_are_rechecked_after_read(self):
        self.stage(title="Zara ripped jeans")

        def fetch(*_):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO search_preferences VALUES (1,'','[\"ripped\"]')"
                )
            return self.details()

        with patch.object(candidates, "fetch_candidate_details", side_effect=fetch):
            candidates.process_one("vinted", now=1021)
        self.assertEqual(self.rows("alert_outbox"), [])
        self.assertIn("title exclusions", self.rows("keyword_candidates")[0]["reason"])

    def test_current_budget_stops_enqueuing_even_without_revision_change(self):
        self.stage()

        def fetch(*_):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_search_budgets SET max_total=100 WHERE query_id=1"
                )
            return self.details()

        with patch.object(candidates, "fetch_candidate_details", side_effect=fetch):
            candidates.process_one("vinted", now=1021)
        self.assertEqual(self.rows("alert_outbox"), [])

    def test_ebay_owner_removal_during_get_cannot_restore_listing(self):
        self.stage(platform="ebay")

        def fetch(*_):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("DELETE FROM ebay_item_owners")
            return self.details()

        with patch.object(candidates, "fetch_candidate_details", side_effect=fetch):
            candidates.process_one("ebay", now=1021)
        self.assertEqual(self.rows("alert_outbox"), [])
        self.assertEqual(self.rows("keyword_description_cache"), [])

    def test_ebay_normal_enqueued_price_remains_pounds_and_saved_total(self):
        self.stage(platform="ebay")
        with patch.object(
            candidates, "fetch_candidate_details", return_value=self.details()
        ):
            candidates.process_one("ebay", now=1021)
        self.assertEqual(self.rows("alert_outbox")[0]["price"], "10.00")

    def test_new_overlap_reuses_completed_cache_without_another_get(self):
        self.stage(self.enable(1, ["skinny"]))
        with patch.object(
            candidates, "fetch_candidate_details", return_value=self.details()
        ) as fetch:
            candidates.process_one("vinted", now=1021)
            self.stage(self.enable(2, ["skinny"]))
            candidates.process_one("vinted", now=1022)
        fetch.assert_called_once()
        self.assertEqual(len(self.rows("alert_outbox")), 1)

    def test_legacy_search_never_stages_or_reads_details(self):
        search = search_settings.get_search(1)
        self.assertFalse(
            candidates.stage("vinted", self.item(), search, self.alert(), now=1020)
        )
        with patch.object(candidates, "fetch_candidate_details") as fetch:
            self.assertFalse(candidates.process_one("vinted", now=1021))
        fetch.assert_not_called()

    def test_title_evidence_needs_no_detail_call_and_does_not_claim_description_read(
        self,
    ):
        self.stage(title="Zara skinny jeans")
        with patch.object(candidates, "fetch_candidate_details") as fetch:
            candidates.process_one("vinted", now=1021)
        fetch.assert_not_called()
        details = json.loads(self.rows("vinted_alert_details")[0]["payload"])
        self.assertFalse(details["description_checked"])

    def test_expired_candidate_stays_unverified_and_performs_no_new_read(self):
        self.stage()
        with patch.object(candidates, "fetch_candidate_details") as fetch:
            self.assertFalse(candidates.process_one("vinted", now=1921))
        fetch.assert_not_called()
        self.assertEqual(self.rows("keyword_candidates")[0]["status"], "unverified")

    def test_late_network_result_cannot_use_an_expired_lease(self):
        self.stage()
        with patch.object(
            candidates.time, "time", side_effect=[1021, 1142]
        ), patch.object(
            candidates, "fetch_candidate_details", return_value=self.details()
        ):
            candidates.process_one("vinted")
        self.assertEqual(self.rows("alert_outbox"), [])
        self.assertEqual(self.rows("keyword_candidates")[0]["status"], "pending")

    def test_missing_current_shared_budget_fails_closed(self):
        self.stage()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets WHERE query_id=1")
        with patch.object(candidates, "fetch_candidate_details") as fetch:
            self.assertFalse(candidates.process_one("vinted", now=1021))
        fetch.assert_not_called()
        self.assertEqual(self.rows("alert_outbox"), [])

    def test_verification_task_does_not_block_delivery_loop(self):
        async def exercise():
            blocked = asyncio.Event()

            async def verification(_platform):
                await blocked.wait()

            worker = alert_delivery.DeliveryWorker(None, 123)
            worker.tick = AsyncMock(side_effect=asyncio.CancelledError)
            with patch.object(
                candidates, "run", side_effect=verification
            ) as task, self.assertRaises(asyncio.CancelledError):
                await worker.run()
            self.assertEqual(worker.tick.await_count, 1)
            task.assert_called_once_with("vinted")

        asyncio.run(exercise())

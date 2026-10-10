"""Quiet dashboard changes cannot consume another active search's new item."""

import unittest
from contextlib import closing
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from test_search_controls import DatabaseFixture

import dashboard_store
import db
import search_settings
import vinted_keywords


class VintedSearchBaselines(DatabaseFixture, unittest.TestCase):
    def save(self, query_id=None, **changes):
        old = search_settings.get_search(query_id) if query_id is not None else None
        form = {
            "shared_alert_version": "1",
            "query_name": "Hollister jacket",
            "platform_mode": "vinted",
            "query": (
                old["query"]
                if old
                else "https://www.vinted.co.uk/catalog?brand_ids[]=111&catalog[]=2050&size_ids[]=4"
            ),
            "shared_keywords": "fur, sherpa",
            "vinted_max_total": "20.00",
            "exclusions": "teddy",
            "reminder": "Check hood; resale aim £60",
            "revision": str(old["revision"] if old else 0),
        }
        form.update(changes)
        return dashboard_store.save_search(query_id, form)

    def page(self, query_id, ids, *, variant=None, title="Hollister fur hood jacket"):
        search = search_settings.get_search(query_id)
        source, output = Queue(), Queue()
        items = [
            SimpleNamespace(
                id=identity,
                title=title,
                brand_title="Hollister",
                price="15",
                currency="GBP",
                photo=None,
                url=f"https://www.vinted.co.uk/items/{identity}",
                has_real_timestamp=False,
                raw_timestamp=1000,
                observed_at=1000,
                raw_data={},
            )
            for identity in ids
        ]
        source.put(
            (
                items,
                query_id,
                variant["url"] if variant else search["query"],
                variant["id"] if variant else None,
            )
        )
        with patch.object(self.core, "time", return_value=1000):
            self.core.clear_item_queue(source, output)
        return [output.get_nowait() for _ in range(output.qsize())]

    def test_ordinary_quiet_baseline_does_not_consume_live_search_hit(self):
        identity = self.save(shared_keywords="")
        self.assertEqual(self.page(identity, [501]), [])
        self.assertFalse(db.is_item_in_db_by_id(501))
        self.assertEqual(len(self.page(2, [501])), 1)
        self.assertEqual(self.page(identity, [501]), [])
        self.assertEqual(len(self.page(identity, [502])), 1)
        self.assertEqual(self.page(2, [502]), [])

    def test_new_keyword_baseline_does_not_consume_existing_keyword_hit(self):
        identity = self.save(shared_keywords="fur")
        fur = vinted_keywords.rows(identity)[0]
        self.assertEqual(self.page(identity, [500], variant=fur), [])
        self.save(identity, shared_keywords="fur, sherpa")
        fur, sherpa = vinted_keywords.rows(identity)
        self.assertEqual(fur["primed"], 1)
        self.assertEqual(sherpa["primed"], 0)
        self.assertEqual(self.page(identity, [501], variant=sherpa), [])
        self.assertFalse(db.is_item_in_db_by_id(501))
        self.assertEqual(len(self.page(identity, [501, 500], variant=fur)), 1)
        self.assertEqual(self.page(identity, [501], variant=sherpa), [])
        self.assertEqual(len(self.page(identity, [502, 501], variant=sherpa)), 1)
        self.assertEqual(self.page(identity, [502, 501], variant=fur), [])

    def test_each_keyword_resumes_with_its_own_quiet_baseline(self):
        identity = self.save()
        fur, sherpa = vinted_keywords.rows(identity)
        self.page(identity, [500], variant=fur)
        self.page(identity, [400], variant=sherpa)
        old = search_settings.get_search(identity)
        dashboard_store.change_state(identity, "pause", old["revision"])
        self.assertEqual(self.page(identity, [501], variant=fur), [])
        old = search_settings.get_search(identity)
        dashboard_store.change_state(identity, "resume", old["revision"])
        fur, sherpa = vinted_keywords.rows(identity)
        self.assertEqual([fur["primed"], sherpa["primed"]], [0, 0])
        self.assertEqual(self.page(identity, [501, 500], variant=fur), [])
        # Completing the first keyword must not make the second emit its backlog.
        self.assertEqual(self.page(identity, [401, 400], variant=sherpa), [])
        self.assertEqual(len(self.page(identity, [502, 501], variant=fur)), 1)
        self.assertEqual(len(self.page(identity, [402, 401], variant=sherpa)), 1)

    def test_reenabled_vinted_keywords_are_quiet_without_changing_buying(self):
        identity = self.save()
        for variant in vinted_keywords.rows(identity):
            self.page(identity, [500], variant=variant)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE search_platforms SET vinted_enabled=0 WHERE query_id=?",
                (identity,),
            )
            buyer_before = tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            attempts_before = conn.execute(
                "SELECT * FROM vinted_buy_attempts"
            ).fetchall()
        self.save(identity)
        variants = vinted_keywords.rows(identity)
        self.assertTrue(all(not row["primed"] for row in variants))
        for variant in variants:
            self.assertEqual(self.page(identity, [501, 500], variant=variant), [])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone()),
                buyer_before,
            )
            self.assertEqual(
                conn.execute("SELECT * FROM vinted_buy_attempts").fetchall(),
                attempts_before,
            )

    def test_notes_only_edit_keeps_live_keyword_baselines(self):
        identity = self.save()
        for variant in vinted_keywords.rows(identity):
            self.page(identity, [500], variant=variant)
        before = vinted_keywords.rows(identity)
        self.save(identity, reminder="Check cuffs; resale aim £65")
        self.assertEqual(vinted_keywords.rows(identity), before)
        self.assertEqual(len(self.page(identity, [501, 500], variant=before[0])), 1)

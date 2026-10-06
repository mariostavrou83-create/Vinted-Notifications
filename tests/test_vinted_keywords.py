"""Grouped searches keep separate baselines, filters and shared deduplication."""

import unittest
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_search_controls import DatabaseFixture

import dashboard_store
import db
import resource_controls
import search_settings
import vinted_keywords as keywords


class KeywordTests(DatabaseFixture, unittest.TestCase):
    def save(self, words="fur, sherpa, lined", **changes):
        old = search_settings.get_search(1)
        form = {
            "query_name": "Cardigans",
            "query": "https://www.vinted.co.uk/catalog?catalog[]=10&size_ids[]=1&size_ids[]=2&status_ids[]=2&brand_ids[]=30&price_to=20&order=newest_first&search_text=old",
            "revision": str(old["revision"]),
            "vinted_keywords": words,
        }
        form.update(changes)
        dashboard_store.save_search(1, form)
        return keywords.rows(1)

    def variant_batch(self, variant, ids):
        source, output = Queue(), Queue()
        items = [
            SimpleNamespace(
                id=i,
                title="fur cardigan",
                brand_title="Hollister",
                price="15",
                currency="GBP",
                photo=None,
                url=f"https://www.vinted.co.uk/items/{i}",
                has_real_timestamp=False,
                raw_timestamp=200,
                observed_at=200,
            )
            for i in ids
        ]
        source.put((items, 1, variant["url"], variant["id"]))
        with patch.object(self.core, "time", return_value=1000):
            self.core.clear_item_queue(source, output)
        return [output.get_nowait() for _ in range(output.qsize())]

    def test_each_alternative_is_remote_search_with_identical_filters(self):
        rows = self.save("fur, sherpa\nfur lined, FUR")
        self.assertEqual([r["keyword"] for r in rows], ["fur", "sherpa", "fur lined"])
        base = parse_qs(urlsplit(search_settings.get_search(1)["query"]).query)
        self.assertNotIn("search_text", base)
        for r in rows:
            params = parse_qs(urlsplit(r["url"]).query)
            self.assertEqual(params.pop("search_text"), [r["keyword"]])
            self.assertEqual(params, base)
        expanded = keywords.expand(search_settings.active_queries())
        self.assertEqual(len(expanded), 46)
        self.assertNotIn(1, expanded)
        self.assertEqual({expanded[-r["id"]][0] for r in rows}, {1})
        self.assertEqual(resource_controls.summary()["checks"], 46)

    def test_independent_priming_frontiers_and_shared_duplicate_suppression(self):
        fur, sherpa, lined = self.save()
        self.assertEqual(self.variant_batch(fur, [1000]), [])
        self.assertEqual(self.variant_batch(sherpa, [500]), [])
        # A newer result for the second keyword is below the first one's frontier.
        self.assertEqual(len(self.variant_batch(sherpa, [501])), 1)
        self.assertEqual(self.variant_batch(lined, []), [])
        self.assertEqual(len(self.variant_batch(lined, [502])), 1)
        self.assertEqual(self.variant_batch(sherpa, [502]), [])
        self.assertEqual(len(self.variant_batch(fur, [1001])), 1)

    def test_adding_keyword_retains_existing_baselines_and_primes_only_new_word(self):
        fur = self.save("fur")[0]
        self.variant_batch(fur, [500])
        rows = self.save("fur, sherpa")
        self.assertEqual(rows[0]["id"], fur["id"])
        self.assertEqual(rows[0]["primed"], 1)
        self.assertEqual(rows[1]["primed"], 0)
        self.assertEqual(len(self.variant_batch(rows[0], [501])), 1)
        self.assertEqual(self.variant_batch(rows[1], [600]), [])

    def test_removed_changed_or_paused_variant_cannot_emit_stale_results(self):
        fur = self.save("fur")[0]
        self.variant_batch(fur, [500])
        self.save("sherpa")
        self.assertEqual(self.variant_batch(fur, [501]), [])
        sherpa = keywords.rows(1)[0]
        self.variant_batch(sherpa, [600])
        old = search_settings.get_search(1)
        dashboard_store.change_state(1, "pause", str(old["revision"]))
        self.assertEqual(self.variant_batch(sherpa, [601]), [])
        self.assertNotIn(
            -sherpa["id"], keywords.expand(search_settings.active_queries())
        )

    def test_existing_searches_and_exclusions_are_preserved(self):
        before = db.get_queries()
        self.assertEqual(len(keywords.expand(search_settings.active_queries())), 44)
        self.assertEqual(db.get_queries(), before)
        fur = self.save("fur", exclusions="cardigan")[0]
        self.variant_batch(fur, [])
        self.assertEqual(self.variant_batch(fur, [501]), [])

    def test_too_many_keywords_rejected_atomically(self):
        before = db.get_queries()
        with self.assertRaises(ValueError):
            self.save(",".join(str(i) for i in range(21)))
        self.assertEqual(db.get_queries(), before)


if __name__ == "__main__":
    unittest.main()

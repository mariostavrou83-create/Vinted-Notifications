import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from discovery_shadow import DiscoveryShadow, discovery_url

URL = "https://www.vinted.co.uk/catalog?search_text=fur+hood&brand_ids%5B%5D=88&size_ids%5B%5D=2&price_to=10&currency=GBP&order=newest_first"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = [10.0]
        self.cooldown = [0]
        self.calls = []
        self.future = Future()
        self.executor = SimpleNamespace(
            submit=lambda *args: (self.calls.append(args) or self.future),
            shutdown=lambda **kwargs: None,
        )
        with patch("discovery_shadow.ThreadPoolExecutor", return_value=self.executor):
            self.shadow = DiscoveryShadow(
                lambda *args: [],
                SimpleNamespace(remaining=lambda: self.cooldown[0]),
                clock=lambda: self.clock[0],
                wall=lambda: self.clock[0],
            )
        self.queries = {10: (10, URL)}
        self.shadow.tick(self.queries)

    def tearDown(self):
        self.shadow.close()

    def item(self, item_id, seen):
        return SimpleNamespace(id=item_id, observed_at=seen)

    def test_only_keyword_removed_and_duplicate_filters_preserved(self):
        url = URL + "&brand_ids%5B%5D=99"
        expected = [
            (k, v) for k, v in parse_qsl(urlsplit(url).query) if k != "search_text"
        ]
        self.assertEqual(parse_qsl(urlsplit(discovery_url(url)).query), expected)
        self.assertIsNone(
            discovery_url("https://www.vinted.co.uk/catalog?search_text=fur")
        )
        self.assertIsNone(discovery_url("https://www.vinted.co.uk/catalog?price_to=10"))

    def test_baselines_do_not_create_false_wins_and_new_item_compares_once(self):
        self.shadow.observe("canonical", 10, URL, [self.item(1, 10)])
        self.shadow.observe("discovery", 10, URL, [self.item(1, 11)])
        self.assertEqual(self.shadow.matches, 0)
        self.shadow.observe("discovery", 10, URL, [self.item(2, 12)])
        self.shadow.observe("canonical", 10, URL, [self.item(2, 17)])
        self.shadow.observe("canonical", 10, URL, [self.item(2, 18)])
        self.assertEqual((self.shadow.matches, self.shadow.wins), (1, 1))
        self.assertEqual(self.shadow.records[(10, URL, 2)]["canonical"], 17)

    def test_rate_bound_and_no_overlapping_discovery(self):
        self.clock[0] = 10.5
        self.future.set_result([])
        self.shadow.tick(self.queries)
        self.assertEqual(len(self.calls), 1)
        self.clock[0] = 11
        self.future = Future()
        self.shadow.tick(self.queries)
        self.clock[0] = 14
        self.shadow.tick(self.queries)
        self.assertEqual(len(self.calls), 2)

    def test_cooldown_or_error_stops_experiment(self):
        self.future.set_exception(RuntimeError("offline"))
        self.shadow.tick(self.queries)
        self.assertTrue(self.shadow.stopped)
        self.assertEqual(self.shadow.errors, 1)

    def test_global_cooldown_stops_without_new_request(self):
        self.cooldown[0] = 60
        self.shadow.tick(self.queries)
        self.assertTrue(self.shadow.stopped)
        self.assertEqual(len(self.calls), 1)

    def test_configuration_change_discards_incompatible_observations(self):
        self.shadow.observe("canonical", 10, URL, [self.item(1, 10)])
        changed = URL.replace("price_to=10", "price_to=20")
        self.shadow.tick({10: (10, changed)})
        self.shadow.observe("discovery", 10, URL, [self.item(1, 11)])
        self.assertFalse(self.shadow.records)

    def test_finite_experiment_stops_after_deadline(self):
        self.clock[0] = 1811
        self.shadow.tick(self.queries)
        self.assertTrue(self.shadow.stopped)

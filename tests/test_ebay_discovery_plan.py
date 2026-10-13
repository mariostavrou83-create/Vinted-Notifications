"""Shared collection reduces calls without mixing remote filters or local rules."""

import copy
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from test_ebay_monitor import EbayFixture, item

import dashboard_store
import ebay_discovery_plan as discovery
import ebay_monitor as monitor
import ebay_search_link as links
import ebay_store as store
import search_settings


def search(identity=1, *, word="skinny", maximum=2500, exclusions=(), brand="Zara"):
    config = links.parse_link(
        "https://www.ebay.co.uk/sch/i.html?_sacat=11554&LH_BIN=1&LH_PrefLoc=1&Brand="
        + brand,
        keywords=links.shared_keyword_query([word]) if word else "",
    )
    config.update(
        shared_alert_version=1,
        shared_keywords=[word] if word else [],
        max_price=maximum,
        include_shipping=True,
    )
    return {"id": identity, "ebay": config, "exclusions": list(exclusions)}


class DiscoveryPlanTests(unittest.TestCase):
    def test_keywords_and_budgets_share_one_fetch_with_unchanged_members(self):
        searches = [
            search(1),
            search(2, word="bootcut", maximum=1800, exclusions=["ripped"]),
        ]
        before = copy.deepcopy(searches)
        pools = discovery.plan(searches)
        self.assertEqual(len(pools), 1)
        self.assertEqual(searches, before)
        self.assertNotIn("q", pools[0].params)
        self.assertEqual(pools[0].members, before)
        self.assertIn("Brand:{Zara}", pools[0].params["aspect_filter"])

    def test_distinct_brands_sizes_conditions_and_listing_types_never_merge(self):
        original = search()
        for field, value in [
            ("aspects", {"Brand": ["Nike"]}),
            ("aspects", {"Brand": ["Zara"], "Size": ["8"]}),
            ("condition_ids", ["3000"]),
            ("buying", "auction"),
            ("uk_only", False),
            ("free_shipping", True),
            ("category", "63862"),
        ]:
            changed = copy.deepcopy(original)
            changed["id"] = 2
            changed["ebay"][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(len(discovery.plan([original, changed])), 2)

    def test_legacy_remote_keywords_are_kept_exact(self):
        legacy = search()
        legacy["ebay"].pop("shared_alert_version")
        legacy["ebay"].pop("shared_keywords")
        pools = discovery.plan([legacy, search(2, word="bootcut")])
        self.assertEqual(len(pools), 2)
        self.assertEqual(pools[0].params, monitor.search_params(legacy["ebay"]))

    def test_keyword_only_search_without_category_does_not_broaden(self):
        one = {
            "id": 1,
            "ebay": dict(
                store.DEFAULTS,
                shared_alert_version=1,
                shared_keywords=["skinny"],
                keywords="skinny",
            ),
            "exclusions": [],
        }
        self.assertEqual(discovery.plan([one])[0].params["q"], "skinny")

    def test_blank_keywords_can_share_a_category_pool(self):
        self.assertEqual(len(discovery.plan([search(1), search(2, word="")])), 1)

    def test_250_simulated_searches_in_10_remote_filter_sets_need_10_reads(self):
        searches = [
            search(
                i + 1,
                word="word" + str(i % 5),
                brand="Brand" + str(i // 25),
                maximum=1500 + (i % 5) * 200,
            )
            for i in range(250)
        ]
        pools = discovery.plan(searches)
        self.assertEqual(len(pools), 10)
        c = discovery.capacity(len(pools))
        self.assertEqual(c["search_calls_per_day"], 28800)
        self.assertEqual(c["required_approved_daily"], 32000)
        self.assertFalse(c["fits_allowance"])

    def test_quota_math_does_not_pretend_5000_supports_250_fast_searches(self):
        c = discovery.capacity(250)
        self.assertEqual(c["search_calls_per_day"], 720000)
        self.assertEqual(c["required_approved_daily"], 800000)
        self.assertEqual(c["minimum_interval_seconds"], 4800)

    def test_full_page_without_overlap_is_a_coverage_gap(self):
        records = [{"itemId": str(i)} for i in range(200)]
        self.assertFalse(discovery.coverage(records, {"old"}, seeded=True)["complete"])
        self.assertTrue(discovery.coverage(records, {"150"}, seeded=True)["complete"])
        self.assertTrue(discovery.coverage(records[:100], (), seeded=True)["complete"])

    def test_first_page_is_quiet_and_malformed_inventory_is_rejected(self):
        self.assertTrue(discovery.coverage([], (), seeded=False)["quiet_baseline"])
        for records in [[{}], [None], [{"itemId": "1"}] * 2]:
            self.assertFalse(discovery.coverage(records, (), seeded=True)["complete"])


class ShadowReadTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(spec=monitor.BrowseClient)
        self.reserve = Mock(return_value=True)
        self.pools = discovery.plan(
            [
                search(1),
                search(2, word="bootcut"),
                search(3, maximum=1800),
                search(4, exclusions=["ripped"]),
                search(5, word=""),
            ]
        )
        self.assertEqual(len(self.pools), 1)

    def jeans(self, number, title="Zara skinny jeans", **changes):
        return item(
            number,
            title=title,
            categories=[{"categoryId": "11554", "categoryName": "Jeans"}],
            **changes
        )

    def check(self, records, **options):
        self.api.search.return_value = (records, "")
        return discovery.shadow_check(
            self.pools[0], self.api, self.reserve, seeded=True, now=1020, **options
        )

    def test_one_read_matches_each_keyword_budget_and_exclusion_separately(self):
        result = self.check(
            [
                self.jeans(101),
                self.jeans(102, "Zara bootcut jeans"),
                self.jeans(103, "Zara ripped skinny jeans"),
            ]
        )
        self.api.search.assert_called_once()
        self.reserve.assert_called_once()
        rows = result["results"]
        self.assertEqual(rows["1"]["matches"], ["v1|101|0", "v1|103|0"])
        self.assertEqual(rows["2"]["matches"], ["v1|102|0"])
        self.assertEqual(rows["3"]["matches"], [])
        self.assertEqual(rows["4"]["matches"], ["v1|101|0"])
        self.assertEqual(len(rows["5"]["matches"]), 3)

    def test_unknown_description_is_pending_and_complete_negative_is_rejected(self):
        result = self.check(
            [
                self.jeans(101, "Zara straight jeans"),
                self.jeans(102, "Zara straight jeans", description="<p>Wide leg.</p>"),
            ]
        )
        row = result["results"]["1"]
        self.assertEqual(row["needs_description"], ["v1|101|0"])
        self.assertEqual(row["rejected"], ["v1|102|0"])

    def test_phrase_in_complete_description_can_match(self):
        pool = discovery.plan([search(1, word="fur hood")])[0]
        self.api.search.return_value = (
            [self.jeans(101, "Zara coat", description="<p>A fur hood.</p>")],
            "",
        )
        result = discovery.shadow_check(
            pool, self.api, self.reserve, seeded=True, now=1020
        )
        self.assertEqual(result["results"]["1"]["matches"], ["v1|101|0"])

    def test_quota_denial_makes_no_request(self):
        self.reserve.return_value = False
        result = discovery.shadow_check(self.pools[0], self.api, self.reserve)
        self.assertEqual(result["status"], "quota_deferred")
        self.api.search.assert_not_called()

    def test_public_client_is_not_used_as_a_way_around_the_api_contract(self):
        from ebay_public import PublicClient

        public_client = Mock(spec=PublicClient)
        with self.assertRaises(TypeError):
            discovery.shadow_check(self.pools[0], public_client, self.reserve)
        public_client.search.assert_not_called()
        self.reserve.assert_not_called()

    def test_first_check_sends_no_matches(self):
        self.api.search.return_value = ([self.jeans(101)], "")
        result = discovery.shadow_check(self.pools[0], self.api, self.reserve)
        self.assertEqual(result["status"], "quiet_baseline")
        self.assertEqual(result["results"], {})

    def test_saturation_never_returns_matches_as_if_coverage_was_complete(self):
        result = self.check([self.jeans(i + 1000) for i in range(200)])
        self.assertEqual(result["status"], "coverage_gap")
        self.assertEqual(result["results"], {})

    def test_mutated_pool_filters_are_rejected_before_reserving_or_fetching(self):
        self.pools[0].config["aspects"] = {"Brand": ["Nike"]}
        with self.assertRaisesRegex(ValueError, "filters differ"):
            discovery.shadow_check(self.pools[0], self.api, self.reserve)
        self.reserve.assert_not_called()
        self.api.search.assert_not_called()


class SharedDiscoveryIntegration(EbayFixture, unittest.TestCase):
    def test_one_pool_response_uses_real_current_baselines_rules_and_deduplication(
        self,
    ):
        identities = []
        for name, words, maximum, exclusions in [
            ("Skinny", "skinny", "25.00", "ripped"),
            ("Bootcut", "bootcut", "20.00", ""),
        ]:
            identity = dashboard_store.save_search(
                None,
                {
                    "shared_alert_version": "1",
                    "query_name": name,
                    "platform_mode": "ebay",
                    "query": "",
                    "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=11554&Brand=Zara&LH_BIN=1&LH_PrefLoc=1",
                    "shared_keywords": words,
                    "vinted_max_total": maximum,
                    "exclusions": exclusions,
                },
            )
            identities.append(identity)
        store.save_live_selection(identities)
        searches = [search_settings.get_search(identity) for identity in identities]
        pools = discovery.plan(searches)
        self.assertEqual(len(pools), 1)
        api = Mock(spec=monitor.BrowseClient)
        api.search.return_value = ([], "")
        reserve = Mock(return_value=True)
        baseline = discovery.shadow_check(pools[0], api, reserve, now=1000)
        self.assertEqual(baseline["status"], "quiet_baseline")
        for member in pools[0].members:
            self.assertEqual(self.snapshot(member, [], now=1000), 0)
        records = [
            item(
                101,
                title="Zara skinny jeans",
                description="",
                categories=[{"categoryId": "11554"}],
            ),
            item(
                102,
                title="Zara bootcut jeans",
                price="19.00",
                description="",
                categories=[{"categoryId": "11554"}],
            ),
            item(
                103,
                title="Zara bootcut jeans",
                price="10.00",
                description="",
                categories=[{"categoryId": "11554"}],
            ),
            item(
                104,
                title="Zara ripped skinny jeans",
                price="8.00",
                description="",
                categories=[{"categoryId": "11554"}],
            ),
        ]
        api.reset_mock()
        api.search.return_value = (records, "")
        result = discovery.shadow_check(pools[0], api, reserve, seeded=True, now=1020)
        self.assertEqual(result["results"][str(identities[0])]["matches"], ["v1|101|0"])
        self.assertEqual(result["results"][str(identities[1])]["matches"], ["v1|103|0"])
        api.search.assert_called_once()
        with closing(search_settings.connection()) as conn:
            before_buyer = list(conn.execute("SELECT * FROM vinted_buyer"))
        with patch(
            "requests.Session.request", side_effect=AssertionError("No live HTTP")
        ):
            for member in pools[0].members:
                self.snapshot(member, records, now=1020)
            for member in pools[0].members:
                self.assertEqual(self.snapshot(member, records, now=1030), 0)
        self.assertEqual(
            {row["item_id"] for row in self.outbox()}, {"ebay:101", "ebay:103"}
        )
        self.assertEqual(len(self.outbox()), 2)
        self.assertTrue(all(row["status"] == "pending" for row in self.outbox()))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                list(conn.execute("SELECT * FROM vinted_buyer")), before_buyer
            )

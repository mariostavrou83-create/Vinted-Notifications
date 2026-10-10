"""Shared search edits stay separate from saved buyers and durable payments.

All sessions, buyer IDs, requests and checkout results below are fictional and
use a temporary SQLite database. Nothing contacts a marketplace or Telegram.
"""

import json
import os
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE, checkout

import dashboard_store
import search_settings
import vinted_budget
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_keywords


def shared_form(**changes):
    return dict(
        {
            "shared_alert_version": "1",
            "query_name": "Fictional shared jackets",
            "platform_mode": "both",
            "query": "https://www.vinted.co.uk/catalog?brand_ids[]=179&size_ids[]=4&search_text=ignored",
            "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988&LH_BIN=1&_nkw=ignored",
            "shared_keywords": "fur, sherpa, fur hood",
            "exclusions": "teddy",
            "vinted_max_total": "20.00",
            "reminder": "Resale aim £60; inspect cuffs",
            "revision": "0",
        },
        **changes,
    )


def edit_search(query_id, **changes):
    current = search_settings.get_search(query_id)
    form = shared_form(
        query=current["query"],
        ebay_search_url=current["ebay"]["search_url"],
        platform_mode=current["platform_mode"],
        shared_keywords="\n".join(current["shared_keywords"]),
        exclusions="\n".join(current["exclusions"]),
        vinted_max_total=f"{current['vinted_max_total'] / 100:.2f}",
        reminder=current["reminder"],
        revision=str(current["revision"]),
    )
    form.update(changes)
    return dashboard_store.save_search(query_id, form)


class SharedBuyerStorageIsolation(DatabaseFixture, unittest.TestCase):
    protected = (
        "vinted_buyer",
        "vinted_buyer_access",
        "vinted_buyer_maintenance",
        "vinted_buy_attempts",
    )

    def setUp(self):
        super().setUp()
        session = buyer.encrypt(
            {"cookies": {"access_token_web": "fictional-access-for-offline-test"}}
        )
        pending = buyer.encrypt({"expires": 9999999999, "fictional": True})
        network = buyer.encrypt({"proxy": "http://offline.example:8080"})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,pending=?,verified_at=100,"
                "user_id='99',username='fictional-owner',enabled=1,"
                "max_total=2500,max_extra=500,browser_info=?,network=?,"
                "pickup_mode='nearest',preferred_card_last4='1234' WHERE id=1",
                (session, pending, json.dumps(DEVICE), network),
            )
            conn.execute(
                "UPDATE vinted_buyer_access SET checked=101,reason='connected',"
                "stage='identity',http_status=200,last_probe=100 WHERE id=1"
            )
            conn.execute(
                "UPDATE vinted_buyer_maintenance SET session_fingerprint="
                "'fictional-bound-seal',retry_at=900,blocked=0 WHERE id=1"
            )
            conn.executemany(
                "INSERT INTO vinted_buy_attempts "
                "(item_id,state,checkout_id,total,message,updated,buyer_id,transaction_id) "
                "VALUES (?,?,?,1900,'Keep this existing result',102,'99',?)",
                [
                    (str(9000 + i), state, f"offline-checkout-{i}", str(8000 + i))
                    for i, state in enumerate(
                        ("paid", "unknown", "needs_action", "paying", "payment_failed")
                    )
                ],
            )

    def snapshot(self):
        with closing(search_settings.connection()) as conn:
            return {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in self.protected
            }

    def test_create_edit_archive_and_restore_preserve_populated_buyer_and_payments(
        self,
    ):
        before = self.snapshot()
        old_searches = [search_settings.get_search(i) for i in range(1, 45)]
        query_id = dashboard_store.save_search(None, shared_form())
        self.assertEqual(before, self.snapshot())
        edit_search(
            query_id,
            shared_keywords="fur hood, sherpa lined",
            exclusions="teddy\nfaux fur",
            reminder="Resale aim £80; inspect lining",
            vinted_max_total="30.00",
        )
        self.assertEqual(before, self.snapshot())
        for action in ("pause", "resume", "archive", "restore"):
            current = search_settings.get_search(query_id)
            dashboard_store.change_state(query_id, action, current["revision"])
            self.assertEqual(before, self.snapshot(), action)
        self.assertEqual(
            old_searches, [search_settings.get_search(i) for i in range(1, 45)]
        )

    def test_platform_changes_never_reset_credentials_preferences_or_attempts(self):
        before = self.snapshot()
        query_id = dashboard_store.save_search(None, shared_form())
        for mode in ("ebay", "both", "vinted", "both"):
            edit_search(
                query_id,
                platform_mode=mode,
                query="https://www.vinted.co.uk/catalog?brand_ids[]=179&size_ids[]=4",
                ebay_search_url="https://www.ebay.co.uk/sch/i.html?_sacat=57988&LH_BIN=1",
            )
            self.assertEqual(before, self.snapshot(), mode)

    def test_invalid_and_stale_edits_are_atomic_and_keep_payment_guard(self):
        before = self.snapshot()
        query_id = dashboard_store.save_search(None, shared_form())
        saved = search_settings.get_search(query_id)
        for changes in (
            {"vinted_max_total": "NaN"},
            {"exclusions": "!!!"},
            {"revision": "0"},
            {"ebay_search_url": "https://evil.example/sch/i.html"},
        ):
            with self.assertRaises(ValueError):
                edit_search(query_id, **changes)
            self.assertEqual(saved, search_settings.get_search(query_id))
            self.assertEqual(before, self.snapshot())
        with self.assertRaises(buyer.BuyerError) as error:
            buying.guard_new_payment("123")
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")
        self.assertEqual(before, self.snapshot())

    def test_keyword_scheduler_ids_resolve_to_one_parent_total_budget(self):
        query_id = dashboard_store.save_search(None, shared_form())
        expanded = vinted_keywords.expand(search_settings.active_queries())
        alternatives = {
            scheduler_id: definition
            for scheduler_id, definition in expanded.items()
            if definition[0] == query_id
        }
        self.assertEqual(len(alternatives), 3)
        for scheduler_id, definition in alternatives.items():
            self.assertLess(scheduler_id, 0)
            self.assertEqual(definition[0], query_id)
            self.assertEqual(
                vinted_budget.purchase_limits({"query_id": definition[0]}),
                vinted_budget.PurchaseLimits(total_maximum=2000),
            )


class SharedCheckoutEdits(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "0"})
        env.start()
        self.addCleanup(env.stop)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',"
                "enabled=1,browser_info=? WHERE id=1",
                (
                    buyer.encrypt({"cookies": {"access_token_web": "fictional-test"}}),
                    json.dumps(DEVICE),
                ),
            )
        self.query_id = dashboard_store.save_search(None, shared_form())
        self.row = {
            "item_id": "123",
            "query_id": self.query_id,
            "url": "https://www.vinted.co.uk/items/123",
            "price": "15.00",
            "currency": "GBP",
        }
        self.client = Mock()
        self.client.listing_page.return_value = {
            "item": {
                "id": 123,
                "can_buy": True,
                "price": {"amount": "15.00", "currency_code": "GBP"},
                "user": {"id": 100},
            }
        }
        self.final = checkout()
        self.on_choices = lambda: None

        def response(method, path, body=None):
            if path == "/api/v2/conversations":
                return {"conversation": {"transaction": {"id": 456}}}
            if path == "/api/v2/purchases/checkout/build":
                return {"checkout": checkout()}
            if path == "/api/v2/purchases/checkout-123/checkout":
                self.on_choices()
                return {"checkout": self.final}
            if path.endswith("/payment"):
                return {"payment": {"status": "success"}}
            raise AssertionError(path)

        self.client.request.side_effect = response

    def run_buy(self):
        with patch.object(buyer, "connected_client", return_value=self.client):
            return buying.buy(self.row)

    def payments(self):
        return [
            call
            for call in self.client.request.call_args_list
            if call.args[1].endswith("/payment")
        ]

    def test_notes_and_exclusion_edit_keep_one_authorised_payment_and_saved_buyer(self):
        with closing(search_settings.connection()) as conn:
            before = tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
        self.on_choices = lambda: edit_search(
            self.query_id, reminder="Updated resale guide", exclusions="teddy\nfaux fur"
        )
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                before, tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            )

    def test_lowering_shared_budget_in_dashboard_stops_before_payment(self):
        self.on_choices = lambda: edit_search(self.query_id, vinted_max_total="18.00")
        result = self.run_buy()
        self.assertEqual(result["state"], "failed_before_payment")
        self.assertEqual(result["reason"], "total_over_budget")
        self.assertEqual(self.payments(), [])

    def test_raising_dashboard_budget_does_not_expand_an_already_started_tap(self):
        self.final = checkout("21.00")
        self.on_choices = lambda: edit_search(self.query_id, vinted_max_total="30.00")
        result = self.run_buy()
        self.assertEqual(result["reason"], "total_over_budget")
        self.assertEqual(self.payments(), [])

    def test_archiving_search_during_checkout_stops_before_payment(self):
        self.on_choices = lambda: dashboard_store.change_state(
            self.query_id,
            "archive",
            search_settings.get_search(self.query_id)["revision"],
        )
        result = self.run_buy()
        self.assertEqual(result["reason"], "search_inactive")
        self.assertEqual(self.payments(), [])

    def test_switching_search_to_ebay_only_stops_existing_vinted_tap(self):
        self.on_choices = lambda: edit_search(self.query_id, platform_mode="ebay")
        result = self.run_buy()
        self.assertEqual(result["reason"], "search_inactive")
        self.assertEqual(self.payments(), [])


if __name__ == "__main__":
    unittest.main()

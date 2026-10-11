"""Owner-attested local resolution never contacts Vinted or retries payment."""

import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from html.parser import HTMLParser
from unittest.mock import patch

from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import search_settings
import vinted_buying as buying
import vinted_owner_payment_resolution as resolution
from web_ui_plugin.web_ui import create_app


class OwnerResolution(DatabaseFixture, unittest.TestCase):
    def seed(self, identity="10320418087", state="unknown"):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES (?,?,?,?)",
                (identity, state, "Original uncertain payment", 123.25),
            )
        return buying.result(identity)

    def owner_audits(self):
        with closing(search_settings.connection()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='vinted_owner_purchase_resolutions'"
            ).fetchone()
            return (
                [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM vinted_owner_purchase_resolutions ORDER BY id"
                    )
                ]
                if exists
                else []
            )

    def test_owner_got_preserves_full_cost_and_bindings_without_marketplace_requests(
        self,
    ):
        identity = "10338220313"
        self.seed(identity, "needs_action")
        buying.record(
            identity,
            "needs_action",
            "Waiting for bank confirmation",
            checkout_id="original-checkout",
            buyer_id="99",
            transaction_id="456",
            total=1884,
            action_url="https://bank.example.test/private-action",
        )
        original = buying.result(identity)
        controls = buying.buyer.settings()
        with patch.object(buying.buyer, "connected_client") as network, patch.object(
            buying, "claim"
        ) as claim, patch.object(buying, "check_payment") as check:
            outcome = buying.resolve_owned(identity, str(original["updated"]), "got")
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(
            outcome["message"],
            "Owner confirmed they purchased this item; no payment resubmitted.",
        )
        self.assertNotIn("reconciliation_outcome", outcome)
        for key in ("checkout_id", "buyer_id", "transaction_id", "total", "action_url"):
            self.assertEqual(outcome[key], original[key])
        self.assertEqual(buying.buyer.settings(), controls)
        network.assert_not_called()
        claim.assert_not_called()
        check.assert_not_called()
        audits = self.owner_audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["operation"], "got")
        self.assertEqual(audits[0]["original_state"], "needs_action")
        self.assertEqual(audits[0]["original_updated"], original["updated"])
        self.assertEqual(audits[0]["resolved_at"], outcome["updated"])
        self.assertEqual(
            json.loads(audits[0]["original_attempt"]),
            {key: value for key, value in original.items() if key != "action_url"},
        )
        self.assertNotIn("private-action", audits[0]["original_attempt"])
        self.assertFalse(buying.claim({"item_id": identity}, recover_preparing=True))
        buying.guard_new_payment("123")

    def test_both_owner_decisions_accept_only_unknown_or_needs_action(self):
        for index, (decision, state) in enumerate(
            (
                ("got", "unknown"),
                ("got", "needs_action"),
                ("miss", "unknown"),
                ("miss", "needs_action"),
            ),
            start=1,
        ):
            identity = str(index)
            with self.subTest(decision=decision, state=state):
                self.seed(identity, state)
                outcome = buying.resolve_owned(identity, "123.25", decision)
                self.assertEqual(
                    outcome["state"], "paid" if decision == "got" else "payment_failed"
                )
                self.assertFalse(
                    buying.claim({"item_id": identity}, recover_preparing=True)
                )
        self.assertEqual(
            [row["operation"] for row in self.owner_audits()],
            ["got", "got", "miss", "miss"],
        )
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM vinted_payment_resolutions"
                ).fetchone()[0],
                2,
            )

    def test_same_owner_decision_is_idempotent_and_opposite_or_newer_state_refuses(
        self,
    ):
        for identity, decision, opposite in (
            ("123", "got", "miss"),
            ("456", "miss", "got"),
        ):
            with self.subTest(decision=decision):
                self.seed(identity)
                outcome = buying.resolve_owned(identity, "123.25", decision)
                audits = self.owner_audits()
                self.assertEqual(
                    buying.resolve_owned(identity, "123.25", decision), outcome
                )
                self.assertEqual(self.owner_audits(), audits)
                with self.assertRaises(buying.buyer.BuyerError) as error:
                    buying.resolve_owned(identity, "123.25", opposite)
                self.assertEqual(error.exception.reason, "resolution_not_allowed")
                buying.record(identity, outcome["state"], "A newer saved result")
                newer = buying.result(identity)
                with self.assertRaises(buying.buyer.BuyerError):
                    buying.resolve_owned(identity, "123.25", decision)
                self.assertEqual(buying.result(identity), newer)
                self.assertEqual(self.owner_audits(), audits)

    def test_owner_decisions_refuse_active_or_terminal_states(self):
        identity = "123"
        self.seed(identity)
        for state in (
            "paying",
            "preparing",
            "paid",
            "payment_failed",
            "failed_before_payment",
        ):
            buying.record(identity, state, "Keep this attempt")
            before = buying.result(identity)
            for decision in ("got", "miss"):
                with self.subTest(state=state, decision=decision), self.assertRaises(
                    buying.buyer.BuyerError
                ) as error:
                    buying.resolve_owned(identity, str(before["updated"]), decision)
                self.assertEqual(error.exception.reason, "resolution_not_allowed")
                self.assertEqual(buying.result(identity), before)
                self.assertEqual(self.owner_audits(), [])

    def test_stale_and_invalid_owner_decisions_leave_attempt_unchanged(self):
        identity = "123"
        self.seed(identity)
        buying.record(identity, "unknown", "Newer uncertain payment")
        before = buying.result(identity)
        with self.assertRaises(buying.buyer.BuyerError) as error:
            buying.resolve_owned(identity, "123.25", "got")
        self.assertEqual(error.exception.reason, "resolution_stale")
        for decision in (None, True, "paid", "failed", "", "GOT", ["got"]):
            with self.subTest(decision=decision), self.assertRaises(
                buying.buyer.BuyerError
            ):
                buying.resolve_owned(identity, str(before["updated"]), decision)
        for version in (
            None,
            before["updated"],
            "nan",
            "inf",
            "-1.0",
            "123",
            "123.25 ",
        ):
            with self.subTest(version=version), self.assertRaises(
                buying.buyer.BuyerError
            ):
                buying.resolve_owned(identity, version, "got")
        self.assertEqual(buying.result(identity), before)
        self.assertEqual(self.owner_audits(), [])

    def test_resolving_owned_item_does_not_clear_another_uncertain_attempt(self):
        self.seed("123")
        other = self.seed("456", "needs_action")
        buying.resolve_owned("123", "123.25", "got")
        with self.assertRaises(buying.buyer.BuyerError) as error:
            buying.guard_new_payment("789")
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")
        self.assertEqual(buying.result("456"), other)
        self.assertIsNone(buying.result("789"))

    def test_failed_owner_state_update_rolls_back_new_and_legacy_audits(self):
        for identity, decision in (("123", "got"), ("456", "miss")):
            self.seed(identity)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "CREATE TRIGGER reject_owner_state BEFORE UPDATE ON vinted_buy_attempts "
                "WHEN NEW.state IN ('paid','payment_failed') "
                "BEGIN SELECT RAISE(ABORT,'offline update failure'); END"
            )
        for identity, decision in (("123", "got"), ("456", "miss")):
            before = buying.result(identity)
            with self.subTest(decision=decision), self.assertRaises(
                sqlite3.IntegrityError
            ):
                buying.resolve_owned(identity, "123.25", decision)
            self.assertEqual(buying.result(identity), before)
            self.assertEqual(self.owner_audits(), [])
        with closing(search_settings.connection()) as conn:
            legacy = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='vinted_payment_resolutions'"
            ).fetchone()
            if legacy:
                self.assertEqual(
                    list(conn.execute("SELECT * FROM vinted_payment_resolutions")), []
                )

    def test_legacy_failed_audit_remains_compatible_without_rewriting_it(self):
        self.seed("123", "payment_failed")
        before = buying.result("123")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("""CREATE TABLE vinted_payment_resolutions (
                id INTEGER PRIMARY KEY,item_id TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation='owner_confirmed_failed'),
                original_state TEXT NOT NULL CHECK(original_state IN ('unknown','needs_action')),
                original_updated REAL NOT NULL,original_attempt TEXT NOT NULL,resolved_at REAL NOT NULL,
                UNIQUE(item_id,operation,original_updated))""")
            conn.execute(
                "INSERT INTO vinted_payment_resolutions VALUES (1,'123','owner_confirmed_failed',"
                "'unknown',100.0,'{}',123.25)"
            )
        self.assertEqual(buying.resolve_failed("123", "100.0"), before)
        self.assertEqual(self.owner_audits(), [])
        with closing(search_settings.connection()) as conn:
            audit = dict(
                conn.execute("SELECT * FROM vinted_payment_resolutions").fetchone()
            )
            self.assertEqual(audit["original_updated"], 100.0)
            self.assertEqual(audit["original_attempt"], "{}")

    def test_resolving_earlier_payment_permanently_closes_blocked_alert_without_network(
        self,
    ):
        self.seed("123", "needs_action")
        with patch.object(buying.buyer, "connected_client") as network:
            outcome = buying.resolve_owned("123", "123.25", "got", close_item_id="456")
            closed = buying.result("456")
            self.assertEqual(closed["state"], "closed")
            self.assertEqual(
                closed["message"],
                "This alert was closed after resolving an earlier payment. Use Autobuy on new items.",
            )
            self.assertNotIn("purchased", closed["message"])
            self.assertFalse(buying.claim({"item_id": "456"}, recover_preparing=True))
            self.seed("789", "unknown")
            row = {
                "item_id": "456",
                "query_id": 1,
                "url": "https://www.vinted.co.uk/items/456",
                "price": "15.00",
                "currency": "GBP",
            }
            with patch.object(
                buying.buyer, "settings", side_effect=AssertionError("No buyer setup")
            ):
                self.assertEqual(buying.buy(row), closed)
                token = buying.buyer.encrypt(
                    {
                        "purpose": "vinted_checkout_test_v1",
                        "checkout_id": "closed-checkout",
                        "item_id": "456",
                        "buyer_id": "99",
                        "transaction_id": "987",
                        "total": 1884,
                        "item_price": 1500,
                        "created": time.time(),
                    }
                ).decode("ascii")
                with patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "0"}):
                    self.assertEqual(buying.buy_checkout_quote(token), closed)
            self.assertEqual(
                buying.resolve_owned("123", "123.25", "got", close_item_id="456"),
                outcome,
            )
            self.assertEqual(buying.result("456"), closed)
            network.assert_not_called()
        self.assertEqual(len(self.owner_audits()), 1)

    def test_close_source_preserves_existing_payment_states_and_bindings(self):
        self.seed("123")
        buying.resolve_owned("123", "123.25", "miss")
        for index, state in enumerate(
            (
                "preparing",
                "failed_before_payment",
                "paying",
                "unknown",
                "needs_action",
                "paid",
                "payment_failed",
                "closed",
            ),
            start=400,
        ):
            identity = str(index)
            self.seed(identity, state)
            buying.record(
                identity,
                state,
                "Keep original source",
                checkout_id="source-checkout",
                total=2500,
                buyer_id="99",
                transaction_id="987",
                action_url="https://bank.example.test/private-action",
            )
            before = buying.result(identity)
            with self.subTest(state=state):
                buying.resolve_owned("123", "123.25", "miss", close_item_id=identity)
                after = buying.result(identity)
                if state in ("preparing", "failed_before_payment"):
                    self.assertEqual(after["state"], "closed")
                    for key in (
                        "checkout_id",
                        "total",
                        "buyer_id",
                        "transaction_id",
                        "action_url",
                    ):
                        self.assertEqual(after[key], before[key])
                else:
                    self.assertEqual(after, before)
                self.assertFalse(
                    buying.claim({"item_id": identity}, recover_preparing=True)
                )
        self.assertEqual(len(self.owner_audits()), 1)

    def test_invalid_or_stale_source_closure_changes_no_attempts(self):
        before = self.seed("123")
        for identity in ("123", 456, "", "invalid", "1" * 25):
            with self.subTest(identity=identity), self.assertRaises(
                buying.buyer.BuyerError
            ):
                buying.resolve_owned("123", "123.25", "got", close_item_id=identity)
            self.assertEqual(buying.result("123"), before)
            self.assertIsNone(buying.result("456"))
            self.assertEqual(self.owner_audits(), [])
        buying.record("123", "unknown", "Newer target result")
        before = buying.result("123")
        with self.assertRaises(buying.buyer.BuyerError):
            buying.resolve_owned("123", "123.25", "got", close_item_id="456")
        self.assertEqual(buying.result("123"), before)
        self.assertIsNone(buying.result("456"))
        self.assertEqual(self.owner_audits(), [])

    def test_source_closure_failure_rolls_back_resolution_and_audits(self):
        before = self.seed("123")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "CREATE TRIGGER reject_closed_source BEFORE INSERT ON vinted_buy_attempts "
                "WHEN NEW.state='closed' "
                "BEGIN SELECT RAISE(ABORT,'offline closure failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            buying.resolve_owned("123", "123.25", "miss", close_item_id="456")
        self.assertEqual(buying.result("123"), before)
        self.assertIsNone(buying.result("456"))
        self.assertEqual(self.owner_audits(), [])

    def test_explicit_once_only_marker_resolves_exact_item_without_network(self):
        self.seed()
        with patch.dict(
            os.environ,
            {"MSJ_OWNER_FAILED_PAYMENT_ON_START": "10320418087:owner_failed_20261010"},
        ), patch.object(
            buying.buyer, "connected_client", side_effect=AssertionError("No network")
        ):
            result = resolution.run_once()
            self.assertEqual(result["outcome"], "owner_attested")
            self.assertEqual(result["state"], "payment_failed")
            self.assertFalse(result["payment_submitted"])
            self.assertFalse(result["other_payments_unconfirmed"])
            before = buying.result("10320418087")
            self.assertIsNone(resolution.run_once())
            self.assertEqual(before, buying.result("10320418087"))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM vinted_payment_resolutions"
                ).fetchone()[0],
                1,
            )

    def test_paid_refuses_and_another_uncertain_attempt_is_preserved(self):
        self.seed(state="paid")
        before = buying.result("10320418087")
        with patch.dict(
            os.environ,
            {"MSJ_OWNER_FAILED_PAYMENT_ON_START": "10320418087:paid_refused"},
        ):
            self.assertEqual(resolution.run_once()["outcome"], "not_resolved")
        self.assertEqual(before, buying.result("10320418087"))
        self.seed("123", "unknown")
        self.seed("456", "unknown")
        with patch.dict(
            os.environ, {"MSJ_OWNER_FAILED_PAYMENT_ON_START": "123:owner_failed"}
        ):
            self.assertTrue(resolution.run_once()["other_payments_unconfirmed"])
        self.assertEqual(buying.result("456")["state"], "unknown")

    def test_missing_or_invalid_marker_does_nothing(self):
        for value in ("", "invalid", "10320418087", "10320418087:bad/marker"):
            with patch.dict(os.environ, {"MSJ_OWNER_FAILED_PAYMENT_ON_START": value}):
                self.assertIsNone(resolution.run_once())

    def owner_client(self):
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline password"),),
            )
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-csrf"
        return client

    def test_owner_route_requires_confirmation_csrf_and_current_version(self):
        self.seed()
        client = self.owner_client()
        form = {
            "csrf": "offline-csrf",
            "action": "buyer_payment_failed",
            "buyer_item_id": "10320418087",
            "buyer_attempt_updated": "123.25",
        }
        self.assertEqual(
            client.post("/connections", data=dict(form, csrf="wrong")).status_code, 400
        )
        client.post("/connections", data=form)
        self.assertEqual(buying.result("10320418087")["state"], "unknown")
        client.post(
            "/connections",
            data=dict(form, buyer_failed_confirm="yes", buyer_attempt_updated="123"),
        )
        self.assertEqual(buying.result("10320418087")["state"], "unknown")
        self.assertEqual(
            client.post(
                "/connections", data=dict(form, buyer_failed_confirm="yes")
            ).status_code,
            303,
        )
        self.assertEqual(buying.result("10320418087")["state"], "payment_failed")

    def test_dashboard_got_and_miss_require_csrf_and_explicit_confirmation(self):
        client = self.owner_client()
        for identity, decision in (("123", "got"), ("456", "miss")):
            before = self.seed(identity)
            form = {
                "csrf": "offline-csrf",
                "action": "buyer_payment_owned",
                "buyer_item_id": identity,
                "buyer_attempt_updated": "123.25",
                "buyer_purchase_decision": decision,
                "buyer_failed_confirm": "yes",
            }
            with self.subTest(decision=decision), patch.object(
                buying, "resolve_owned", wraps=buying.resolve_owned
            ) as resolve:
                self.assertEqual(
                    client.post(
                        "/connections", data=dict(form, csrf="wrong")
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    client.post(
                        "/connections", data=dict(form, buyer_failed_confirm="")
                    ).status_code,
                    303,
                )
                resolve.assert_not_called()
                self.assertEqual(buying.result(identity), before)
                self.assertEqual(self.owner_audits(), [])

    def test_dashboard_records_got_and_miss_without_network_or_another_purchase(self):
        client = self.owner_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET enabled=1,session=?,verified_at=1,user_id='99' WHERE id=1",
                (buying.buyer.encrypt({"cookies": {"access_token_web": "offline"}}),),
            )
        controls = buying.buyer.settings()
        for identity, decision, state in (
            ("123", "got", "paid"),
            ("456", "miss", "payment_failed"),
        ):
            self.seed(identity, "needs_action")
            with self.subTest(decision=decision), patch.object(
                buying.buyer, "connected_client"
            ) as network, patch.object(buying, "buy") as buy, patch.object(
                buying, "buy_checkout_quote"
            ) as quote, patch.object(
                buying, "check_payment"
            ) as check, patch.object(
                buying, "claim"
            ) as claim:
                response = client.post(
                    "/connections",
                    data={
                        "csrf": "offline-csrf",
                        "action": "buyer_payment_owned",
                        "buyer_item_id": identity,
                        "buyer_attempt_updated": "123.25",
                        "buyer_purchase_decision": decision,
                        "buyer_failed_confirm": "yes",
                    },
                )
                self.assertEqual(response.status_code, 303)
                outcome = buying.result(identity)
                self.assertEqual(outcome["state"], state)
                self.assertIn("Owner confirmed", outcome["message"])
                self.assertNotIn("Vinted confirmed", outcome["message"])
                self.assertNotIn("reconciliation_outcome", outcome)
                network.assert_not_called()
                buy.assert_not_called()
                quote.assert_not_called()
                check.assert_not_called()
                claim.assert_not_called()
            self.assertEqual(buying.buyer.settings(), controls)
            self.assertFalse(
                buying.claim({"item_id": identity}, recover_preparing=True)
            )
            page = client.get("/connections").get_data(as_text=True)
            self.assertIn(outcome["message"], page)
            self.assertNotIn(f'data-owner-failed-payment="{identity}"', page)
        self.assertEqual(
            [row["operation"] for row in self.owner_audits()], ["got", "miss"]
        )

    def test_dashboard_stale_decision_and_paying_attempt_refuse_without_change(self):
        client = self.owner_client()
        self.seed("123")
        buying.record("123", "unknown", "A newer unconfirmed result")
        before = buying.result("123")
        response = client.post(
            "/connections",
            data={
                "csrf": "offline-csrf",
                "action": "buyer_payment_owned",
                "buyer_item_id": "123",
                "buyer_attempt_updated": "123.25",
                "buyer_purchase_decision": "got",
                "buyer_failed_confirm": "yes",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(buying.result("123"), before)
        self.assertIn(
            "This payment attempt changed",
            client.get("/connections").get_data(as_text=True),
        )
        paying = self.seed("456", "paying")
        for decision in ("got", "miss"):
            with self.subTest(decision=decision):
                self.assertEqual(
                    client.post(
                        "/connections",
                        data={
                            "csrf": "offline-csrf",
                            "action": "buyer_payment_owned",
                            "buyer_item_id": "456",
                            "buyer_attempt_updated": "123.25",
                            "buyer_purchase_decision": decision,
                            "buyer_failed_confirm": "yes",
                        },
                    ).status_code,
                    303,
                )
                self.assertEqual(buying.result("456"), paying)
        self.assertEqual(self.owner_audits(), [])

    def test_dashboard_template_binds_both_buttons_to_exact_pending_attempt(self):
        class DecisionForms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms = {}
                self.current = None

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "form" and "data-owner-failed-payment" in values:
                    self.current = {"form": values, "inputs": {}, "buttons": []}
                    self.forms[values["data-owner-failed-payment"]] = self.current
                elif self.current is not None and tag == "input":
                    self.current["inputs"][values.get("name")] = values
                elif self.current is not None and tag == "button":
                    self.current["buttons"].append(values)

            def handle_endtag(self, tag):
                if tag == "form":
                    self.current = None

        for identity, state in (
            ("123", "unknown"),
            ("456", "needs_action"),
            ("789", "paying"),
            ("987", "paid"),
            ("654", "payment_failed"),
        ):
            self.seed(identity, state)
        parser = DecisionForms()
        parser.feed(self.owner_client().get("/connections").get_data(as_text=True))
        self.assertEqual(set(parser.forms), {"123", "456"})
        for identity, form in parser.forms.items():
            self.assertEqual(form["form"]["method"], "post")
            inputs = form["inputs"]
            self.assertEqual(inputs["csrf"]["value"], "offline-csrf")
            self.assertEqual(inputs["buyer_item_id"]["value"], identity)
            self.assertEqual(inputs["buyer_attempt_updated"]["value"], "123.25")
            self.assertEqual(inputs["action"]["value"], "buyer_payment_owned")
            self.assertIn("required", inputs["buyer_failed_confirm"])
            self.assertEqual(
                [(button["name"], button["value"]) for button in form["buttons"]],
                [
                    ("buyer_purchase_decision", "got"),
                    ("buyer_purchase_decision", "miss"),
                ],
            )

    def test_new_form_failure_retains_shared_inputs(self):
        client = self.owner_client()
        result = client.post(
            "/search/new",
            data={
                "csrf": "offline-csrf",
                "shared_alert_version": "1",
                "query_name": "New jacket",
                "platform_mode": "both",
                "query": "https://www.vinted.co.uk/catalog?brand_ids[]=111",
                "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988",
                "shared_keywords": "fur, sherpa",
                "reminder": "Resale <£60>",
                "exclusions": "teddy",
                "vinted_max_total": "",
            },
        )
        self.assertEqual(result.status_code, 200)
        content = result.get_data(as_text=True)
        self.assertIn("fur, sherpa", content)
        self.assertIn("Resale &lt;£60&gt;", content)
        self.assertIn("_sacat=57988", content)
        self.assertIn("teddy", content)
        self.assertNotIn('id="vinted_postage_estimate"', content)

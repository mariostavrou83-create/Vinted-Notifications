import os
import unittest
from unittest.mock import patch

import vinted_pickup_policy as policy

TARGET = {"name": "Example Shop", "postcode": "AA1 1AA", "code": "TEST-SHOP-42"}
RATE = {"carrier": {"name": "InPost", "code": "INPOST_UK"}}


class RequiredShopTests(unittest.TestCase):
    def test_operator_code_identifies_the_shop(self):
        for key in ("code", "external_code", "external_id"):
            with self.subTest(key=key):
                self.assertTrue(policy.matches({key: "test-shop-42"}, {}, TARGET))

    def test_local_id_needs_three_matching_properties(self):
        point = {"code": "different", "name": "InPost Shop - Example Shop", "address": {"postal_code": "aa11aa"}}
        self.assertTrue(policy.matches(point, RATE, TARGET))
        self.assertTrue(policy.matches(point, {}, TARGET, entry=RATE))
        self.assertFalse(policy.matches(point, {}, TARGET))
        self.assertFalse(policy.matches(dict(point, name="Different Shop"), RATE, TARGET))
        self.assertFalse(policy.matches(dict(point, name="Example Shop Annex"), RATE, TARGET))
        self.assertFalse(policy.matches(dict(point, address={}), RATE, TARGET))
        self.assertFalse(policy.matches(dict(point, address="AA1 1AB"), RATE, TARGET))
        self.assertTrue(policy.matches(dict(point, address="Test Road, AA1 1AA"), RATE, TARGET))

    def test_conflicts_beat_the_operator_code(self):
        point = {"code": TARGET["code"], "name": TARGET["name"]}
        for carrier in ("Relay", "DPD", "Royal Mail", "InPost home", "InPost / DPD"):
            with self.subTest(carrier=carrier):
                self.assertFalse(policy.matches(dict(point, carrier_name=carrier), RATE, TARGET))
        self.assertFalse(policy.matches(dict(point, postcode="AA1 1AB"), RATE, TARGET))
        self.assertFalse(policy.matches(dict(point, available=False), RATE, TARGET))
        self.assertFalse(policy.matches(point, dict(RATE, restriction="unavailable"), TARGET))
        self.assertFalse(policy.matches(point, RATE, TARGET, entry={"is_available": False}))

    def test_new_courier_can_qualify_only_at_the_exact_shop_and_postcode(self):
        rate = {"rate_uuid":"shipping-rate","carrier_name":"CitySprint"}
        point = {"name":"CitySprint Pickup Point - Example Shop","code":"CITY-77","postcode":"AA11AA"}
        self.assertEqual(policy.choice(point,rate,TARGET),(2,"citysprint"))
        self.assertIsNone(policy.choice(dict(point, postcode="AA11AB"),rate,TARGET))
        self.assertIsNone(policy.choice(dict(point, name="Example Shop West"),rate,TARGET))
        self.assertIsNone(policy.choice(point,dict(rate,service_type="home_delivery"),TARGET))
        self.assertIsNone(policy.choice({"code":TARGET["code"]},rate,TARGET))

    def test_incomplete_configuration_stays_restrictive(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(policy.required_point())
            os.environ["MSJ_INPOST_POINT_NAME"] = TARGET["name"]
            self.assertIsNotNone(policy.required_point())
            self.assertFalse(policy.matches({"code": TARGET["code"]}, RATE, policy.required_point()))

    def test_courier_trading_name_can_differ_at_exact_numbered_street_and_postcode(self):
        target = dict(TARGET, street_address="17 Example Road")
        rate = {"rate_uuid": "rate-a", "carrier_name": "DPD"}
        point = {"name": "Example Town Post Office", "postcode": "AA1 1AA"}
        for details in (
            {"address": "Example Town Post Office, 17 Example Rd., Sample Town, AA11AA"},
            {"address": {"house_number": 17, "street": "Example Road"}},
            {"address": {"address_line_1": "Example Town Post Office", "address_line_2": "17 EXAMPLE RD"}},
            {"name": "17 Example Rd"},
            {"name": "24/7 Locker", "street_address": "17 Example Road"},
        ):
            with self.subTest(details=details):
                self.assertEqual(policy.choice(dict(point, **details), rate, target), (2, "dpd"))
        self.assertIsNone(policy.choice({"name": "17 Example Road"}, rate, target))
        self.assertIsNone(policy.choice(dict(point, address={"street":"Example Road"}), rate, target))
        self.assertIsNone(policy.choice(dict(point, address={"house_number":17}), rate, target))
        self.assertIsNone(policy.choice(dict(point, address="17 Example Rd"), dict(rate, service_type="home_delivery"), target))

    def test_conflicting_addresses_override_all_location_matches(self):
        target = dict(TARGET, street_address="17 Example Road", alternate_name="Example Town Post Office")
        point = {"name": "Example Town Post Office", "code": TARGET["code"], "postcode": TARGET["postcode"]}
        for address in (
            "17A Example Road", "17–19 Example Road", "17/1 Example Road", "117 Example Road",
            "17 Example Road South", "17 Example Road, Unit 2", "Suite A, 17 Example Road",
            "17 Example Road, AA1 2AB", {"house_number":"19", "street_name":"Example Road"},
            {"house_number":17, "street_name":"Other Street"}, {"address_line":"17 Example Road", "flat_number":"B"},
            {"line1":"17 Example Road", "line2":"19 Other Street"},
        ):
            candidate = dict(point, address=address)
            with self.subTest(address=address):
                self.assertIsNone(policy.choice(candidate, RATE, target))
                self.assertTrue(policy.conflicts_with_target(target, candidate, fallback=True))

    def test_alternate_configured_name_matches_exactly_with_confirmed_postcode(self):
        target = dict(TARGET, alternate_name="Meadow Post Office")
        rate = {"rate_uuid":"native-rate","carrier_name":"DPD"}
        point = {"name":"DPD Pickup - Meadow Post Office","postcode":"AA11AA"}
        self.assertEqual(policy.choice(point, rate, target), (2, "dpd"))
        for changed in ({"postcode":""}, {"postcode":"AA11AB"}, {"name":"Meadow Post Office Annex"}):
            self.assertIsNone(policy.choice(dict(point, **changed), rate, target))

    def test_verified_address_can_include_the_known_unnumbered_parent_road(self):
        target = dict(TARGET, street_address="17 Example Parade", street_context="Market Park Road")
        rate = {"rate_uuid":"rate-a", "carrier_name":"DPD"}
        point = {"name":"Town Postal Counter", "postcode":"AA11AA"}
        for address in (
            "17 Example Parade, Market Park Road",
            "17 EXAMPLE PDE. MARKET PARK RD.",
            "17 Example Parade / Market Park Rd, AA1 1AA",
            {"house_number":17, "street_name":"Example Parade, Market Park Rd"},
            {"building_number":17, "street":"Example Pde Market Park Road"},
            {"street":"Market Park Road", "address_line_1":"17 Example Parade"},
        ):
            with self.subTest(address=address):
                self.assertEqual(policy.choice(dict(point, address=address), rate, target), (2, "dpd"))
        for address in ("Market Park Road", "17 Market Park Road", "17 Example Parade, Another Road",
                        "19 Example Parade Market Park Road", "17A Example Parade Market Park Rd",
                        "17–19 Example Parade, Market Park Road", "17 Example Parade, Unit A, Market Park Rd",
                        {"house_number":17, "street":"Market Park Road"}):
            with self.subTest(address=address):
                self.assertIsNone(policy.choice(dict(point, address=address), rate, target))
        code = dict(point, code=TARGET["code"], address="17 Example Parade, Market Park Rd")
        self.assertEqual(policy.choice(code, RATE, target), (0, "inpost"))
        self.assertIsNone(policy.choice(dict(code, address="17 Example Parade, Another Road"), RATE, target))
        self.assertIsNone(policy.choice(dict(code, address={"house_number":17, "street":"Market Park Road"}), RATE, target))

    def test_same_native_selection_can_echo_the_verified_address_alias_without_a_street(self):
        target = dict(TARGET, street_address="17 Example Street")
        verified = {"name":"Meadow Service Counter","carrier_name":"DPD","postcode":"AA11AA", "uuid":"point-1",
                    "code":"dpd-1","rate_uuid":"rate-a", "address":"17 Example St"}
        echo = {k:v for k,v in verified.items() if k not in ("postcode", "address")}
        check = {"fallback":True, "selected_carrier":"dpd", "selected_point":verified}
        self.assertFalse(policy.conflicts_with_target(target, echo, **check))
        for changed in ({"name":"Other Counter"}, {"postcode":"AA11AB"}, {"address":"17A Example Street"},
                        {"uuid":"point-2"}, {"code":"dpd-2"}, {"rate_uuid":"rate-b"}):
            self.assertTrue(policy.conflicts_with_target(target, dict(echo, **changed), **check))

    def test_pickup_configuration_adds_optional_private_location_fields_only_when_set(self):
        config = {"MSJ_INPOST_POINT_" + k.upper(): v for k,v in TARGET.items()}
        with patch.dict(os.environ, config, clear=True):
            self.assertEqual(policy.required_point(), TARGET)
            os.environ["MSJ_PICKUP_STREET_ADDRESS"] = "17 Example Street"
            os.environ["MSJ_PICKUP_STREET_CONTEXT"] = "Market Park Road"
            os.environ["MSJ_PICKUP_ALTERNATE_NAME"] = "Meadow Post Office"
            self.assertEqual(policy.required_point(), dict(TARGET, street_address="17 Example Street", street_context="Market Park Road",
                                                           alternate_name="Meadow Post Office"))

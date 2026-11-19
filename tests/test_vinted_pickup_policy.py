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

    def test_incomplete_configuration_stays_restrictive(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(policy.required_point())
            os.environ["MSJ_INPOST_POINT_NAME"] = TARGET["name"]
            self.assertIsNotNone(policy.required_point())
            self.assertFalse(policy.matches({"code": TARGET["code"]}, RATE, policy.required_point()))

import unittest

from trial_price_rules import field, product_mismatch, size_lookup_keys


def listing(title, size="XS / UK 4–6", brand="Next"):
    return {"title": title, "size": size, "brand": brand, "condition": "Very good"}


class GarmentLabels(unittest.TestCase):
    def test_actual_next_alert_repeated_uk_size_is_not_a_model(self):
        target = listing("Next Pink Beaded Cami Top Tagged UK 6")
        self.assertIsNone(product_mismatch(target, listing("Next Pink Beaded Cami Top")))
        self.assertIsNotNone(product_mismatch(target, listing("Next Pink Beaded Cami Top UK 4")))
        self.assertIsNotNone(product_mismatch(target, listing("Next Pink Beaded Cami Top UK 12")))
        self.assertIsNotNone(product_mismatch(target, listing("Next Pink Beaded Cami Top", size="S / UK 8-10")))

    def test_observed_y2k_and_2000s_are_the_same_era_for_matching_garments(self):
        target = listing("Hollister Y2K argyle cropped hoodie", "S / UK 8–10", "Hollister")
        for variant in ("argyle cropped hoodie", "2000s argyle cropped hoodie", "00s argyle cropped hoodie"):
            self.assertIsNone(product_mismatch(target, listing("Hollister " + variant, "S / UK 8–10", "Hollister")))
        self.assertIsNotNone(product_mismatch(target, listing("1990s argyle cropped hoodie", "S / UK 8–10", "Hollister")))

    def test_equivalent_label_spelling_keeps_size_and_numeric_model_strict(self):
        self.assertEqual(field(listing("", "XS / UK 4–6"), "size"),field(listing("", "Extra Small"), "size"))
        self.assertIn("xs uk 4 6",size_lookup_keys(listing("", "Extra Small")))
        self.assertNotEqual(field(listing("", "XS / UK 6-8"), "size"),field(listing("", "XS / UK 4-6"), "size"))
        self.assertNotEqual(field(listing("", "UK 4"), "size"),field(listing("", "UK 6"), "size"))
        self.assertIsNotNone(product_mismatch(listing("Levi's 501 jeans size UK 6"),
                                             listing("Levi's 511 jeans size UK 6")))
        self.assertIsNotNone(product_mismatch(listing("Next Y2K beaded cami top pack of 2"),
                                             listing("Next 2000s beaded cami top pack of 3")))
        self.assertIsNotNone(product_mismatch(listing("Next TX2000 computer"),
                                             listing("Next TX3000 computer")))

    def test_fur_collar_and_fur_outer_shell_are_different_even_with_legacy_rows(self):
        target = listing("Jane Norman fur collared coat", brand="Jane Norman")
        other = listing("Faux Fur Coat", brand="Jane Norman")
        self.assertIn("different constructions", product_mismatch(target, other))
        self.assertIn("different constructions", product_mismatch(other, target))
        self.assertIsNone(product_mismatch(listing("Jane Norman puffer coat with faux fur collar", brand="Jane Norman"),
                                           listing("Jane Norman faux fur collar puffer coat", brand="Jane Norman")))
        self.assertIsNone(product_mismatch(listing("faux fur collar", brand="Jane Norman"),
                                           listing("faux fur collar", brand="Jane Norman")))

    def test_own_catalogue_and_brand_projected_without_guessing_leaf_from_ancestors(self):
        from trial_market_hour import public_product
        p = public_product({"category_ids": [2,3], "catalog_id": 3, "brand": {"id": 19, "title": "Jane Norman"}})
        self.assertEqual(p, {"vinted_catalog_id": "3", "vinted_brand_id": "19"})
        self.assertEqual(public_product({"category_ids": [2,3], "category": {"id": 44}}), {})
        self.assertEqual(public_product({"catalog_id": 3, "catalogue_id": 4}), {})
        self.assertEqual(public_product({"category": {"id": 44, "is_leaf": True}}), {"vinted_catalog_id": "44"})
        target = dict(listing("Jane Norman brown coat",brand="Jane Norman"), **p)
        self.assertEqual(product_mismatch(target, dict(target, vinted_catalog_id="44")), "Different product category")
        self.assertEqual(product_mismatch(target, dict(target, vinted_brand_id="88")), "Different brand")
        self.assertIsNone(product_mismatch(target, target))

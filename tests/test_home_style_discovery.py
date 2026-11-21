"""Home discovery regression cases for the owner's open-label style searches."""
import unittest
from unittest.mock import patch

import vinted_home_feed
import vinted_style_photos
from test_vinted_home_feed import listing, native
from test_vinted_home_matching import item, search
from vinted_home_matching import match_candidate


def jacket(**changes):
    return search(**{
        "query": "https://www.vinted.co.uk/catalog?catalog[]=1037&size_ids[]=2,3,4&price_to=15",
        "shared_alert_version": 1,
        "shared_keywords": ["funnel neck", "cargo jacket", "high neck bomber"],
        "vinted_max_total": 1700,
        **changes,
    })


class HomeStyleDiscoveryTests(unittest.TestCase):
    def test_optional_attributes_do_not_erase_other_evidence(self):
        data = vinted_home_feed.parse_listing(listing(color2_id=None, material_id=None,
            seller_country_code="gb"), "123", 1000)["item"]
        self.assertEqual((data["size_id"],data["brand_id"],data["seller_country"]), ("3","88","GB"))

    def test_displayed_size_in_real_womens_breadcrumb_supplies_missing_id(self):
        source = listing(catalog_id=None, size_id=None).replace(
            "/catalog/222-tops?referrer=item-crumbs", "https://www.vinted.co.uk/catalog/1037-jackets-and-coats")
        data = vinted_home_feed.parse_listing(source, "123", 1000)["item"]
        data.update(title="Fitted high collar zip front", description="")
        self.assertEqual(match_candidate(data, [jacket()])["style_evidence"], "scraper_style")
        self.assertIsNone(match_candidate(dict(data,size_id="7"), [jacket()]))
        self.assertIsNone(match_candidate(dict(data,conflicting_fields=["size_id"]), [jacket()]))

    def test_public_stock_and_empty_description_are_valid_and_sold_wins(self):
        stock = '<link itemprop="availability" href="https://schema.org/InStock">'
        sold = '<span data-testid="item-status--sold">Sold</span>'
        source = listing(extra=stock).replace(native(), "").replace(
            'description">Fitted cotton top in very good condition.', 'description">')
        result = vinted_home_feed.parse_listing(source, "123", 1000)
        self.assertTrue(result["item"]["description_complete"])
        self.assertEqual(result["item"]["description"], "")
        for blocked in [source.replace(stock,stock+sold), listing(extra=stock+sold),
                        source.replace("</main>", '</main><script type="application/json" id="__NEXT_DATA__">{"id":"123","seller_id":37,"is_reserved":true}</script>')]:
            self.assertIsNone(vinted_home_feed.parse_listing(blocked, "123", 1000)["item"])

    def test_ralph_literal_features_survive_equivalent_phrase_normalisation(self):
        for title, term in [("Full zip pinstripe jacket","zip"), ("Full button cable knit cardigan","cable")]:
            self.assertEqual(match_candidate(item(title=title), [search(vinted_keywords=[term])])["style_evidence"],"literal")
        self.assertIsNone(match_candidate(item(title="Not full zip"),[search(vinted_keywords=["zip"])]))

    def test_photo_can_only_supply_style_for_an_eligible_open_label_garment(self):
        plain = item(title="Women's jacket", description="", brand_id=None, size_id=None, size_title="S / UK 8-10")
        eligible = set()
        self.assertIsNone(match_candidate(plain,[jacket()],eligible=eligible))
        self.assertEqual(eligible,{"utility_jacket"})
        self.assertEqual(match_candidate(plain,[jacket()],photo_styles=eligible)["style_evidence"],"scraper_photo")
        for changes in [{"size_title":"XL"},{"price":{"amount":"24","currency_code":"GBP"}},
                        {"description":"teddy lining"},{"category_ids":["999"]}]:
            offered=set()
            self.assertIsNone(match_candidate(dict(plain,**changes),[jacket()],photo_styles=eligible,eligible=offered))
            self.assertFalse(offered)
        self.assertIsNone(match_candidate(plain, [search()], photo_styles=eligible))

    def test_y2k_and_outfit_tags_do_not_establish_cut(self):
        for text in ["Y2K vintage jacket","Brown jacket styled with a cropped high collar coat",
                     "Jacket. #cropped #high #collar","Jacket. Keywords: fitted high collar zip front"]:
            self.assertIsNone(match_candidate(item(title=text,description=""), [jacket()]))

    def test_untrained_profiles_never_fetch_cover(self):
        with patch("alert_images.download_photo") as download:
            self.assertEqual(vinted_style_photos.photo_matches("https://images1.vinted.net/a.jpg",{"cowl_top"}),((),False))
            download.assert_not_called()

    def test_real_quantized_guides_have_fixed_dimensions(self):
        import base64
        for profile in vinted_style_photos.profiles().values():
            for source in ("positive","negative","examples"):
                data=base64.b64decode(profile[source], validate=True)
                self.assertGreaterEqual(len(data), 2*512)
                self.assertEqual(len(data)%512,0)

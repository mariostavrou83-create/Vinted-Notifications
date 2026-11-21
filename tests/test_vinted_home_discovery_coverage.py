"""Realistic Home cards, the one-minute read cap, and independent owner guides."""
import unittest
from contextlib import closing
from unittest.mock import patch

import db
import vinted_home_controls
import search_settings
import vinted_home_feed
import vinted_home_guides
import vinted_home_monitor
from test_search_controls import DatabaseFixture
from test_home_style_discovery import jacket
from test_vinted_home_feed import listing, native
from test_vinted_home_matching import item, search
from vinted_home_matching import match_candidate


class RecognitionTests(unittest.TestCase):
    def test_cut_across_fields_and_natural_descriptions(self):
        garment = item(title="Brown jacket", description="Standing collar. Shaped waist.")
        self.assertEqual(match_candidate(garment, [jacket()])["reason"], "scraper_look")
        for text in ("Standing collar. Without a shaped waist.", "Shaped waist. #standing collar",
                     "Shaped waist. Styled with a standing collar"):
            self.assertIsNone(match_candidate(dict(garment, description=text), [jacket()]))
        self.assertIsNone(match_candidate(dict(garment,brand_title="SHEIN"), [jacket()]))
        self.assertIsNone(match_candidate(dict(garment,brand_title="New Look"), [jacket()]))

    def test_priority_ralph_neighbour_category_stays_specific(self):
        target = search(vinted_keywords=["pinstripe","pin striped","striped zip","zip"])
        garment = item(title="Ribbed jumper", description="A full length zip runs down the front.",
                       category_ids=["1904","13"])
        self.assertEqual(match_candidate(garment,[target])["reason"], "scraper_look")
        for changes in ({"brand_id":"99"}, {"category_ids":["5","1859"]}, {"size_id":"5"},
                        {"title":"Ribbed jumper size L (12/14)"}, {"description":"A quarter-zip front."},
                        {"price":{"amount":"40","currency_code":"GBP"}}):
            self.assertIsNone(match_candidate(dict(garment,**changes),[target]))
        self.assertTrue(match_candidate(dict(garment,title="Ribbed jumper size UK 12-14"),[target]))

    def test_target_timestamp_recovers_genuine_third_minute(self):
        body = listing(age="2 minutes ago",created_at_ts=850)
        verified = vinted_home_feed.parse_listing(body,"123",1000)["age"]
        self.assertEqual((verified["max_age_seconds"],verified["evidence"]),(150,"target_upload_timestamp"))
        for other in (listing(age="2 minutes ago"), listing(age="2 minutes ago",created_at_ts=600),
                      listing(age="2 minutes ago",extra=native(id="999",created_at_ts=850)),
                      listing(age="2 minutes ago",created_at=850,created_at_ts=830)):
            self.assertEqual(vinted_home_feed.parse_listing(other,"123",1000)["age"]["max_age_seconds"],180)
        body = listing(age="2 minutes ago").replace('itemprop="upload_date"', 'itemprop="upload_date" content="850"')
        self.assertEqual(vinted_home_feed.listing_age(body,"123",1000)["max_age_seconds"],150)


class HomeGuideTests(DatabaseFixture, unittest.TestCase):
    def test_independent_guide_has_own_budget_and_photos_and_no_catalogue_monitor(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO dashboard_media VALUES ('example',X'01',NULL,0)")
            for candidate in vinted_home_guides.GUIDES:
                conn.execute("INSERT INTO queries(id,query,query_name) VALUES (?,?,?)",
                             (candidate.example_search,'https://www.vinted.co.uk/catalog',candidate.example_name))
                conn.execute("INSERT INTO search_dashboard(query_id,reference_id) VALUES (?,'example')", (candidate.example_search,))
                conn.execute("INSERT INTO search_reference_photos VALUES (?,0,'example')", (candidate.example_search,))
        saved_ids = {r[0] for r in search_settings.active_queries()}
        self.assertEqual(vinted_home_guides.install(),4)
        self.assertEqual(vinted_home_guides.install(),0)
        self.assertEqual({r[0] for r in search_settings.active_queries()},saved_ids)
        self.assertEqual({r[0] for r in db.get_queries()},saved_ids)
        extra = {r[0] for r in search_settings.active_queries(include_home=True)} - saved_ids
        records = {s["home_style"]:s for s in (search_settings.get_search(qid) for qid in extra)}
        self.assertEqual(len(records),4)
        styles = [
            ("layered_henley","12","Henley top with vest","Double-layer fitted jersey."),
            ("fur_hood_bomber","1037","Cropped bomber","Fur trimmed hood. Ribbed waistband."),
            ("beaded_evening_top","12","Halter blouse","Beaded detail on silk."),
            ("foldover_flares","9","Yoga flares","Fold over printed waistband."),
        ]
        for style, cat, title, description in styles:
            target = records[style]
            self.assertFalse(target["ebay_enabled"])
            self.assertEqual(target["reference_id"],"example")
            garment = item(title=title,description=description,brand_title="Superdry",brand_id="42",
                           category_ids=["1904",cat],condition="Very good",price={"amount":"5","currency_code":"GBP"})
            self.assertEqual(match_candidate(garment,[target])["home_style"],style)
            self.assertTrue(match_candidate(dict(garment,description=description+" No holes."),[target]))
            for change in ({"condition":"Satisfactory"},{"brand_title":"SHEIN"},{"size_id":"6"},
                           {"description":"Stained. "+description},{"description":"","title":"Y2K stylish"},
                           {"price":{"amount":"22","currency_code":"GBP"}}, {"brand_title":"Zara"}):
                self.assertIsNone(match_candidate(dict(garment,**change),[target]))
        henley = records["layered_henley"]
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE search_dashboard SET paused=1 WHERE query_id=?",(henley["id"],))
        self.assertNotIn(henley["id"],{q[0] for q in search_settings.active_queries(include_home=True)})
        self.assertEqual(vinted_home_guides.install(),0)

    def test_repeat_heavy_feed_checks_newest_cards_across_all_styles(self):
        worker = vinted_home_monitor.Monitor(clock=lambda:1000,fetch=lambda *args:("home",1000))
        cards = [{"id":str(v),"title":"Simple title"} for v in range(100,120)]
        cards[15].update(brand_title="Polo Ralph Lauren",title="Full zip ribbed cardigan")
        cards[16]["size_title"]="13 years / 158 cm"
        worker.examined.update({str(v):999 for v in range(100,115)})
        worker.page_reads["seller"].append(1000)
        with patch.object(vinted_home_monitor,"parse_home",return_value={"reason":"ok","items":cards}):
            worker.home("seller",1000)
        self.assertEqual(worker.next_home["seller"],1032)
        self.assertEqual([x["id"] for x in worker.pending["seller"]],["119","118","117"])
        self.assertEqual(worker.counts["seller"]["card_screened"],0)

    def test_budget_is_enforced_across_overlapping_cycles(self):
        now=[1000]
        calls=[]
        def fetch(role,url):
            calls.append((role,now[0]))
            return "home",now[0]
        worker=vinted_home_monitor.Monitor(clock=lambda:now[0],fetch=fetch)
        vinted_home_controls.configure("1")
        def page(_):
            return {"reason":"ok","items":[{"id":str(now[0]*20+i),"title":"Brown jacket","url":"listing"} for i in range(15)]}
        with patch.object(vinted_home_monitor,"_account_ready",return_value=None), \
             patch.object(vinted_home_monitor,"active_searches",return_value=[search()]), \
             patch.object(vinted_home_monitor,"parse_home",side_effect=page), \
             patch.object(vinted_home_monitor,"parse_listing",return_value={"age":{"verified":False}}):
            for instant in range(1000,1150):
                now[0]=instant
                worker.tick()
        self.assertEqual({role for role, _ in calls}, {"seller"})
        reads = [at for _,at in calls]
        self.assertGreaterEqual(len(reads),8)
        for at in reads:
            self.assertLessEqual(sum(at-60 < r <= at for r in reads),7)

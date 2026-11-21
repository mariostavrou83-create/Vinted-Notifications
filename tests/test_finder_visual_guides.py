import io
import json
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from test_dashboard import photo_bytes
from test_search_controls import DatabaseFixture
from test_home_style_discovery import jacket
import dashboard_store
import finder_buy_guides
import search_settings
from shared_matching import keyword_evidence


def pack(entries, image=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("guides.json", json.dumps(entries))
        if image:
            z.writestr("1.jpg", image)
    stream.seek(0)
    return stream


class BuyingGuideTests(DatabaseFixture, unittest.TestCase):
    def test_packaged_guides_fill_active_gaps_and_preserve_owner_edits(self):
        owner = {"id": 1, "name": "Owner guide", "guidance": "My limit is £7"}
        self.assertEqual(finder_buy_guides.save_pack(pack([owner])), 1)
        entries = [
            dict(owner, guidance="Packaged default"),
            dict(owner, id=2, guidance="Max £10 delivered"),
            dict(owner, id=999),
        ]
        path = Path(self.temp.name) / "release.zip"
        path.write_bytes(pack(entries).getvalue())
        self.assertEqual(finder_buy_guides.apply_release(path), 1)
        self.assertEqual(finder_buy_guides.apply_release(path), 0)
        self.assertEqual(search_settings.get_search(1)["buying_advice"], "My limit is £7")
        self.assertEqual(search_settings.get_search(2)["buying_advice"], "Max £10 delivered")
        self.assertEqual(finder_buy_guides.coverage(), (44, 2, 0, 0, 0, []))

    def test_fallback_guide_preserves_owner_search_and_is_idempotent(self):
        before = search_settings.get_search(1)
        guide = {"id":1,"name":"Vintage jacket","guidance":"Max £8 all-in","photo":"1.jpg","style_alert_cap":1100}
        photo = dashboard_store.normalize_photo(io.BytesIO(photo_bytes()))
        for _ in range(2):
            self.assertEqual(finder_buy_guides.save_pack(pack([guide],photo)),1)
        after = search_settings.get_search(1)
        self.assertTrue(after["reference_id"])
        self.assertEqual(after["buying_advice"],"Max £8 all-in")
        self.assertEqual(after["style_alert_cap"],1100)
        from vinted_home_matching import match_candidate
        from test_vinted_home_matching import item
        self.assertIsNone(match_candidate(item(title="Fitted cropped funnel neck bomber",price={"amount":"9.97","currency_code":"GBP"}),[jacket(style_alert_cap=800)]))
        for key in ["revision","query","exclusions","last_item","query_name","paused","archived"]:
            self.assertEqual(before[key],after[key])
        self.assertEqual(dashboard_store.get_media(after["reference_id"])["image"],photo)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM finder_buy_guides").fetchone()[0],1)
            self.assertFalse(conn.execute("SELECT reference_id FROM search_dashboard WHERE query_id=1").fetchone()[0] if conn.execute("SELECT 1 FROM search_dashboard WHERE query_id=1").fetchone() else None)
        with self.assertRaises(ValueError):
            finder_buy_guides.save_pack(pack([dict(guide,photo=None,guidance="Different"),dict(guide,id=999999,photo=None)]))
        self.assertEqual(search_settings.get_search(1)["buying_advice"],"Max £8 all-in")

    def test_label_and_construction_gate_only_scoped_styles(self):
        search=jacket()
        def proof(title,brand,description=""):
            return keyword_evidence(search,title,description,brand=brand,description_complete=True)["state"]
        self.assertEqual(proof("Funnel neck cropped bomber","Primark"),"absent")
        self.assertEqual(proof("Funnel neck cropped bomber","Zara"),"absent")
        self.assertEqual(proof("Original 2000s cropped funnel neck bomber","Zara"),"matched")
        self.assertEqual(proof("Funnel neck jacket","Other"),"absent")
        self.assertEqual(proof("Funnel neck military jacket with waist buckle","Other"),"matched")

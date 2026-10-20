"""Full 128-search release, transaction guards and category/recall fixtures."""

import base64
import hashlib
import json
import os
import sqlite3
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_search_controls import DatabaseFixture

import ebay_store
import search_audit_release as release
import search_settings
import vinted_budget
import vinted_keywords
from shared_matching import keyword_evidence


def manifest_fixture():
    """Synthetic owner data; the real 128 definitions are private config."""
    examples = {
        142: ("top; mesh; graphic; off shoulder; long sleeve", "oversized"),
        162: ("coat; snood; thumb holes; funnel; puffer", "quarter zip; pullover"),
        168: ("top; graphic; printed; mesh; 3/4 sleeves", "oversized"),
        177: ("graphic; long sleeve; baby tee; mesh", "t angie"),
        180: (
            "military jacket; fitted puffer; cargo jacket",
            "swing coat; teddy; fleece jacket",
        ),
        195: ("fur; sherpa lined; fitted puffer", "teddy; aviator; fleece jacket"),
        200: ("gilet; bodywarmer", "fleece gilet"),
        229: ("silk halter; ruched silk; strapless silk", "jersey dress; shirt dress"),
        257: ("denim knit; knit panel; denim zip", "trucker; button up"),
        260: (
            "ribbed hem; ribbed waist; fur hood bomber",
            "everest; fuji; fleece jacket",
        ),
    }
    rows = []
    for identity in range(142, 270):
        url = f"https://www.vinted.co.uk/catalog?brand_ids[]={identity}&catalog[]=1037&price_to=25.00"
        words, blocks = examples.get(
            identity, ("fitted bomber; fur hood", "teddy; fleece jacket")
        )
        words = words.split("; ")
        blocks = blocks.split("; ")
        expected = {
            "name": f"Synthetic search {identity}",
            "url": url,
            "keywords": ["old keyword"],
            "exclusions": ["old block"],
            "maximum": 2500,
            "folder_id": 1,
            "photo_count": identity % 8 + 1,
            "reminder": "Synthetic resale notes",
        }
        rows.append(
            {
                "id": identity,
                "expected": expected,
                "url": url + "&size_ids[]=2&size_ids[]=3&size_ids[]=4",
                "ebay_url": "https://www.ebay.co.uk/sch/i.html?Brand=Test&Size=XS%7CS%7CM&_sacat=63862",
                "keywords": words,
                "exclusions": blocks,
                "reminder": "Updated synthetic notes",
            }
        )
    return {"id": release.RELEASE, "old_ids": list(range(3, 43)), "rows": rows}


class AuditedRelease(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.manifest = manifest_fixture()
        raw = json.dumps(self.manifest).encode()
        encoded = base64.b64encode(zlib.compress(raw)).decode()
        env = {
            "MSJ_SEARCH_AUDIT_RELEASE": release.RELEASE,
            "MSJ_SEARCH_AUDIT_SHA256": hashlib.sha256(raw).hexdigest(),
        }
        env.update(
            {
                f"MSJ_SEARCH_AUDIT_CONFIG_{i}": encoded[i * 25000 : (i + 1) * 25000]
                for i in range(4)
            }
        )
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO search_dashboard(query_id,paused,archived) SELECT id,1,1 FROM queries"
            )
            for identity in self.manifest["old_ids"]:
                conn.execute(
                    "INSERT OR IGNORE INTO queries(id,query,query_name,last_item) VALUES (?,'https://www.vinted.co.uk/catalog?brand_ids[]=93','OLD',100)",
                    (identity,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO search_dashboard(query_id,paused,archived) VALUES (?,0,0)",
                    (identity,),
                )
            for row in self.manifest["rows"]:
                i, e = row["id"], row["expected"]
                conn.execute(
                    "INSERT OR IGNORE INTO search_folders(id,name) VALUES (?,?)",
                    (e["folder_id"], "Folder " + str(e["folder_id"])),
                )
                conn.execute(
                    "INSERT INTO queries(id,query,query_name,last_item) VALUES (?,?,?,100)",
                    (i, e["url"], e["name"]),
                )
                conn.execute(
                    "INSERT INTO search_dashboard(query_id,paused) VALUES (?,1)", (i,)
                )
                conn.execute(
                    "INSERT INTO search_preferences VALUES (?,?,?)",
                    (i, e["reminder"], json.dumps(e["exclusions"])),
                )
                conn.execute(
                    "INSERT INTO search_buying_guide(query_id,folder_id) VALUES (?,?)",
                    (i, e["folder_id"]),
                )
                conn.execute(
                    "INSERT INTO vinted_search_budgets VALUES (?,?,220)",
                    (i, e["maximum"]),
                )
                config = dict(
                    ebay_store.DEFAULTS,
                    shared_alert_version=1,
                    shared_keywords=e["keywords"],
                    max_price=e["maximum"],
                )
                conn.execute(
                    "INSERT INTO search_platforms VALUES (?,1,1,?,0)",
                    (i, json.dumps(config)),
                )
                vinted_keywords.save(conn, i, e["url"], e["keywords"])
                for pos in range(e["photo_count"]):
                    media = f"media-{i}-{pos}"
                    conn.execute(
                        "INSERT INTO dashboard_media VALUES (?,?,NULL,0)",
                        (media, b"test-photo"),
                    )
                    conn.execute(
                        "INSERT INTO search_reference_photos VALUES (?,?,?)",
                        (i, pos, media),
                    )
                conn.execute(
                    "UPDATE search_dashboard SET reference_id=? WHERE query_id=?",
                    (f"media-{i}-0", i),
                )

    def dump(self):
        with closing(search_settings.connection()) as conn:
            return list(conn.iterdump())

    def test_full_release_keeps_old_limits_references_and_buying_state(self):
        old = [search_settings.get_search(i) for i in self.manifest["old_ids"]]
        with closing(search_settings.connection()) as conn:
            photos = [
                tuple(r) for r in conn.execute("SELECT * FROM search_reference_photos")
            ]
            buys = [tuple(r) for r in conn.execute("SELECT * FROM vinted_buy_attempts")]
        result = release.apply_once(now=12345)
        self.assertEqual(
            (result["new_active"], result["old_active"], result["ebay_live"]),
            (128, 40, 0),
        )
        self.assertTrue(Path(result["backup"]).is_file())
        with closing(sqlite3.connect(result["backup"])) as backup:
            self.assertEqual(
                backup.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                backup.execute("SELECT COUNT(*) FROM search_audit_runs").fetchone()[0],
                0,
            )
        self.assertEqual(
            old, [search_settings.get_search(i) for i in self.manifest["old_ids"]]
        )
        for row in self.manifest["rows"]:
            s = search_settings.get_search(row["id"])
            self.assertFalse(s["paused"])
            self.assertFalse(s["archived"])
            self.assertEqual(s["vinted_max_total"], row["expected"]["maximum"])
            self.assertEqual(
                parse_qs(urlsplit(s["query"]).query)["size_ids[]"], ["2", "3", "4"]
            )
            self.assertTrue(all(not v["primed"] for v in s["vinted_variants"]))
            self.assertEqual(s["shared_keywords"], row["keywords"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                photos,
                [
                    tuple(r)
                    for r in conn.execute("SELECT * FROM search_reference_photos")
                ],
            )
            self.assertEqual(
                buys,
                [tuple(r) for r in conn.execute("SELECT * FROM vinted_buy_attempts")],
            )
            self.assertEqual(ebay_store.live_ids(conn), [])
        before = self.dump()
        self.assertIsNone(release.apply_once(now=99999))
        self.assertEqual(self.dump(), before)

    def test_owner_edit_aborts_entire_release_before_any_search_changes(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_search_budgets SET max_total=999 WHERE query_id=269"
            )
        before = self.dump()
        with self.assertRaisesRegex(RuntimeError, "#269 changed in maximum"):
            release.apply_once()
        self.assertEqual(self.dump(), before)

    def test_guard_failure_during_write_rolls_back_all_128_and_run_marker(self):
        before = self.dump()
        with patch.object(
            vinted_keywords, "save", side_effect=RuntimeError("write failure")
        ), self.assertRaisesRegex(RuntimeError, "write failure"):
            release.apply_once()
        self.assertEqual(self.dump(), before)

    def test_disabled_release_makes_no_changes(self):
        before = self.dump()
        with patch.dict(os.environ, {"MSJ_SEARCH_AUDIT_RELEASE": ""}):
            self.assertIsNone(release.apply_once())
        self.assertEqual(self.dump(), before)

    def test_wrong_private_configuration_hash_stops_before_any_write(self):
        before = self.dump()
        with patch.dict(
            os.environ, {"MSJ_SEARCH_AUDIT_SHA256": "wrong"}
        ), self.assertRaisesRegex(RuntimeError, "checksum"):
            release.apply_once()
        self.assertEqual(self.dump(), before)

    def test_decompression_is_bounded_before_database_access(self):
        encoded = base64.b64encode(zlib.compress(b"x" * 600000)).decode()
        before = self.dump()
        env = {
            f"MSJ_SEARCH_AUDIT_CONFIG_{i}": encoded if i == 0 else "" for i in range(4)
        }
        with patch.dict(os.environ, env), self.assertRaisesRegex(
            RuntimeError, "bounded"
        ):
            release.apply_once()
        self.assertEqual(self.dump(), before)

    def test_actual_v25_upgrade_preserves_references_and_makes_audit_schema(self):
        with closing(search_settings.connection()) as conn, conn:
            photos = [
                tuple(r) for r in conn.execute("SELECT * FROM search_reference_photos")
            ]
            conn.execute("DROP TABLE search_match_log")
            conn.execute("DROP TABLE search_audit_members")
            conn.execute("DROP TABLE search_audit_runs")
            conn.execute(
                "UPDATE parameters SET value='25' WHERE key='msj_search_schema'"
            )
        backup = search_settings.ensure_schema()
        self.assertTrue(Path(backup).is_file())
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                photos,
                [
                    tuple(r)
                    for r in conn.execute("SELECT * FROM search_reference_photos")
                ],
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM search_match_log").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertIsNone(search_settings.ensure_schema())

    def test_saved_good_text_paths_and_same_category_noise(self):
        rows = {r["id"]: r for r in self.manifest["rows"]}
        for identity, title in [
            (162, "Bench fleece with neck snood and thumb holes size uk 8 xs"),
            (162, "Bench coat"),
            (142, "Save the Queen top"),
            (142, "Save the Queen off shoulder graphic top"),
            (168, "Custo Barcelona graphic 3/4 sleeves top"),
            (177, "Diesel gold crest graphic long sleeve"),
            (195, "Hollister fitted faux fur coat"),
            (195, "Hollister fur hood parka fleece lined"),
            (200, "Hollister hoodless padded gilet"),
            (260, "Superdry short fur hood bomber with ribbed hem"),
            (229, "Monsoon strapless silk maxi dress"),
        ]:
            r = rows[identity]
            self.assertIsNone(
                search_settings.excluded_by(title, r["exclusions"]), (identity, title)
            )
            self.assertEqual(
                keyword_evidence(
                    {"shared_alert_version": 1, "shared_keywords": r["keywords"]}, title
                )["state"],
                "matched",
                (identity, title),
            )
        for identity, title in [
            (195, "Hollister fur teddy fleece jacket"),
            (195, "Hollister faux shearling aviator"),
            (195, "Hollister plain fleece jacket"),
            (180, "Dorothy Perkins fur trim swing coat"),
            (260, "Superdry Everest fur hood coat"),
            (257, "Stradivarius denim button up trucker"),
            (177, "Diesel T-Angie graphic top"),
        ]:
            self.assertIsNotNone(
                search_settings.excluded_by(title, rows[identity]["exclusions"]),
                (identity, title),
            )

    def test_protection_boundary_uses_supplied_value_and_current_cap(self):
        item = SimpleNamespace(
            price="7.00",
            currency="GBP",
            raw_data={"total_item_price": {"amount": "8.05", "currency_code": "GBP"}},
        )
        for maximum, expected in [(1024, False), (1025, True)]:
            result = vinted_budget.estimate(
                item, {"shared_alert_version": 1, "vinted_max_total": maximum}
            )
            self.assertEqual(result["total"], 1025)
            self.assertEqual(result["within_budget"], expected)
            self.assertFalse(result["buyer_protection_estimated"])

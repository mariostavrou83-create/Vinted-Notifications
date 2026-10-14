"""Eight ordered originals, immutable Telegram pages, and safe schema upgrades."""

import hashlib
import io
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_dashboard as dashboard_tests
import test_shared_alert_store as shared_store_tests
from PIL import Image
from test_search_controls import DatabaseFixture

import alert_images
import dashboard_store as store
import db
import search_settings as settings


def coloured_photo(colour):
    stream = io.BytesIO()
    Image.new("RGB", (160, 240), colour).save(stream, "PNG")
    return store.normalize_photo(io.BytesIO(stream.getvalue()))


class ReferencePagesTests(DatabaseFixture, unittest.TestCase):
    shared_form = shared_store_tests.SharedAlertStore.form
    database_state = dashboard_tests.ReferencePhotoTransactionTests.database_state
    colours = (
        (220, 20, 20),
        (20, 220, 20),
        (20, 20, 220),
        (220, 220, 20),
        (220, 20, 220),
        (20, 220, 220),
        (80, 80, 80),
        (160, 80, 20),
    )

    def setUp(self):
        super().setUp()
        self.photos = [coloured_photo(colour) for colour in self.colours]

    def legacy_form(self, **changes):
        row = settings.get_search(1)
        return dict(
            query_name=row["query_name"],
            query=row["query"],
            revision=str(row["revision"]),
            **changes,
        )

    def test_shared_search_retains_all_eight_and_two_uncropped_ordered_pages(self):
        original_queries = db.get_queries()
        identity = store.save_search(None, self.shared_form(), photos=self.photos)
        row = settings.get_search(identity)
        self.assertEqual(row["platform_mode"], "both")
        self.assertEqual(db.get_queries()[:44], original_queries)
        originals = store.reference_photos(identity)
        self.assertEqual([p["position"] for p in originals], list(range(8)))
        self.assertEqual(
            [p["media_id"] for p in originals],
            [hashlib.sha256(photo).hexdigest() for photo in self.photos],
        )
        pages = store.get_reference_pages(row["reference_id"])
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["id"], row["reference_id"])
        for page, colours in zip(pages, (self.colours[:4], self.colours[4:])):
            with Image.open(io.BytesIO(page["image"])) as image:
                self.assertEqual(image.size, (1280, 1280))
                for colour, (x, y, right, bottom) in zip(
                    colours, alert_images.cells(4)
                ):
                    actual = image.getpixel(((x + right) // 2, (y + bottom) // 2))
                    self.assertTrue(all(abs(a - b) < 6 for a, b in zip(actual, colour)))

    def test_second_page_changes_do_not_mutate_existing_alerts(self):
        store.save_search(1, self.legacy_form(), photos=self.photos)
        old_id = settings.get_search(1)["reference_id"]
        old_pages = store.get_reference_pages(old_id)
        replacement = coloured_photo((120, 40, 180))
        store.save_search(
            1, self.legacy_form(remove_reference=["7"]), photos=[replacement]
        )
        new_id = settings.get_search(1)["reference_id"]
        new_pages = store.get_reference_pages(new_id)
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(old_pages[0]["image"], new_pages[0]["image"])
        self.assertNotEqual(old_pages[1]["image"], new_pages[1]["image"])
        self.assertEqual(store.get_reference_pages(old_id), old_pages)

    def test_five_photos_make_a_single_image_second_page_and_four_keep_legacy_id(self):
        store.save_search(1, self.legacy_form(), photos=self.photos[:4])
        reference_id = settings.get_search(1)["reference_id"]
        self.assertEqual(
            reference_id,
            hashlib.sha256(alert_images.collage(self.photos[:4])).hexdigest(),
        )
        self.assertEqual(len(store.get_reference_pages(reference_id)), 1)
        store.save_search(1, self.legacy_form(), photos=self.photos[4:5])
        pages = store.get_reference_pages(settings.get_search(1)["reference_id"])
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["image"], alert_images.collage(self.photos[4:5]))
        self.assertEqual(store.get_reference_pages("missing"), [])

    def test_failed_second_collage_and_ninth_photo_cannot_change_saved_search(self):
        store.save_search(1, self.legacy_form(), photos=self.photos[:4])
        before = self.database_state()
        with patch.object(
            alert_images,
            "collage",
            side_effect=[
                alert_images.collage(self.photos[:4]),
                OSError("second page failed"),
            ],
        ), self.assertRaises(OSError):
            store.save_search(
                1, self.legacy_form(reminder="must not save"), photos=self.photos[4:]
            )
        self.assertEqual(self.database_state(), before)
        with self.assertRaisesRegex(ValueError, "eight"):
            store.save_search(1, self.legacy_form(), photos=self.photos[3:])
        self.assertEqual(self.database_state(), before)

    def test_old_pages_survive_search_edits_and_cleanup_while_alert_retains_them(self):
        store.save_search(1, self.legacy_form(), photos=self.photos)
        reference_id = settings.get_search(1)["reference_id"]
        pages = store.get_reference_pages(reference_id)
        self.batch(1, [110])
        with closing(settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE alert_outbox SET reference_id=?,photo_status='pending' WHERE item_id='110'",
                (reference_id,),
            )
            conn.execute(
                "INSERT INTO telegram_photo_cards(platform,message_id,item_id,reference_id,details) VALUES ('vinted',42,'110',?,'{}')",
                (reference_id,),
            )
        store.save_search(1, self.legacy_form(remove_photo="yes"), photos=[])
        with closing(settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_media SET created=0")
            store._store_media(conn, coloured_photo((15, 30, 45)))
        self.assertEqual(
            store.get_reference_pages(reference_id),
            [dict(page, created=0) for page in pages],
        )
        with closing(settings.connection()) as conn, conn:
            conn.execute("DELETE FROM telegram_photo_cards")
            conn.execute(
                "UPDATE alert_outbox SET photo_status='none' WHERE item_id='110'"
            )
            store._store_media(conn, coloured_photo((30, 45, 60)))
            store._store_media(conn, coloured_photo((45, 60, 75)))
        self.assertEqual(store.get_reference_pages(reference_id), [])
        self.assertIsNone(store.get_media(pages[1]["id"]))


class EightPhotoMigrationTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        photo = coloured_photo((20, 200, 70))
        store.save_search(
            1,
            {
                "query_name": "Saved originals",
                "query": settings.get_search(1)["query"],
                "revision": "0",
            },
            photos=[photo] * 4,
        )
        # Recreate exactly the previous constraint, then upgrade as production
        # does with the previous schema marker and existing original photos.
        with closing(settings.connection()) as conn, conn:
            conn.execute("ALTER TABLE search_reference_photos RENAME TO wider_photos")
            conn.execute("""CREATE TABLE search_reference_photos (
                query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 3),
                media_id TEXT NOT NULL REFERENCES dashboard_media(id),
                PRIMARY KEY(query_id,position))""")
            conn.execute(
                "INSERT INTO search_reference_photos SELECT * FROM wider_photos"
            )
            conn.execute("DROP TABLE wider_photos")
            conn.execute("DROP TABLE dashboard_reference_pages")
            conn.execute(
                "UPDATE parameters SET value='23' WHERE key='msj_search_schema'"
            )

    def test_upgrade_preserves_originals_buyer_and_queries_and_backups_before_migration(
        self,
    ):
        originals = store.reference_photos(1)
        queries = db.get_queries()
        with closing(settings.connection()) as conn:
            buyer = [tuple(r) for r in conn.execute("SELECT * FROM vinted_buyer")]
        backup = settings.ensure_schema()
        self.assertTrue(Path(backup).is_file())
        self.assertEqual(store.reference_photos(1), originals)
        self.assertEqual(db.get_queries(), queries)
        with closing(settings.connection()) as conn:
            self.assertEqual(
                [tuple(r) for r in conn.execute("SELECT * FROM vinted_buyer")], buyer
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM parameters WHERE key='msj_search_schema'"
                ).fetchone()[0],
                "23",
            )
        row = settings.get_search(1)
        store.save_search(
            1,
            {
                "query_name": row["query_name"],
                "query": row["query"],
                "revision": str(row["revision"]),
            },
            photos=[coloured_photo((70, 20, 200))] * 4,
        )
        self.assertEqual(len(store.reference_photos(1)), 8)
        self.assertIsNone(settings.ensure_schema())

    def test_failed_migration_rolls_back_shadow_table_and_can_be_retried(self):
        originals = store.reference_photos(1)
        with patch(
            "ebay_schema.migrate", side_effect=RuntimeError("migration interrupted")
        ), self.assertRaisesRegex(RuntimeError, "interrupted"):
            settings.ensure_schema()
        self.assertEqual(store.reference_photos(1), originals)
        with closing(settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM parameters WHERE key='msj_search_schema'"
                ).fetchone()[0],
                "23",
            )
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name IN ('search_reference_photos_v24','dashboard_reference_pages')"
                ).fetchone()
            )
        self.assertTrue(settings.ensure_schema())
        self.assertEqual(store.reference_photos(1), originals)


if __name__ == "__main__":
    unittest.main()

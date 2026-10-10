"""Dashboard integration tests: real SQLite, Flask requests and mocked Telegram transport."""

import io
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import dashboard_store as store
import db
import search_settings
from web_ui_plugin.web_ui import create_app


def photo_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (400, 600), "green").save(stream, "PNG")
    return stream.getvalue()


class DashboardTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def owner(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline test password"),),
            )
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-csrf"

    def form(self, **kwargs):
        return dict(
            query_name="Hollister fur",
            query=db.get_queries()[0][1],
            reminder="Check the cuffs",
            exclusions="teddy coat",
            revision="0",
            csrf="offline-csrf",
            **kwargs,
        )

    def test_every_private_route_and_legacy_config_is_protected(self):
        for path in (
            "/",
            "/search/1",
            "/reference/abc",
            "/config",
            "/queries",
            "/logs",
            "/items",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertEqual(response.location, "/login")
        self.owner()
        for path in ("/config", "/logs", "/queries"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/healthz").json, {"status": "ok"})

    def test_setup_login_csrf_logout_persistence_and_one_time_code(self):
        code = (
            (Path(db.DB_PATH).parent / "dashboard-setup-code.txt").read_text().strip()
        )
        self.assertEqual(self.client.get("/setup").status_code, 200)
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post(
            "/setup",
            data={
                "csrf": csrf,
                "code": code,
                "password": "twelve plus characters",
                "confirm": "twelve plus characters",
            },
        )
        self.assertEqual(response.location, "/")
        self.assertFalse(
            (Path(db.DB_PATH).parent / "dashboard-setup-code.txt").exists()
        )
        self.assertEqual(self.client.get("/setup").location, "/login")
        self.assertEqual(self.client.get("/").status_code, 200)
        key = self.app.secret_key
        self.assertEqual(create_app({"TESTING": True}).secret_key, key)
        self.assertEqual(
            self.client.post("/search/1/pause", data={"revision": "0"}).status_code, 400
        )
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        self.assertEqual(
            self.client.post("/logout", data={"csrf": csrf}).status_code, 302
        )
        self.assertEqual(self.client.get("/").location, "/login")
        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        self.assertEqual(
            self.client.post(
                "/login", data={"csrf": csrf, "password": "twelve plus characters"}
            ).location,
            "/",
        )

    def test_rate_limit_is_persistent(self):
        self.client.get("/setup")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        for _ in range(15):
            self.assertEqual(
                self.client.post(
                    "/setup", data={"csrf": csrf, "code": "wrong"}
                ).status_code,
                200,
            )
        self.assertEqual(
            self.client.post(
                "/setup", data={"csrf": csrf, "code": "wrong"}
            ).status_code,
            429,
        )
        self.assertEqual(self.client.get("/").location, "/login")

    def test_edit_photo_round_trip_retains_all_44_and_watermarks(self):
        self.owner()
        original = db.get_queries()
        form = self.form(photo=(io.BytesIO(photo_bytes()), "example.png"))
        response = self.client.post(
            "/search/1", data=form, content_type="multipart/form-data"
        )
        self.assertEqual(response.location, "/")
        self.assertEqual([r[:3] for r in original], [r[:3] for r in db.get_queries()])
        self.assertTrue(db.is_item_in_db_by_id(99))
        row = search_settings.get_search(1)
        self.assertEqual(row["exclusions"], ["teddy coat"])
        self.assertEqual(row["reminder"], "Check the cuffs")
        self.assertEqual(
            self.client.get("/reference/" + row["reference_id"]).mimetype, "image/jpeg"
        )
        self.assertEqual(len(self.batch(1, [111])), 1)
        self.assertEqual(self.batch(1, [112])[0][5]["id"], row["reference_id"])
        self.assertEqual(self.client.get("/search/1").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/?view=archive").status_code, 200)
        self.assertEqual(self.client.get("/search/new").status_code, 200)
        with self.assertRaises(ValueError):
            store.save_search(1, self.form())

    def test_url_edit_stale_response_and_silent_baseline(self):
        form = self.form()
        form["query"] = "https://www.vinted.co.uk/catalog?search_text=changed"
        store.save_search(1, form)
        self.assertEqual(db.get_queries()[0][2], 100)
        from queue import Queue

        source, output = Queue(), Queue()
        source.put(([], 1, "https://www.vinted.co.uk/catalog?search_text=test1"))
        self.core.clear_item_queue(source, output)
        self.assertEqual(search_settings.get_search(1)["rebaseline"], 1)
        self.assertEqual(self.batch(1, [110]), [])
        self.assertEqual(search_settings.get_search(1)["rebaseline"], 0)
        self.assertEqual(len(self.batch(1, [111])), 1)

    def test_pause_archive_restore_preserve_all_history(self):
        store.change_state(1, "pause", 0)
        self.assertEqual(len(search_settings.active_queries()), 43)
        self.assertEqual(self.batch(1, [110]), [])
        store.change_state(1, "archive", 1)
        self.assertEqual(len(store.list_searches(True)), 1)
        store.change_state(1, "restore", 2)
        store.change_state(1, "resume", 3)
        self.assertEqual(len(search_settings.active_queries()), 44)
        self.assertTrue(db.is_item_in_db_by_id(99))
        self.assertEqual(self.batch(1, [110]), [])
        self.assertEqual(len(self.batch(1, [111])), 1)

    def test_add_search_and_reject_bad_uploads_and_urls_without_changes(self):
        self.owner()
        form = self.form()
        for url in (
            "http://127.0.0.1/catalog",
            "https://www.vinted.co.uk.evil.com/catalog",
            "https://www.vinted.co.uk@evil.com/catalog",
            "https://www.vinted.co.uk/items/123",
        ):
            with self.assertRaises(ValueError):
                store.normalize_url(url)
        with self.assertRaises(ValueError):
            store.normalize_photo(io.BytesIO(b'<svg onload="evil"/>'))
        with self.assertRaises(ValueError):
            store.normalize_photo(io.BytesIO(b"x" * (8 * 1024 * 1024 + 1)))
        form["query"] = "https://www.vinted.co.uk/catalog?search_text=fresh"
        response = self.client.post("/search/new", data=form)
        self.assertEqual(response.location, "/")
        self.assertEqual(len(db.get_queries()), 45)
        self.assertEqual(self.batch(45, [150]), [])
        self.assertEqual(len(self.batch(45, [151])), 1)
        self.assertIn(
            "frame-ancestors 'none'",
            self.client.get("/").headers["Content-Security-Policy"],
        )


class PhotoDeliveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_retry_photo_does_not_resend_listing_and_caches_file_id(self):
        from telegram.error import NetworkError

        from telegram_bot_plugin.telegram_bot import LeRobot

        store.save_search(
            1,
            {
                "query_name": "Hollister",
                "query": db.get_queries()[0][1],
                "revision": "0",
            },
            store.normalize_photo(io.BytesIO(photo_bytes())),
        )
        media_id = search_settings.get_search(1)["reference_id"]

        class Bot:
            send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
            send_photo = AsyncMock(
                side_effect=[
                    NetworkError("offline"),
                    SimpleNamespace(photo=[SimpleNamespace(file_id="cached-photo")]),
                ]
            )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        robot = object.__new__(LeRobot)
        robot.bot = Bot()
        with patch(
            "telegram_bot_plugin.telegram_bot.asyncio.sleep", new_callable=AsyncMock
        ):
            await robot.send_new_post(
                "Listing",
                "https://www.vinted.co.uk/items/123",
                "Open",
                reference={"id": media_id, "name": "Hollister", "query_id": 1},
            )
        self.assertEqual(robot.bot.send_message.await_count, 1)
        self.assertEqual(robot.bot.send_photo.await_count, 2)
        args = robot.bot.send_photo.await_args.kwargs
        self.assertTrue(args["disable_notification"])
        self.assertEqual(args["reply_parameters"].message_id, 42)
        self.assertEqual(store.get_media(media_id)["telegram_file_id"], "cached-photo")
        robot.bot.send_photo.side_effect = None
        robot.bot.send_photo.return_value = SimpleNamespace(
            photo=[SimpleNamespace(file_id="cached-photo")]
        )
        with patch(
            "telegram_bot_plugin.telegram_bot.asyncio.sleep", new_callable=AsyncMock
        ):
            await robot.send_reference(
                {"id": media_id, "name": "Hollister", "query_id": 1}, 43
            )
        self.assertEqual(
            robot.bot.send_photo.await_args.kwargs["photo"], "cached-photo"
        )


class ReferencePhotoTransactionTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.photo = store.normalize_photo(io.BytesIO(photo_bytes()))

    def form(self):
        saved = search_settings.get_search(1)
        return {
            "query_name": "Updated search",
            "query": saved["query"],
            "revision": str(saved["revision"]),
            "reminder": "Updated reminder",
        }

    def database_state(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            return {
                table: conn.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            }

    def test_independent_writer_commits_while_collage_is_paused(self):
        import alert_images

        entered, release = threading.Event(), threading.Event()
        original = alert_images.collage

        def paused(images):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Offline collage test was not released")
            return original(images)

        form = self.form()
        with (
            patch.object(alert_images, "collage", side_effect=paused),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(store.save_search, 1, form, photos=[self.photo])
            try:
                self.assertTrue(entered.wait(5))
                with closing(sqlite3.connect(db.DB_PATH, timeout=0.1)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE queries SET query_name='Other writer' WHERE id=2"
                    )
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5), 1)
        self.assertEqual(search_settings.get_search(2)["query_name"], "Other writer")
        self.assertEqual(len(store.reference_photos(1)), 1)

    def test_photo_membership_reference_and_search_revision_races_are_rejected(self):
        import alert_images

        store.save_search(1, self.form(), photos=[self.photo])
        original = alert_images.collage
        mutations = (
            "UPDATE search_reference_photos SET position=3 WHERE query_id=1",
            (
                "UPDATE dashboard_media SET image=X'72616365' WHERE id IN "
                "(SELECT media_id FROM search_reference_photos WHERE query_id=1)"
            ),
            "UPDATE search_dashboard SET reference_id=NULL WHERE query_id=1",
            "UPDATE search_dashboard SET revision=revision+1 WHERE query_id=1",
            "DELETE FROM queries WHERE id=1",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                before = self.database_state()
                form = self.form()
                raced = []

                def concurrent_edit(images, mutation=mutation, raced=raced):
                    with closing(search_settings.connection()) as conn, conn:
                        if mutation == "DELETE FROM queries WHERE id=1":
                            conn.execute("DELETE FROM items WHERE query_id=1")
                        conn.execute(mutation)
                    raced.append(self.database_state())
                    return original(images)

                # Replace the original example, so even a concurrent corrupt
                # example cannot be decoded before the race is revalidated.
                form["remove_photo"] = "yes"
                with (
                    patch.object(alert_images, "collage", side_effect=concurrent_edit),
                    self.assertRaises(ValueError),
                ):
                    store.save_search(1, form, photos=[self.photo])
                self.assertEqual(self.database_state(), raced[0])
                self.assertNotEqual(raced[0], before)
                # Restore only the local fixture for the next independent case.
                with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    for table, rows in before.items():
                        conn.execute(f'DELETE FROM "{table}"')
                        if rows:
                            placeholders = ",".join("?" for _ in rows[0])
                            conn.executemany(
                                f'INSERT INTO "{table}" VALUES ({placeholders})', rows
                            )

    def test_failed_invalid_or_over_capacity_photos_leave_every_saved_value_intact(
        self,
    ):
        import alert_images

        store.save_search(1, self.form(), photos=[self.photo])
        before = self.database_state()
        form = self.form()
        form.update(query_name="Must not persist", reminder="Must not persist")
        with (
            patch.object(
                alert_images, "collage", side_effect=OSError("offline failure")
            ),
            self.assertRaises(OSError),
        ):
            store.save_search(1, form, photos=[self.photo])
        self.assertEqual(self.database_state(), before)
        with self.assertRaises((OSError, ValueError)):
            store.save_search(1, form, photos=[b"invalid image"])
        self.assertEqual(self.database_state(), before)
        with self.assertRaises(ValueError):
            store.save_search(1, form, photos=[self.photo] * 4)
        self.assertEqual(self.database_state(), before)
        invalid = dict(form, remove_reference=["3"])
        with self.assertRaises(ValueError):
            store.save_search(1, invalid, photos=[])
        self.assertEqual(self.database_state(), before)

        # Capacity must be checked against current storage inside the atomic
        # write, and a late capacity failure must roll back the whole save.
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO dashboard_media(id,image,created) VALUES ('full',zeroblob(?),?)",
                (50 * 1024 * 1024, 10**12),
            )
        full = self.database_state()
        with self.assertRaisesRegex(ValueError, "storage is full"):
            store.save_search(1, form, photos=[self.photo])
        self.assertEqual(self.database_state(), full)

    def test_noop_photo_save_skips_collage_and_preserves_photo_bytes_and_order(self):
        import alert_images

        store.save_search(1, self.form(), photos=[self.photo] * 4)
        references = store.reference_photos(1)
        media = self.database_state()["dashboard_media"]
        with patch.object(alert_images, "collage") as collage:
            store.save_search(1, self.form(), photos=[])
        collage.assert_not_called()
        self.assertEqual(store.reference_photos(1), references)
        self.assertEqual(self.database_state()["dashboard_media"], media)


class ScaledDashboardReadTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT INTO queries VALUES (?,?,100,?)",
                [
                    (
                        i,
                        f"https://www.vinted.co.uk/catalog?search_text=test{i}",
                        f"Search {i}",
                    )
                    for i in range(45, 251)
                ],
            )
        for query_id, mode, keywords in ((1, "both", "fur,sherpa"), (2, "ebay", "")):
            saved = search_settings.get_search(query_id)
            store.save_search(
                query_id,
                {
                    "query_name": f"Mixed search {query_id}",
                    "query": saved["query"],
                    "revision": str(saved["revision"]),
                    "platform_mode": mode,
                    "ebay_keywords": "bench jacket",
                    "vinted_keywords": keywords,
                },
            )

    def test_250_search_values_match_unbatched_reads_with_one_closed_connection(self):
        expected = store._list_searches()
        self.assertEqual(len(expected), 250)
        opened = []
        connect = sqlite3.connect

        def counted_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with patch.object(db.sqlite3, "connect", side_effect=counted_connect):
            actual = store.list_searches()
        self.assertEqual(actual, expected)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        mixed = {row["id"]: row for row in actual}
        self.assertEqual(mixed[1]["platform_mode"], "both")
        self.assertEqual(mixed[1]["vinted_keywords"], ["fur", "sherpa"])
        self.assertEqual(mixed[2]["platform_mode"], "ebay")
        self.assertEqual(mixed[250]["platform_mode"], "vinted")

        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE queries SET query_name='Changed now' WHERE id=1")
            conn.execute("INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (2)")
            conn.execute("UPDATE search_dashboard SET archived=1 WHERE query_id=2")
        fresh = store.list_searches()
        self.assertEqual(len(fresh), 249)
        self.assertEqual(
            next(row for row in fresh if row["id"] == 1)["query_name"], "Changed now"
        )
        self.assertEqual([row["id"] for row in store.list_searches(archived=True)], [2])

    def test_failed_read_rolls_back_unfinished_work_and_next_read_remains_fresh(self):
        expected = store.list_searches()

        def interrupted(_query_id):
            conn = db.get_db_connection()
            conn.execute(
                "INSERT INTO parameters VALUES ('unfinished_dashboard_test','discard')"
            )
            raise sqlite3.OperationalError("interrupted read")

        with (
            patch("ebay_store.platform_details", side_effect=interrupted),
            self.assertRaises(sqlite3.OperationalError),
        ):
            store.list_searches()
        self.assertIsNone(db.get_parameter("unfinished_dashboard_test"))
        self.assertEqual(store.list_searches(), expected)


if __name__ == "__main__":
    unittest.main()

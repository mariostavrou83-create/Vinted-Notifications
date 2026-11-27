import io
import json
import zipfile

from pilot import home_companion, home_routes
from pilot.tests.test_home_watch import HomeWatchTest


class CompanionTests(HomeWatchTest):
    def owner_post(self, path, uid=1, **data):
        with self.client.session_transaction() as session:
            session.update(user_id=uid, csrf_token="csrf")
        return self.client.post(
            "/api/home-routes/" + path, json=data, headers={"X-CSRF-Token": "csrf"}
        )

    def pair(self, code, account="702"):
        return self.client.post(
            "/api/home/companion/pair", json={"code": code, "account_id": account}
        )

    def create(self):
        created = self.owner_post("sources", label="Spare profile").json
        paired = self.pair(created["code"])
        self.assertEqual(paired.status_code, 200)
        return created["id"], paired.json["token"]

    def submit(self, token, rows, account="702"):
        return self.client.post(
            "/api/home/companion/events",
            json={"items": rows, "account_id": account},
            headers={"Authorization": "Bearer " + token},
        )

    def card(self, identity="99", **changes):
        return dict(
            {
                "id": identity,
                "url": "https://www.vinted.co.uk/items/" + identity,
                "title": "Linen jacket",
                "price_gbp": 10.50,
                "listed_at": self.now - 4,
            },
            **changes,
        )

    def test_own_extra_and_consenting_recipient_use_their_linked_chats_and_filters(
        self,
    ):
        sid, token = self.create()
        self.conn.execute("DELETE FROM native_devices WHERE user_id=1")
        invite = self.owner_post("invite", 2).json["code"]
        self.assertEqual(
            self.owner_post("accept", 1, code=invite, source_id=sid).status_code, 200
        )
        self.conn.execute(
            "UPDATE settings SET data=? WHERE user_id=2",
            (
                json.dumps(
                    dict(
                        categories=["Women"], sizes=["S"], quiet_start="", quiet_end=""
                    )
                ),
            ),
        )
        self.assertEqual(
            self.submit(
                token, [self.card(), self.card("100", size="S", category="Women")]
            ).json["accepted"],
            2,
        )
        rows = list(
            self.conn.execute(
                "SELECT user_id,listing_id FROM pilot_alerts ORDER BY user_id,listing_id"
            )
        )
        self.assertEqual(list(map(tuple, rows)), [(1, "100"), (1, "99"), (2, "100")])
        self.assertTrue(home_routes.routed_active(self.conn, 1, "99"))
        self.assertTrue(home_routes.routed_active(self.conn, 2, "100"))
        snapshot = self.client.get("/api/home-routes").json
        self.assertEqual(len(snapshot["extras"]), 1)
        self.assertTrue(snapshot["extras"][0]["paired"])
        self.assertEqual(len(snapshot["incoming"]), 0)
        self.assertEqual(len(snapshot["outgoing"]), 1)
        self.assertEqual(snapshot["outgoing"][0]["home"], "Spare profile")
        self.assertNotIn(token, json.dumps(snapshot))
        self.assertEqual(self.submit(token, []).status_code, 429)
        self.conn.execute("UPDATE pilot_chats SET chat_id=301 WHERE user_id=1")
        self.assertFalse(home_routes.routed_active(self.conn, 1, "99"))
        self.assertTrue(home_routes.routed_active(self.conn, 2, "100"))
        self.assertEqual(self.owner_post(f"sources/{sid}/remove", 2).status_code, 404)
        self.assertEqual(self.owner_post(f"sources/{sid}/remove", 1).status_code, 200)
        self.assertFalse(home_routes.routed_active(self.conn, 2, "100"))
        self.assertEqual(self.submit(token, []).status_code, 403)

    def test_pair_code_replay_and_reconnect_require_exact_profile_and_rotate_only_on_success(
        self,
    ):
        original = self.owner_post("sources", label="Other").json
        sid, code = original["id"], original["code"]
        old = self.pair(code).json["token"]
        self.assertEqual(self.pair(code).status_code, 403)
        self.assertEqual(
            self.submit(old, [self.card()], account="703").status_code, 403
        )
        self.assertEqual(
            self.owner_post(f"sources/{sid}/reconnect", 2).status_code, 404
        )
        reconnect = self.owner_post(f"sources/{sid}/reconnect").json["code"]
        self.assertEqual(self.pair(reconnect, "703").status_code, 403)
        self.assertEqual(self.submit(old, [self.card()]).json["accepted"], 1)
        rotated = self.pair(reconnect).json["token"]
        self.assertNotEqual(old, rotated)
        self.assertEqual(self.submit(old, []).status_code, 403)
        self.assertEqual(self.pair(reconnect).status_code, 403)
        self.assertTrue(home_routes.routed_active(self.conn, 1, "99"))
        self.conn.execute("UPDATE extra_home_sources SET last_seen=NULL")
        self.assertEqual(self.submit(rotated, [self.card("200")]).json["accepted"], 1)
        self.assertEqual(
            self.client.post(
                "/api/native/home-watch",
                json={"username": "buyer1", "enabled": True},
                headers={"Authorization": "Bearer " + rotated},
            ).status_code,
            401,
        )
        self.conn.execute("UPDATE extra_home_sources SET pair_until=0")
        pending = self.owner_post("sources", label="Unpaired").json
        self.conn.execute(
            "UPDATE extra_home_sources SET pair_until=0 WHERE id=?", (pending["id"],)
        )
        self.assertEqual(self.pair(pending["code"]).status_code, 403)

    def test_never_attach_new_route_to_a_previous_revoked_delivery(self):
        first, t1 = self.create()
        self.assertEqual(self.submit(t1, [self.card()]).json["accepted"], 1)
        self.assertEqual(self.owner_post(f"sources/{first}/remove").status_code, 200)
        second, t2 = self.create()
        self.assertNotEqual(first, second)
        self.assertEqual(self.submit(t2, [self.card()]).json["accepted"], 0)
        self.assertFalse(home_routes.routed_active(self.conn, 1, "99"))

    def test_strict_listing_limits_and_protected_zip(self):
        _, token = self.create()
        self.assertEqual(self.submit(token, [{}] * 51).status_code, 400)
        for changes in [
            {"id": "0"},
            {"price_gbp": True},
            {"price_gbp": 0.001},
            {"url": "https://vinted.co.uk.evil/items/99"},
            {"url": "https://www.vinted.co.uk/items/11"},
            {"listed_at": self.now - 180},
            {"listed_at": self.now + 5},
            {"cookie": "should never be accepted"},
            {"title": "bad\ntext"},
        ]:
            with self.subTest(changes=changes):
                self.assertIsNone(
                    home_companion.public_item(self.card(**changes), self.now)
                )
        self.assertEqual(
            self.submit(token, [self.card("3"), self.card("3")]).json["accepted"], 1
        )
        self.assertEqual(
            self.client.post(
                "/api/home-routes/sources", json={"label": "No CSRF"}
            ).status_code,
            403,
        )
        archive = self.client.get("/extension/home-companion.zip")
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.data)) as z:
            manifest = json.loads(z.read("manifest.json"))
            self.assertNotIn("cookies", manifest["permissions"])
            self.assertIn("home.js", z.namelist())
            self.assertEqual(
                json.loads(z.read("dashboard.json"))["origin"], "http://localhost"
            )
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/api/home-routes").status_code, 401)
        self.assertNotEqual(
            self.client.get("/extension/home-companion.zip").status_code, 200
        )

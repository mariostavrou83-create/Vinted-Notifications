import io
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from pilot.app import create_app
from pilot.db import get_db


class DashboardAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "pilot.sqlite")
        self.app = create_app({"TESTING": True, "DATABASE": path, "SESSION_COOKIE_SECURE": False})
        self.login_password = "private-test-login-123"

    def token(self, client):
        with client.session_transaction() as sess:
            return sess["csrf_token"]

    def invite(self):
        result = self.app.test_cli_runner().invoke(args=["invite"])
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output.strip()

    def signup(self, name, code=None):
        client = self.app.test_client()
        self.assertEqual(client.get("/signup").status_code, 200)
        resp = client.post("/signup", data={
            "csrf_token": self.token(client), "invite": code or self.invite(),
            "username": name, "password": self.login_password,
        })
        return client, resp

    def fields(self):
        return {"url": "https://www.vinted.co.uk/catalog?catalog[]=3", "name": "Leather",
                "include_keywords": ["leather"], "exclude_keywords": ["faux"], "budget": "25.75"}

    def test_invites_are_single_use_and_private_searches_start_empty(self):
        invitation = self.invite()
        owner, resp = self.signup("owner", invitation)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(owner.get("/api/state").json["searches"], [])
        self.assertEqual(self.signup("another", invitation)[1].status_code, 400)
        self.assertEqual(self.app.test_client().get("/api/state").status_code, 401)
        self.assertEqual(owner.post("/api/settings", json={"categories": ["Women"]}).status_code, 403)
        duplicate_invite = self.invite()
        self.assertEqual(self.signup("OWNER", duplicate_invite)[1].status_code, 409)
        self.assertEqual(self.signup("different", duplicate_invite)[1].status_code, 302)

    def test_update_upload_scope_and_home_preferences(self):
        owner, _ = self.signup("owner")
        other, _ = self.signup("other")
        hdrs = {"X-CSRF-Token": self.token(owner)}
        other_hdrs = {"X-CSRF-Token": self.token(other)}
        image = io.BytesIO()
        Image.new("RGB", (12, 12), "white").save(image, format="PNG")
        image.seek(0)
        data = self.fields()
        data.update(include_keywords="leather\nzip", exclude_keywords="faux",
                    photos=(image, "jacket.png"))
        response = owner.post("/api/searches", data=data, headers=hdrs)
        self.assertEqual(response.status_code, 200)
        url = f"/api/searches/{response.json['id']}"
        row = owner.get("/api/state").json["searches"][0]
        self.assertEqual((row["budget_pence"], row["include_keywords"]), (2575, ["leather", "zip"]))
        image_url = row["photos"][0]["url"]
        self.assertEqual(owner.get(image_url).mimetype, "image/jpeg")
        self.assertEqual(other.get(image_url).status_code, 404)
        self.assertEqual(other.get("/api/state").json["searches"], [])
        self.assertEqual(other.post(url, json={"name": "x"}, headers=other_hdrs).status_code, 404)
        self.assertEqual(other.delete(url, headers=other_hdrs).status_code, 404)
        resp = owner.post(url, json={"remove_photo_ids": [row["photos"][0]["id"]]}, headers=hdrs)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(owner.get(image_url).status_code, 404)
        self.assertEqual(owner.get("/api/state").json["searches"][0]["budget"], "25.75")
        resp = owner.post("/api/settings", json={"categories": ["Men"],
                                                 "delivery_mode": "home", "max_total": "50"}, headers=hdrs)
        self.assertEqual(resp.json["settings"]["max_total"], "50.00")
        self.assertEqual(other.get("/api/state").json["settings"]["home_address_ref"], "")
        self.assertEqual(owner.post(url, json={"url": "https://vinted.co.uk.evil.test/catalog"},
                                    headers=hdrs).status_code, 400)

    def test_concurrent_last_slot_is_enforced_per_user(self):
        client, _ = self.signup("owner")
        data = client.get("/api/state").json
        uid = data["user"]["id"]
        with self.app.app_context():
            get_db().executemany("""INSERT INTO searches(user_id,name,url,include_keywords,exclude_keywords,enabled)
                                    VALUES(?,?,'https://www.vinted.co.uk/catalog','[]','[]',1)""",
                                 ((uid, str(i)) for i in range(99)))
        gate = threading.Barrier(2)

        def add(_):
            c = self.app.test_client()
            with c.session_transaction() as session:
                session.update(user_id=uid, csrf_token="test")
            gate.wait(timeout=5)
            resp = c.post("/api/searches", json=self.fields(), headers={"X-CSRF-Token": "test"})
            return resp.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(add, [0, 1])), [200, 409])
        self.assertEqual(len(client.get("/api/state").json["searches"]), 100)


if __name__ == "__main__":
    unittest.main()

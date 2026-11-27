import hashlib
import re
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from pilot.app import create_app
from pilot.db import get_db
from pilot import flow, telegram_poll


class HostedTeamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.owner_code = secrets.token_urlsafe(32)
        self.bot_token = "987654:" + secrets.token_urlsafe(24)
        self.origin = "https://finder.example"
        self.config = {
            "TESTING": True, "DATABASE": str(Path(self.directory.name) / "pilot.sqlite"),
            "FINDER_PILOT_OWNER_INVITE_HASH": hashlib.sha256(self.owner_code.encode()).hexdigest(),
            "FINDER_PILOT_CONFIG_KEY": Fernet.generate_key().decode(),
            "FINDER_PUBLIC_URL": self.origin
        }
        self.app = create_app(self.config)
        self.owner = self.app.test_client()

    def token(self, client):
        with client.session_transaction(base_url=self.origin) as sess:
            return sess["csrf_token"]

    def signup(self, client, code, name):
        client.get("/signup", base_url=self.origin)
        return client.post("/signup", base_url=self.origin, data={"username":name,"password":"long-test-password-2026",
                          "csrf_token":self.token(client), "invite":code})

    def invite(self):
        response = self.post(self.owner,"/team/invites",{"label":"Private tester"})
        self.assertEqual(response.status_code,200)
        return re.search(r"/signup#invite=([A-Za-z0-9_-]+)",response.get_data(as_text=True))[1]

    def post(self, client, path, data, **kwargs):
        return client.post(path,base_url=self.origin,data={"csrf_token":self.token(client),**data},**kwargs)

    def bot_form(self):
        return {"bot_token":self.bot_token, "bot_username":"@MyTestPilotBot","dedicated":"yes"}

    def test_first_tester_is_not_owner_and_invites_are_single_use_and_revocable(self):
        cli = self.app.test_cli_runner().invoke(args=["invite"])
        self.assertEqual(cli.exit_code,0)
        first = self.app.test_client()
        self.assertEqual(self.signup(first,cli.output.strip(),"first-tester").status_code,302)
        self.assertEqual(first.get("/team",base_url=self.origin).status_code,403)
        self.assertEqual(self.post(first,"/team/invites",{"label":"No"}).status_code,403)
        self.assertEqual(self.signup(self.owner,self.owner_code,"real-owner").status_code,302)
        self.assertEqual(self.owner.get("/team",base_url=self.origin).status_code,200)
        self.assertEqual(self.signup(self.app.test_client(),self.owner_code,"owner-replay").status_code,400)
        invite = self.invite()
        self.assertNotIn(invite, self.owner.get("/team",base_url=self.origin).get_data(as_text=True))
        self.assertEqual(self.signup(self.app.test_client(),invite,"second-tester").status_code,302)
        self.assertEqual(self.signup(self.app.test_client(),invite,"tester-replay").status_code,400)
        revoked = self.invite()
        self.assertEqual(self.post(self.owner,"/team/invites/2/revoke",{}).status_code,200)
        self.assertEqual(self.signup(self.app.test_client(),revoked,"revoked").status_code,400)
        self.assertEqual(self.owner.post("/team/invites",base_url=self.origin,data={"label":"No CSRF"}).status_code,403)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM team_owner WHERE user_id IS NOT NULL").fetchone()[0],1)
        rotated = "b" * 44
        replacement = create_app({**self.config, "FINDER_PILOT_OWNER_INVITE_HASH":hashlib.sha256(rotated.encode()).hexdigest()})
        self.assertEqual(self.signup(replacement.test_client(),rotated,"rival").status_code,400)

    def test_no_bootstrap_no_owner_and_duplicate_username_rolls_back_claim(self):
        cli = self.app.test_cli_runner().invoke(args=["invite"])
        self.assertEqual(self.signup(self.app.test_client(), cli.output.strip(),"already-taken").status_code,302)
        self.assertEqual(self.signup(self.owner,self.owner_code,"already-taken").status_code,409)
        self.assertEqual(self.signup(self.owner,self.owner_code,"new-name").status_code,302)
        self.assertEqual(self.owner.get("/team",base_url=self.origin).status_code,200)
        separate = create_app({"TESTING":True,"DATABASE":str(Path(self.directory.name)/"no-owner.sqlite")})
        cli = separate.test_cli_runner().invoke(args=["invite"])
        ordinary=separate.test_client()
        self.assertEqual(self.signup(ordinary,cli.output.strip(),"first").status_code,302)
        self.assertEqual(ordinary.get("/team",base_url=self.origin).status_code,403)

    def test_owner_bot_encrypted_dynamic_private_and_polls(self):
        self.signup(self.owner,self.owner_code,"owner")
        with self.app.app_context():
            self.assertEqual(flow.tg_config("TELEGRAM_BOT_TOKEN"),"")
        me = {"id":987654,"username":"MyTestPilotBot","is_bot":True}
        with patch("pilot.flow_worker.telegram", side_effect=[me,{"url":""}]) as verify:
            response=self.post(self.owner,"/team/telegram",self.bot_form())
            self.assertEqual([call.args[0] for call in verify.call_args_list],["getMe","getWebhookInfo"])
        self.assertEqual(response.status_code,303)
        self.assertNotIn(self.bot_token,response.get_data(as_text=True))
        self.assertNotIn(self.bot_token,self.owner.get("/team",base_url=self.origin).get_data(as_text=True))
        with self.app.app_context():
            db=get_db()
            encrypted=db.execute("SELECT ciphertext FROM team_bot").fetchone()[0]
            self.assertNotIn(self.bot_token.encode(),encrypted)
            self.assertEqual(flow.tg_config("TELEGRAM_BOT_TOKEN"),self.bot_token)
            self.assertEqual(flow.tg_config("TELEGRAM_MODE"),"polling")
            self.assertEqual(flow.tg_config("TELEGRAM_BOT_USERNAME"),me["username"])
            with patch("pilot.flow_worker.telegram",side_effect=[me,{"url":""},[]]) as worker:
                self.assertTrue(telegram_poll.receive_updates(db,time.time()))
                self.assertEqual([c.args[0] for c in worker.call_args_list],["getMe","getWebhookInfo","getUpdates"])
            self.assertIn("listening",telegram_poll.polling_status(db).lower())

    def test_initial_tester_never_becomes_owner_or_reopens_after_restart(self):
        code=secrets.token_urlsafe(32)
        digest=hashlib.sha256(code.encode()).hexdigest()
        expiry=int(time.time())+1800
        config={**self.config,"FINDER_PILOT_INITIAL_INVITE_HASH":digest,
                "FINDER_PILOT_INITIAL_INVITE_EXPIRES":str(expiry)}
        started=create_app(config)
        restarted=create_app({**config,"FINDER_PILOT_INITIAL_INVITE_EXPIRES":str(expiry+3600)})
        with restarted.app_context():
            self.assertEqual(get_db().execute("SELECT expires_at FROM invitations WHERE digest=?",(digest,)).fetchone()[0],expiry)
        invited=started.test_client()
        self.assertEqual(self.signup(invited,code,"smoke").status_code,302)
        self.assertEqual(invited.get("/team",base_url=self.origin).status_code,403)
        self.assertEqual(self.signup(restarted.test_client(),self.owner_code,"owner").status_code,302)
        final=create_app(config)
        self.assertEqual(self.signup(final.test_client(),code,"replay").status_code,400)
        expired=secrets.token_urlsafe(32)
        final=create_app({**config,"FINDER_PILOT_INITIAL_INVITE_HASH":hashlib.sha256(expired.encode()).hexdigest(),
                          "FINDER_PILOT_INITIAL_INVITE_EXPIRES":str(int(time.time())-1)})
        self.assertEqual(self.signup(final.test_client(),expired,"too-late").status_code,400)

    def test_invalid_username_existing_webhook_foreign_origin_and_untrusted_scheme(self):
        self.signup(self.owner,self.owner_code,"owner")
        me={"id":987654,"username":"MyTestPilotBot","is_bot":True}
        with patch("pilot.flow_worker.telegram",side_effect=[{**me,"username":"AnotherBot"}]) as calls:
            self.assertEqual(self.post(self.owner,"/team/telegram",self.bot_form()).status_code,400)
            self.assertEqual(len(calls.call_args_list),1)
        with patch("pilot.flow_worker.telegram",side_effect=[me,{"url":"https://existing.example/hook"}]) as calls:
            response=self.post(self.owner,"/team/telegram",self.bot_form())
            self.assertEqual(response.status_code,409)
            self.assertEqual([c.args[0] for c in calls.call_args_list],["getMe","getWebhookInfo"])
            self.assertNotIn("existing.example",response.get_data(as_text=True))
        with patch("pilot.flow_worker.telegram") as calls:
            self.assertEqual(self.post(self.owner,"/team/telegram",self.bot_form(),headers={"Origin":"https://other.example"}).status_code,403)
            self.assertEqual(self.post(self.owner,"/team/telegram",{**self.bot_form(),"dedicated":""}).status_code,400)
            self.assertEqual(self.post(self.owner,"/team/telegram",self.bot_form(),environ_overrides={"wsgi.url_scheme":"http"}).status_code,403)
            self.assertFalse(calls.called)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT ciphertext FROM team_bot").fetchone())
        tester=self.app.test_client()
        self.signup(tester,self.invite(),"tester")
        self.assertEqual(self.post(tester,"/team/telegram",self.bot_form()).status_code,403)
        self.app.config["PILOT_TELEGRAM_BOT_TOKEN"]="explicit:hosting"
        with patch("pilot.flow_worker.telegram") as calls:
            self.assertEqual(self.post(self.owner,"/team/telegram",self.bot_form()).status_code,409)
            self.assertFalse(calls.called)

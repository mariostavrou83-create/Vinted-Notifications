"""Inspect the actual buying/selling controls using only synthetic account data."""
import unittest
from pathlib import Path

from flask import Flask, render_template
from lxml import html


TEMPLATES = Path(__file__).resolve().parents[1] / 'web_ui_plugin' / 'templates'


class HomeRoutingUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(TEMPLATES))
        self.app.add_url_rule('/connections', 'connections', lambda: '')
        self.app.add_url_rule('/vinted/reconnect', 'vinted_reconnect.page', lambda: '')
        self.routing = {
            'revision': 'opaque-r1',
            'sources': [
                {'id': 'original', 'label': 'Original Home', 'kind': 'original',
                 'connected': True, 'destinations': ['individual', 'team']},
                {'id': 'web-2', 'label': 'Weekend <Home>', 'kind': 'browser',
                 'connected': False, 'destinations': ['team']},
            ],
            'destinations': [
                {'id': 'individual', 'label': 'Private chat', 'kind': 'private'},
                {'id': 'team', 'label': 'Sourcing & Co', 'kind': 'group', 'removable': True},
            ],
        }

    def render(self, name='msj_home_routing.html', **kw):
        with self.app.test_request_context('/connections'):
            return html.fromstring(render_template(name, csrf='offline-token', **kw))

    def test_original_and_chrome_homes_can_share_one_chat_or_select_several(self):
        page = self.render(home_routing=self.routing)
        forms = page.xpath('//form[input[@name="action"][@value="routes"]]')
        self.assertEqual(len(forms), 2)
        for form, source in zip(forms, self.routing['sources']):
            self.assertEqual(form.get('method'), 'post')
            self.assertEqual(form.get('action'), '/api/home/telegram-routing')
            self.assertEqual(form.xpath('./input[@name="revision"]/@value'), ['opaque-r1'])
            self.assertEqual(form.xpath('.//input[@name="source"]/@value'), [source['id']])
            self.assertEqual(form.xpath('.//input[@name="csrf"]/@value'), ['offline-token'])
            self.assertEqual(form.xpath('.//input[@name="destination"]/@value'), ['individual', 'team'])
            self.assertEqual(form.xpath('.//input[@checked]/@value'), source['destinations'])
        self.assertIn('Weekend <Home>', page.text_content())
        self.assertFalse(page.xpath('//home'))
        self.assertEqual(page.xpath('//form[input[@value="remove"]]/input[@name="source"]/@value'), ['web-2'])
        self.assertEqual(page.xpath('//form[input[@value="remove_destination"]]/input[@name="destination"]/@value'), ['team'])
        self.assertFalse(page.xpath('//input[@name="chat_id" or @name="autobuy" or @name="password"]'))

    def test_separate_single_use_code_disclosures_and_empty_state(self):
        home = {'code': 'test-home', 'expires_in': 600, 'origin': 'https://finder.test'}
        dest = {'code': 'test-chat', 'expires_in': 480}
        page = self.render(home_routing={'revision':'r1','sources':[],'destinations':[]},
                           home_pairing=home, home_destination_pairing=dest)
        self.assertIn('No Home feeds', page.text_content())
        self.assertIn('test-home', page.text_content())
        self.assertIn('/home_alerts test-chat', page.text_content())
        self.assertIn('https://finder.test', page.text_content())
        self.assertEqual(page.xpath('//details[@open]/summary/text()'),
                         ['Manage Telegram chats', 'Pair another Vinted Home'])
        self.assertTrue(page.xpath('//a[@href="/connections/home-companion.zip"]'))
        self.assertCountEqual(page.xpath('//form/input[@name="action"]/@value'), ['pair', 'pair_destination'])
        self.assertFalse(page.xpath('//input[@value="test-home" or @value="test-chat"]'))
        without_codes = self.render(home_routing=self.routing)
        self.assertNotIn('test-home', without_codes.text_content())
        self.assertNotIn('/home_alerts', without_codes.text_content())
        self.assertFalse(self.render().xpath('//form'))
        without_owner = self.render(home_routing={
            'sources': [{'kind': 'original', 'connected': False}], 'destinations': [], 'revision': 'r0'
        })
        self.assertIn('Connect the dashboard owner', without_owner.text_content())
        self.assertFalse(without_owner.xpath('//form'))

    def test_seller_and_buyer_checks_submit_only_their_own_role(self):
        for role, action in (('selling','seller_recheck'), ('buying','buyer_recheck')):
            with self.subTest(role=role):
                account={'connected':True, 'username':'test buyer' if role=='buying' else 'test seller',
                         'connection_health':{'state':'awaiting_verification', 'message':'Recheck in Finder',
                                              'last_verified':'Monday', 'last_checked':'Tuesday'}}
                page=self.render('msj_account_connection.html',account=account,account_role=role)
                form, = page.xpath('//form')
                self.assertEqual(form.xpath('button/@value'), [action])
                self.assertEqual(form.xpath('input[@name="csrf"]/@value'), ['offline-token'])
                self.assertEqual('account=selling' in form.get('action'), role=='selling')
                self.assertTrue(page.xpath('//details/summary'))
                self.assertEqual(bool(page.xpath('//a[@href="/vinted/reconnect"]')),role=='buying')
                self.assertIn('awaiting verification', page.text_content())
                self.assertNotIn('AutoBuy', page.text_content())
        disconnected=self.render('msj_account_connection.html',account={'connected':False},account_role='buying')
        self.assertFalse(disconnected.xpath('//form'))

    def test_paired_chrome_reconnect_shows_heartbeat_health_separately(self):
        source = self.routing['sources'][1]
        source.update(connected=True)
        for state, label in [('waiting','Waiting for the first'), ('active','checked in within 10 minutes'),
                             ('stale','stopped checking in')]:
            with self.subTest(state=state):
                source['state']=state
                page=self.render(home_routing=self.routing)
                self.assertIn(label, page.text_content())
                self.assertEqual(page.xpath('//form[button[text()="Refresh Home status"]]/@method'), ['get'])
                self.assertIn('paired desktop Chrome profile', page.text_content())


class SellerCheckNavigationTests(unittest.TestCase):
    def test_owner_returns_to_the_seller_page_after_success_or_error(self):
        from unittest.mock import patch
        from test_dashboard import DashboardTests

        owner=DashboardTests()
        owner.setUp()
        try:
            owner.owner()
            for failure in (None, ValueError('Offline test failure')):
                with self.subTest(failure=bool(failure)), patch(
                    'vinted_seller.check_saved_connection', return_value='Check complete', side_effect=failure
                ) as check:
                    response=owner.client.post('/connections?account=selling',data={
                        'csrf':'offline-csrf','action':'seller_recheck'
                    })
                    self.assertEqual(response.status_code,303)
                    self.assertEqual(response.location,'/connections?account=selling#vinted-selling')
                    check.assert_called_once_with()
        finally:
            owner.tearDown()

    def test_saved_routes_round_trip_through_current_buying_dashboard(self):
        from contextlib import closing
        from werkzeug.datastructures import MultiDict
        from test_dashboard import DashboardTests
        from search_settings import connection

        owner=DashboardTests()
        owner.setUp()
        try:
            owner.owner()
            with closing(connection()) as conn, conn:
                conn.execute("INSERT OR REPLACE INTO parameters(key,value) VALUES('telegram_chat_id','81234567')")
                conn.execute("INSERT INTO home_route_sources(owner,id,label,account) VALUES(81234567,'test-home','Second Home','2456')")
                conn.execute("INSERT INTO home_route_destinations(owner,id,label,chat) VALUES(81234567,'test-chat','Test team',-99888)")
            def route_forms():
                response=owner.client.get('/connections')
                self.assertEqual(response.status_code,200)
                doc=html.fromstring(response.data)
                forms=doc.xpath('//form[input[@name="action"][@value="routes"]]')
                return {f.xpath('input[@name="source"]/@value')[0]:f for f in forms}
            def submit(key, selected):
                form=route_forms()[key]
                values=MultiDict((name,value) for name,value in form.form_values() if name!='destination')
                for dest in selected:
                    values.add('destination',dest)
                result=owner.client.post(form.action,data=values)
                self.assertEqual((result.status_code,result.location),(303,'/connections#home-routing'))
            submit('original',['owner','test-chat'])
            submit('test-home',['test-chat'])
            actual=route_forms()
            self.assertCountEqual(actual['original'].xpath('.//input[@checked]/@value'),['owner','test-chat'])
            self.assertEqual(actual['test-home'].xpath('.//input[@checked]/@value'),['test-chat'])
            submit('test-home',[])
            self.assertFalse(route_forms()['test-home'].xpath('.//input[@checked]'))
            selling=html.fromstring(owner.client.get('/connections?account=selling').data)
            self.assertFalse(selling.xpath('//*[@id="home-routing"]'))
        finally:
            owner.tearDown()

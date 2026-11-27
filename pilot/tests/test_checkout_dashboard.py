"""Buyer-facing controls at the signed-in entry point."""
import os
import tempfile
import unittest
from html.parser import HTMLParser

from pilot.app import create_app
from pilot.db import get_db


class DashboardMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.inputs = {}
        self.links = set()

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if "id" in fields:
            self.ids.add(fields["id"])
        if tag == "input":
            self.inputs[fields.get("name")] = fields
        if tag == "a":
            self.links.add(fields.get("href"))


class ChromeDashboardTests(unittest.TestCase):
    def test_pilot_exposes_device_pairing_privately_and_accepts_displayed_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app({"TESTING": True, "DATABASE": os.path.join(tmp, "pilot.sqlite")})
            with app.app_context():
                get_db().execute("INSERT INTO users VALUES (1,'buyer','fixture')")
            client = app.test_client()
            self.assertEqual(client.get('/api/native/status').status_code, 401)
            with client.session_transaction() as session:
                session.update(user_id=1, csrf_token='fixture')
            markup = DashboardMarkup()
            markup.feed(client.get('/').get_data(as_text=True))
            self.assertTrue({'native-pair-form', 'native-revoke', 'native-last-seen', 'native-preferences',
                             'native-status-error', 'native-pair-code', 'native-home-status', 'buyer-account-status'} <= markup.ids)
            self.assertIn('/import/vinted', markup.links)
            self.assertIn('#chrome-checkout', markup.links)
            self.assertEqual(markup.inputs['card_last4']['maxlength'], '4')
            self.assertEqual(markup.inputs['pickup_id']['type'], 'hidden')
            self.assertEqual(markup.inputs['home_address_ref']['type'], 'hidden')
            with client.get('/static/chrome-checkout.js') as script:
                self.assertEqual(script.status_code, 200)
            self.assertFalse(client.get('/api/native/status').json['paired'])

            csrf = {'X-CSRF-Token':'fixture'}
            issue = client.post('/api/native/pair-code', headers=csrf, json={'vinted_username':'shopper'})
            self.assertEqual(issue.status_code, 200)
            self.assertEqual(issue.json['pilot_username'], 'buyer')
            code = issue.json['code']
            self.assertNotIn(code, client.get('/').get_data(as_text=True))
            received = client.post('/native/device/pair', json={
                'code':code, 'vinted_username':'shopper', 'pilot_username':'buyer',
            })
            self.assertEqual(received.status_code, 200)
            details = {'delivery_mode':'pickup','location_label':'Paddington Post Office','postcode':'W2 1UA',
                       'preferred_courier':'InPost','allowed_couriers':['Evri'],'allow_same_shop_fallback':True,
                       'payment_label':'Visa •••• 4242'}
            self.assertEqual(client.post('/api/native/settings', headers=csrf, json=details).status_code, 200)
            paired = client.get('/api/native/status').json
            self.assertTrue(paired['paired'])
            self.assertEqual(paired['vinted_username'], 'shopper')
            self.assertEqual(paired['preferences']['location_label'], details['location_label'])
            self.assertEqual(paired['preferences']['payment_label'], details['payment_label'])
            self.assertEqual(client.post('/api/native/disconnect', headers=csrf,json={}).status_code, 200)
            self.assertFalse(client.get('/api/native/status').json['paired'])


if __name__ == '__main__':
    unittest.main()

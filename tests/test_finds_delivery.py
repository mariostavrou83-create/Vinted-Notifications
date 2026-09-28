"""Restart recovery, transport scheduling and dashboard data preservation."""
from contextlib import closing
import io
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telegram.error import NetworkError, RetryAfter, BadRequest
import alert_delivery as delivery
import dashboard_store as store
import db
import search_settings as settings
from test_search_controls import DatabaseFixture
import test_dashboard as dashboard_tests
from test_dashboard import photo_bytes


def outbox(item_id):
    with closing(settings.connection()) as conn:
        row=conn.execute('SELECT * FROM alert_outbox WHERE item_id=?',(str(item_id),)).fetchone()
        return dict(row) if row else None


class OutboxTests(DatabaseFixture, unittest.TestCase):
    def test_atomic_write_rolls_back_seen_item_and_watermark_on_outbox_failure(self):
        with closing(settings.connection()) as conn,conn:
            conn.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON alert_outbox BEGIN SELECT RAISE(ABORT,'disk simulation'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.batch(1,[110])
        self.assertFalse(db.is_item_in_db_by_id(110))
        self.assertEqual(db.get_last_timestamp(1),100)
        self.assertIsNone(outbox(110))
        with closing(settings.connection()) as conn,conn: conn.execute('DROP TRIGGER fail_outbox')
        self.batch(1,[110])
        self.assertEqual(outbox(110)['status'],'pending')

    def test_restart_lease_recovery_dedup_and_silent_baseline(self):
        self.batch(1,[110])
        self.batch(2,[110])
        first=delivery.claim(now=1000)
        self.assertEqual(first['item_id'],'110')
        self.assertIsNone(delivery.claim(now=1119))
        recovered=delivery.claim(now=1121)
        self.assertNotEqual(first['lease_token'],recovered['lease_token'])
        delivery.finish(first,message_id=1,now=1122) # stale worker cannot overwrite new lease
        self.assertEqual(outbox(110)['status'],'pending')
        delivery.finish(recovered,message_id=42,now=1123)
        self.assertEqual(outbox(110)['telegram_message_id'],42)
        self.assertIsNone(delivery.claim(now=2000))
        db.add_query_to_db('https://www.vinted.co.uk/catalog?search_text=new')
        self.batch(45,[111])
        self.assertIsNone(outbox(111))
        self.batch(45,[112])
        self.assertIsNotNone(outbox(112))

    def test_migration_from_dashboard_schema_preserves_auth_media_preferences(self):
        from web_ui_plugin.web_ui import create_app
        create_app({'TESTING':True})
        store.save_search(1,dict(query_name='Fur',query=db.get_queries()[0][1],revision='0',reminder='Keep me',exclusions='teddy'),b'preserved blob')
        with closing(settings.connection()) as conn,conn:
            conn.execute("UPDATE parameters SET value='2' WHERE key='msj_search_schema'")
            originals={table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)]
                       for table in ('queries','items','search_preferences','search_dashboard','dashboard_media','dashboard_auth')}
        backup=settings.ensure_schema()
        self.assertIsNotNone(backup)
        with closing(settings.connection()) as conn:
            for table,rows in originals.items():
                self.assertEqual(rows,[tuple(r) for r in conn.execute('SELECT * FROM '+table)],table)


class WorkerTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def robot(self):
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            send_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='cached')])) )
        return delivery.DeliveryWorker(bot,'123')

    async def test_photo_retry_yields_to_new_listing_without_resending_confirmed_one(self):
        store.save_search(1,dict(query_name='Fur',query=db.get_queries()[0][1],revision='0'),b'photo')
        self.batch(1,[110])
        worker=self.robot()
        await worker.tick(now=1000)
        self.assertEqual(outbox(110)['status'],'sent')
        self.assertEqual(outbox(110)['photo_status'],'pending')
        worker.bot.send_photo.side_effect=NetworkError('offline')
        await worker.tick(now=1002)
        self.batch(2,[111])
        restarted=delivery.DeliveryWorker(worker.bot,'123')
        await restarted.tick(now=1004)
        self.assertEqual(outbox(111)['status'],'sent')
        worker.bot.send_photo.side_effect=None
        await restarted.tick(now=1006)
        self.assertEqual(outbox(110)['photo_status'],'sent')
        self.assertEqual(worker.bot.send_message.await_count,2)
        self.assertEqual(worker.bot.send_photo.await_args.kwargs['reply_parameters'].message_id,42)
        self.assertTrue(worker.bot.send_photo.await_args.kwargs['disable_notification'])

    async def test_rate_limit_persists_across_restart_and_permanent_error_is_visible(self):
        self.batch(1,[110,111])
        worker=self.robot()
        worker.bot.send_message.side_effect=RetryAfter(30)
        await worker.tick(now=1000)
        self.assertIsNone(delivery.claim(now=1030))
        worker=delivery.DeliveryWorker(worker.bot,'123')
        worker.bot.send_message.side_effect=BadRequest('invalid')
        await worker.tick(now=1032)
        self.assertEqual(outbox(111)['status'],'failed')
        self.assertEqual(outbox(111)['error'],'BadRequest')
        worker.bot.send_message.side_effect=None
        await worker.tick(now=1034)
        self.assertEqual(outbox(110)['status'],'sent')

    async def test_network_failure_stays_pending_and_cached_photo_can_fall_back(self):
        store.save_search(1,dict(query_name='Fur',query=db.get_queries()[0][1],revision='0'),b'photo')
        reference=settings.get_search(1)['reference_id']
        store.cache_telegram_photo(reference,'stale')
        self.batch(1,[110])
        worker=self.robot()
        worker.bot.send_message.side_effect=NetworkError('timeout')
        await worker.tick(now=1000)
        self.assertEqual(outbox(110)['status'],'pending')
        worker.bot.send_message.side_effect=None
        await worker.tick(now=1003)
        worker.bot.send_photo.side_effect=BadRequest('bad file')
        await worker.tick(now=1005)
        self.assertIsNone(store.get_media(reference)['telegram_file_id'])
        worker.bot.send_photo.side_effect=None
        await worker.tick(now=1008)
        self.assertIsInstance(worker.bot.send_photo.await_args.kwargs['photo'],io.BytesIO)
        self.assertEqual(outbox(110)['photo_status'],'sent')


class FindsTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from web_ui_plugin.web_ui import create_app
        self.app=create_app({'TESTING':True,'SESSION_COOKIE_SECURE':False})
        self.client=self.app.test_client()

    owner=dashboard_tests.DashboardTests.owner
    form=dashboard_tests.DashboardTests.form

    def test_guide_folder_edit_preserves_history_and_one_photo(self):
        self.owner()
        originals=db.get_queries()
        folder=store.save_folder('Hollister')
        form=self.form(folder_id=str(folder),max_buy='15.25',resale_low='30',resale_high='45.50',must_have='Fur <hood> & fitted',photo=(io.BytesIO(photo_bytes()),'one.png'))
        response=self.client.post('/search/1',data=form,content_type='multipart/form-data')
        self.assertEqual(response.location,'/')
        row=settings.get_search(1)
        self.assertEqual(row['max_buy'],1525)
        self.assertEqual(row['resale_high'],4550)
        self.assertEqual(row['folder_name'],'Hollister')
        self.assertEqual([r[:3] for r in originals],[r[:3] for r in db.get_queries()])
        content=self.batch(1,[110])[0][0]
        self.assertIn('£15.25',content)
        self.assertIn('£30.00–£45.50',content)
        self.assertIn('Fur &lt;hood&gt; &amp; fitted',content)
        self.assertEqual(len(self.batch(1,[110])),0)
        self.assertIn(b'value="15.25"',self.client.get('/search/1').data)
        self.assertIn(b'Hollister',self.client.get('/?folder='+str(folder)).data)
        store.save_folder('Fur favourites',folder)
        self.assertEqual(settings.get_search(1)['folder_name'],'Fur favourites')
        store.delete_folder(folder)
        self.assertIsNone(settings.get_search(1)['folder_id'])
        self.assertEqual(settings.get_search(1)['reference_id'],row['reference_id'])
        self.assertEqual(len(db.get_queries()),44)

    def test_finds_filters_status_private_routes_and_csrf(self):
        for route in ('/finds','/folders'):
            self.assertEqual(self.client.get(route).location,'/login')
        self.owner()
        self.batch(1,[110,111],title='Fur hood & cuffs')
        self.assertEqual(self.client.get('/finds').status_code,200)
        self.assertEqual(self.client.post('/finds/110/status',data={'new_status':'bought'}).status_code,400)
        response=self.client.post('/finds/110/status',data={'csrf':'offline-csrf','new_status':'bought','q':'Fur','page':'1'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(outbox(110)['user_status'],'bought')
        rows,total=store.list_finds(text='cuffs',status='bought')
        self.assertEqual([r['item_id'] for r in rows],['110'])
        self.assertEqual(total,1)
        self.assertEqual(store.list_finds(text='%')[1],0)
        self.assertIn(b'Bought',self.client.get('/finds?status=bought').data)
        self.assertIsNone(store.safe_photo_url('https://evil.test/a.jpg'))
        self.assertEqual(store.safe_photo_url('https://images1.vinted.net/a.jpg'),'https://images1.vinted.net/a.jpg')
        self.assertEqual(self.client.get('/folders').status_code,200)
        self.assertEqual(self.client.post('/folders',data={'csrf':'offline-csrf','name':'Jackets'}).status_code,302)
        with self.assertRaises(ValueError): store.save_folder('jackets')

    def test_invalid_guide_is_atomic_and_retained_in_form(self):
        self.owner()
        original=settings.get_search(1)
        for low,high in [('40','20'),('nan','30'),('15.001','30'),('-1','30')]:
            form=self.form(resale_low=low,resale_high=high)
            response=self.client.post('/search/1',data=form)
            self.assertEqual(response.status_code,200)
            self.assertEqual(settings.get_search(1),original)
        self.assertEqual(store.parse_money('0.01'),1)
        self.assertIsNone(store.parse_money(''))


if __name__=='__main__': unittest.main()

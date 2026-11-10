import base64
import unittest
from unittest.mock import patch

from tests import test_scout_web as fixture
from tests.test_sticker_previews import PNG


class TwitchScoutWebTests(unittest.TestCase):
    setUp=fixture.ScoutWebTests.setUp
    call=fixture.ScoutWebTests.call

    def tearDown(self):
        self.server.worker.twitch_scout.close();fixture.ScoutWebTests.tearDown(self)

    def test_browser_and_catalog_are_admin_only_and_mutations_need_csrf(self):
        for path in ('/api/scout/twitch','/api/scout/twitch/frame','/api/twitch/emotes','/api/twitch/emotes/image?id=1'):
            self.assertEqual(self.call('GET',path)[0],401)
        for path in ('/api/scout/twitch/open','/api/scout/twitch/input','/api/scout/twitch/disconnect','/api/scout/twitch/session','/api/twitch/emotes/save','/api/twitch/emotes/import'):
            self.assertEqual(self.call('POST',path,{},self.cookie,csrf='')[0],403)

    def test_image_upload_preview_and_description_need_no_telegram_token(self):
        data={'provider':'twitch','code':'ExampleChannelSmile','source':'example_channel','description':'Радость',
              'image_data':'data:image/png;base64,'+base64.b64encode(PNG).decode()}
        status,_,result=self.call('POST','/api/twitch/emotes/save',data,self.cookie)
        self.assertEqual(status,200,result);key=result['emotes'][0]['id']
        status,headers,raw=self.call('GET','/api/twitch/emotes/image?id='+str(key),cookie=self.cookie,binary=True)
        self.assertEqual(status,200);self.assertEqual(raw,PNG)
        self.assertEqual(headers['Content-Type'],'image/png');self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertFalse(self.server.store.setting('telegram_token'))

    def test_browser_inputs_are_forwarded_without_saving_password(self):
        browser=self.server.worker.twitch_scout.browser
        with patch.object(browser,'request',return_value={'ok':True}) as request:
            status,_,_=self.call('POST','/api/scout/twitch/input',{'kind':'text','text':'temporary-login-secret'},self.cookie)
        self.assertEqual(status,200)
        self.assertEqual(request.call_args.args[1]['text'],'temporary-login-secret')
        self.assertNotIn('temporary-login-secret',str([tuple(row) for row in self.server.store.db.execute('SELECT key,value FROM settings')]))

    def test_open_endpoint_does_not_accept_arbitrary_url(self):
        status,_,_=self.call('POST','/api/scout/twitch/open',{'destination':'http://127.0.0.1/secret'},self.cookie)
        self.assertEqual(status,400)


    def test_session_transfer_does_not_save_token_or_enable_replies_or_claim_authentication(self):
        scout=self.server.worker.twitch_scout
        scout.configure({'channel':'example_channel','account_login':'assistant_scout','enabled':False,'replies':False})
        token='fixture_session_token_1234567890'
        with patch.object(scout.browser,'start'),patch.object(scout.browser,'request',return_value={'transferred':True}) as request:
            status,_,result=self.call('POST','/api/scout/twitch/session',{'token':token},self.cookie)
        self.assertEqual(status,200);self.assertTrue(result['transferred']);self.assertFalse(result['authenticated'])
        self.assertFalse(result['settings']['replies']);self.assertNotIn(token,str(result))
        self.assertEqual(request.call_args_list[0].args,('session',{'token':token}))
        for table in ('settings','state','scout_events','scout_twitch_outbox'):
            self.assertNotIn(token,str([tuple(row) for row in self.server.store.db.execute('SELECT * FROM '+table)]))

    def test_invalid_session_value_is_rejected_before_opening_browser(self):
        with patch.object(self.server.worker.twitch_scout.browser,'start') as start:
            status,_,result=self.call('POST','/api/scout/twitch/session',{'token':'bad\r\nvalue'},self.cookie)
        self.assertEqual(status,400);start.assert_not_called()

    def test_private_log_export_and_manual_account_links_require_admin_and_csrf(self):
        for path in ('/api/scout/twitch/log','/api/scout/twitch/export?day=2026-11-10','/api/repost-stats'):
            self.assertEqual(self.call('GET',path)[0],401)
        self.assertEqual(self.call('POST','/api/social/link',{'twitch_id':'1','telegram_id':'2'},self.cookie,csrf='')[0],403)
        self.assertEqual(self.call('GET','/api/scout/twitch/export?day=invalid',cookie=self.cookie)[0],400)
        status,headers,raw=self.call('GET','/api/scout/twitch/export?day=2026-11-10',cookie=self.cookie,binary=True)
        self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'no-store');self.assertIn(b'2026-11-10',raw)

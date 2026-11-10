import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch,Mock

from tests import test_reply_context as fixture
from repostbot.twitch import parse_irc_line


class TwitchScoutTests(unittest.TestCase):
    setUp=fixture.ReplyContextTests.setUp

    def tearDown(self):
        self.worker.twitch_scout.close()
        fixture.ReplyContextTests.tearDown(self)

    def configure(self,replies=False):
        scout=self.worker.twitch_scout
        with patch.object(scout.browser,'start'):
            scout.configure({'enabled':True,'channel':'example_channel','account_login':'assistant_scout','replies':replies,'external_emotes':False})
        self.store.set_settings({'community_enabled':'0'})
        scout.wire('NICK assistant_scout',True)
        scout.wire('@user-id=999;display-name=Assistant_scout :tmi.twitch.tv GLOBALUSERSTATE')
        scout.wire('@room-id=12 :tmi.twitch.tv ROOMSTATE #example_channel')
        return scout

    @staticmethod
    def line(key='m1',person='7',text='Привет',channel='example_channel',emotes=''):
        return '@id='+key+';user-id='+person+';display-name=SampleUser;emotes='+emotes+' :sample_user!sample_user@tmi.twitch.tv PRIVMSG #'+channel+' :'+text

    def drain(self):
        self.worker.twitch_scout.ingest_pool.submit(lambda:None).result(timeout=3)
        self.worker.twitch_scout.book_pool.submit(lambda:None).result(timeout=3)

    def test_repeated_wire_messages_are_accounted_once_and_own_messages_are_ignored(self):
        scout=self.configure()
        with patch.object(self.worker.community,'observe',return_value=None) as book:
            scout.wire(self.line());scout.wire(self.line());scout.wire(self.line('own','999','Моя реплика').replace('sample_user!sample_user','assistant_scout!assistant_scout'))
            self.drain()
        book.assert_called_once()
        self.assertEqual(scout.panel()['messages'],1)
        self.assertEqual(self.worker.community.stats()['total'],1)

    def test_channel_and_identity_are_bound_to_configured_account(self):
        scout=self.configure(replies=True)
        scout.wire(self.line(channel='other'));self.drain()
        self.assertEqual(scout.panel()['messages'],0)
        scout.wire('NICK another_account',True)
        scout.wire('@user-id=555 :tmi.twitch.tv GLOBALUSERSTATE')
        self.assertFalse(scout.authenticated);self.assertIsNone(scout.send('Привет'))

    def test_primary_oauth_configuration_is_preserved_and_duplicate_primary_reply_is_suppressed(self):
        self.store.set_settings({'twitch_channel':'example_channel','twitch_chat_access':'saved-token','twitch_chat_refresh':'saved-refresh'})
        scout=self.configure()
        self.worker.twitch.on_chat(parse_irc_line(self.line()))
        self.assertEqual(self.worker.community.stats()['total'],0)
        scout.wire(self.line());self.drain()
        self.assertEqual(self.worker.community.stats()['total'],1)
        self.assertEqual(self.store.setting('twitch_chat_access'),'saved-token')
        self.assertEqual(self.store.setting('twitch_chat_refresh'),'saved-refresh')

    def test_read_only_mode_never_generates_a_direct_answer(self):
        scout=self.configure()
        with patch.object(self.worker.conversation,'generate') as model,patch.object(scout.browser,'request') as browser:
            scout.wire(self.line(text='помощник, привет'))
            self.drain();scout.pool.submit(lambda:None).result(timeout=3)
        model.assert_not_called();browser.assert_not_called()
        self.assertEqual(self.worker.community.stats()['total'],1)

    def test_legacy_reply_setting_keeps_persona_but_does_not_enable_paused_ai_answers(self):
        scout=self.configure(replies=True)
        event=parse_irc_line(self.line(text='помощник, приём'))
        with patch('repostbot.ai.response_text') as model,patch.object(scout,'send') as send:
            self.worker.community.record_chat(event)
            scout.on_chat(event,recorded=True)
        model.assert_not_called();send.assert_not_called()
        self.assertTrue(scout.settings()['prompt'])

    def test_disabling_replies_during_generation_discards_the_draft(self):
        scout=self.configure(replies=True)
        event=parse_irc_line(self.line(text='помощник, приём'))
        def generate(*args,**kwargs):
            scout.configure({'replies':False});return {'action':'text','text':'Поздний ответ'}
        with patch.object(self.worker.conversation,'generate',side_effect=generate),patch.object(scout.browser,'request') as browser:
            scout._reply(event,'Приём')
        browser.assert_not_called();self.assertEqual(scout.panel()['outbox'],[])

    def test_negative_notice_is_not_retried_and_never_marks_sent(self):
        scout=self.configure(replies=True)
        def browser(action,data=None,wait=True):
            text=scout.prepare_send(data['id']);scout.wire('PRIVMSG #example_channel :'+text,True)
            scout.wire('@msg-id=msg_banned :tmi.twitch.tv NOTICE #example_channel :Cannot send')
        with patch.object(scout.browser,'request',side_effect=browser):
            with self.assertRaises(RuntimeError):scout.send('Привет')
        self.assertEqual(scout.panel()['outbox'][0]['status'],'failed')

    def test_modern_native_emote_id_is_counted_for_each_occurrence(self):
        scout=self.configure()
        key='emotesv2_aabbcc'
        scout.wire(self.line(text='Hey Hey',emotes=key+':0-2,4-6'));self.drain()
        uses=self.store.db.execute('SELECT emote_id,amount FROM twitch_emotes').fetchone()
        self.assertEqual(tuple(uses),(key,2))

    def test_oauth_frames_are_never_saved(self):
        scout=self.configure();secret='oauth:must-not-be-stored'
        scout.wire('PASS '+secret,True)
        with self.store.lock:
            values=' '.join(str(tuple(row)) for row in self.store.db.execute('SELECT key,value FROM state'))
        self.assertNotIn(secret,values)

    def test_statistics_continue_while_book_processing_is_waiting(self):
        scout=self.configure();entered=threading.Event();release=threading.Event()
        def slow_book(*args,**kwargs):entered.set();release.wait(3)
        with patch.object(self.worker.community,'observe',side_effect=slow_book):
            try:
                scout.wire(self.line('first'));self.assertTrue(entered.wait(2))
                scout.wire(self.line('second'))
                scout.ingest_pool.submit(lambda:None).result(timeout=2)
                self.assertEqual(scout.panel()['messages'],2)
                self.assertEqual(self.worker.community.stats()['total'],2)
            finally:release.set();self.drain()

    def test_unfinished_book_entry_resumes_without_a_late_chat_answer(self):
        scout=self.configure(replies=True)
        event=parse_irc_line(self.line('pending',text='помощник, старый вопрос'))
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO scout_twitch_messages(id,channel,person,at,payload) VALUES (?,?,?,?,?)',
                (event['message_id'],'example_channel','7',1,json.dumps(event)))
        with patch.object(scout.browser,'request') as send:
            scout.resume_book();self.drain();scout.pool.submit(lambda:None).result(timeout=2)
        send.assert_not_called()
        self.assertEqual(self.worker.community.stats()['total'],1)
        self.assertEqual(self.store.db.execute("SELECT processed FROM scout_twitch_messages WHERE id='pending'").fetchone()[0],1)

    def test_old_socket_closing_cannot_disconnect_the_new_socket(self):
        scout=self.configure();page=Mock();handlers=[]
        page.on.side_effect=lambda event,handler:handlers.append(handler)
        scout.browser.attach(page)
        def socket():
            result=Mock();result.url='wss://irc-ws.chat.twitch.tv';result.callbacks={}
            result.on.side_effect=lambda event,handler:result.callbacks.update({event:handler})
            return result
        old,new=socket(),socket();handlers[0](old);handlers[0](new)
        scout.authenticated=True;scout.reading=True
        old.callbacks['close'](old)
        self.assertTrue(scout.authenticated);self.assertTrue(scout.reading)
        new.callbacks['close'](new)
        self.assertFalse(scout.authenticated);self.assertFalse(scout.reading)

    def test_restart_preserves_login_profile_settings_and_catalog(self):
        scout=self.configure();browser=scout.browser
        browser.profile.mkdir();(browser.profile/'saved-session').write_text('persistent fixture')
        before=scout.settings();generation=scout.generation;browser.thread=Mock()
        with patch.object(browser,'start') as start,patch.object(browser,'request') as request,patch.object(browser,'forget') as forget:
            scout.open_browser('restart')
        self.assertEqual(scout.settings(),before);self.assertGreater(scout.generation,generation)
        self.assertEqual((browser.profile/'saved-session').read_text(),'persistent fixture')
        self.assertEqual(request.call_args_list,[unittest.mock.call('pause'),unittest.mock.call('chat',wait=False)])
        start.assert_called_once();forget.assert_not_called();browser.thread=None
        scout.authenticated=False;scout.reading=False
        with patch.object(browser,'start'),patch.object(browser,'request') as request:
            scout.open_browser('restart')
        self.assertEqual(request.call_args.args[0],'login')

    def test_official_chrome_is_preferred_but_chromium_remains_available(self):
        browser=self.worker.twitch_scout.browser
        with patch('repostbot.scout_twitch_browser.shutil.which',side_effect=lambda name:'/usr/bin/'+name if name in ('google-chrome-stable','chromium') else None):
            self.assertEqual(browser.executable(),'/usr/bin/google-chrome-stable')
        with patch('repostbot.scout_twitch_browser.shutil.which',side_effect=lambda name:'/usr/bin/chromium' if name=='chromium' else None):
            self.assertEqual(browser.executable(),'/usr/bin/chromium')


if __name__=='__main__':unittest.main()

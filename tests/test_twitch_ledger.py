import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
from tests import test_twitch_scout as fixture

class LedgerTests(unittest.TestCase):
    setUp=fixture.TwitchScoutTests.setUp
    tearDown=fixture.TwitchScoutTests.tearDown
    configure=fixture.TwitchScoutTests.configure
    line=staticmethod(fixture.TwitchScoutTests.line)
    drain=fixture.TwitchScoutTests.drain

    def test_bots_are_excluded_before_book_stats_and_daily_export(self):
        scout=self.configure();day=datetime.now(timezone.utc).astimezone(__import__('repostbot.twitch_ledger',fromlist=['MSK']).MSK).date().isoformat()
        for i,login in enumerate(('Nightbot','StreamElements','ordinarybotfan')):
            scout.wire(self.line('bot-'+str(i),str(i+10),'hello').replace('sample_user!',login.lower()+'!'))
        self.drain();_,rows=scout.ledger.rows(day)
        self.assertEqual([row['login'] for row in rows],['ordinarybotfan'])
        self.assertEqual(self.worker.community.stats()['total'],1)
        self.assertNotIn('nightbot',scout.ledger.export(day).decode().casefold())

    def test_moscow_day_exports_all_messages_once_and_keeps_emote_and_reply_metadata(self):
        scout=self.configure();from repostbot.twitch import parse_irc_line
        def event(key,clock):
            e=parse_irc_line(self.line(key,text='Тест Kappa',emotes='25:5-9'));e['sent_at']=int(datetime.fromisoformat(clock).timestamp());e['reply_parent_message_id']='parent';return e
        first=event('one','2026-11-10T20:59:59+00:00');second=event('two','2026-11-10T21:00:00+00:00')
        scout.ledger.message(first);scout.ledger.message(first);scout.ledger.message(second)
        one=[json.loads(line) for line in scout.ledger.export('2026-11-10').decode().splitlines()]
        two=[json.loads(line) for line in scout.ledger.export('2026-11-11').decode().splitlines()]
        self.assertEqual(len(one),2);self.assertEqual(len(two),2);self.assertEqual(one[1]['reply_id'],'parent')
        self.assertEqual(one[1]['details']['fragments'][0]['emote']['id'],'25')
        self.assertIn('+03:00',two[1]['time_moscow'])

    def test_reward_without_message_is_logged_separately_without_applying_a_rule(self):
        scout=self.configure();ledger=scout.ledger
        old={'key':1,'html':'<div class="user-notice-line">OldUser redeemed Old reward 100</div>'}
        reward={'key':2,'html':'<div class="user-notice-line"><span data-a-user="sample_user_">SampleUser_</span> получает награду: Съесть немного ресурс 🐼 200</div>'}
        ledger.notices({'page':'page-a','ready':True,'rows':[old]})
        with patch.object(self.worker.community,'adjust') as adjust:
            ledger.notices({'page':'page-a','ready':True,'rows':[old,reward]})
            ledger.notices({'page':'page-a','ready':True,'rows':[old,reward]})
            ledger.notices({'page':'page-b','ready':True,'rows':[old,reward]})
        adjust.assert_not_called();day=datetime.now(__import__('repostbot.twitch_ledger',fromlist=['MSK']).MSK).date().isoformat()
        total,rows=ledger.rows(day,'reward');self.assertEqual(total,1)
        self.assertEqual(rows[0]['cost'],200);self.assertIn('Съесть немного ресурс',rows[0]['title'])

    def test_only_optional_roll_replies_and_legacy_ai_switches_cannot_enable_conversations(self):
        scout=self.configure(replies=True)
        from repostbot.twitch import parse_irc_line
        with patch.object(scout,'send',return_value='sent') as send,patch.object(self.worker.conversation,'generate') as model:
            for text in ('помощник, привет','!rep','!roll d20+2'):
                scout.on_chat(parse_irc_line(self.line(text=text)),recorded=True)
            send.assert_not_called()
            scout.configure({'roll_enabled':True})
            event=parse_irc_line(self.line('roll',text='!roll d20+2'));scout.on_chat(event,recorded=True);scout.on_chat(event,recorded=True)
        self.assertEqual(send.call_count,1);self.assertIn('1d20+2',send.call_args.args[0]);model.assert_not_called()

    def test_read_only_browser_disconnect_does_not_invalidate_direct_chat(self):
        scout=self.configure();scout.store.set_settings({'scout_twitch_settings':json.dumps({**scout.settings(),'transport':'irc'})})
        scout.phase='connected';scout.browser.disconnected()
        self.assertTrue(scout.authenticated);self.assertTrue(scout.reading);self.assertEqual(scout.phase,'connected')

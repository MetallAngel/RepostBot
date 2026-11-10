import io
import json
import threading
import time
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError

from tests import test_reply_context as fixture
from repostbot.scout_twitch_irc import validate_session,InvalidSession


class DirectScoutTests(unittest.TestCase):
    setUp=fixture.ReplyContextTests.setUp
    def tearDown(self):
        self.worker.twitch_scout.close();fixture.ReplyContextTests.tearDown(self)

    def configure(self):
        scout=self.worker.twitch_scout
        with patch.object(scout.irc,'start'):
            scout.configure({'enabled':True,'replies':True,'channel':'fixture','account_login':'fixture_scout','transport':'irc','external_emotes':False})
        self.store.set_settings({'community_enabled':'0'})
        return scout

    def until(self,condition,seconds=5):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if condition():return
            threading.Event().wait(.01)
        self.fail('Condition did not become true')

    def test_validation_needs_no_client_id_and_does_not_reflect_http_body(self):
        token='fixture-private-session-token'
        response=Mock();response.__enter__=Mock(return_value=io.StringIO(json.dumps({'login':'fixture_scout','user_id':'999'})));response.__exit__=Mock(return_value=False)
        with patch('repostbot.scout_twitch_irc.urlopen',return_value=response) as request:
            self.assertEqual(validate_session(token,'fixture_scout'),{'login':'fixture_scout','id':'999'})
        self.assertEqual(request.call_args.args[0].get_header('Authorization'),'OAuth '+token)
        self.assertIsNone(request.call_args.args[0].get_header('Client-id'))
        error=HTTPError('https://id.twitch.tv/oauth2/validate',401,'Unauthorized',{},io.BytesIO(token.encode()))
        with patch('repostbot.scout_twitch_irc.urlopen',side_effect=error):
            with self.assertRaises(InvalidSession) as caught:validate_session(token,'fixture_scout')
        self.assertNotIn(token,str(caught.exception))

    def test_other_account_validation_never_accepts_configured_login(self):
        response=Mock();response.__enter__=Mock(return_value=io.StringIO(json.dumps({'login':'other_user','user_id':'555'})));response.__exit__=Mock(return_value=False)
        with patch('repostbot.scout_twitch_irc.urlopen',return_value=response):
            with self.assertRaisesRegex(InvalidSession,'other_user'):validate_session('fixture-token','fixture_scout')

    def test_real_local_websocket_confirms_account_counts_messages_and_acknowledges_send(self):
        from websockets.sync.server import serve
        scout=self.configure();connections=[];commands=[]
        def handle(socket):
            connections.append(socket)
            for payload in socket:
                for line in payload.split('\r\n'):
                    commands.append(line)
                    if line.startswith('NICK '):socket.send('@user-id=999 :tmi.twitch.tv GLOBALUSERSTATE\r\n')
                    elif line.startswith('JOIN '):socket.send('@room-id=12 :tmi.twitch.tv ROOMSTATE #fixture\r\n')
                    elif line.startswith('PRIVMSG '):socket.send('@id=confirmed-message :tmi.twitch.tv USERSTATE #fixture\r\n')
                    elif line.startswith('PING '):socket.send('PONG :repostbot\r\n')
        with serve(handle,'127.0.0.1',0,subprotocols=['irc']) as server:
            threading.Thread(target=server.serve_forever,daemon=True).start();port=server.socket.getsockname()[1]
            with patch('repostbot.scout_twitch_irc.IRC_URL','ws://127.0.0.1:'+str(port)), \
                 patch.object(scout.irc,'credentials',return_value='fixture-private-token'), \
                 patch('repostbot.scout_twitch_irc.validate_session',return_value={'login':'fixture_scout','id':'999'}):
                scout.irc.start();self.until(lambda:scout.authenticated and scout.reading)
                incoming='@id=human-one;user-id=7;display-name=Alice :alice!alice@tmi.twitch.tv PRIVMSG #fixture :hello'
                connections[0].send(incoming+'\r\n'+incoming+'\r\n')
                self.until(lambda:scout.panel()['messages']==1)
                self.assertEqual(scout.send('Ответ через прямой чат'),'confirmed-message')
                self.assertEqual(scout.panel()['outbox'][0]['status'],'sent')
                self.assertEqual(sum(line.startswith('PRIVMSG ') for line in commands),1)
                self.assertNotIn('fixture-private-token',str([tuple(row) for row in self.store.db.execute('SELECT * FROM settings')]))
                scout.irc.close()
            server.shutdown()

    def test_lost_acknowledgement_is_not_sent_again_after_reconnect(self):
        from websockets.sync.server import serve
        scout=self.configure();connections=[];messages=[]
        def handle(socket):
            connections.append(socket)
            for payload in socket:
                for line in payload.split('\r\n'):
                    if line.startswith('NICK '):socket.send('@user-id=999 :tmi.twitch.tv GLOBALUSERSTATE\r\n')
                    elif line.startswith('JOIN '):socket.send('@room-id=12 :tmi.twitch.tv ROOMSTATE #fixture\r\n')
                    elif line.startswith('PRIVMSG '):messages.append(line);socket.close();return
        with serve(handle,'127.0.0.1',0,subprotocols=['irc']) as server:
            threading.Thread(target=server.serve_forever,daemon=True).start();port=server.socket.getsockname()[1]
            with patch('repostbot.scout_twitch_irc.IRC_URL','ws://127.0.0.1:'+str(port)), \
                 patch.object(scout.irc,'credentials',return_value='fixture-token'), \
                 patch('repostbot.scout_twitch_irc.validate_session',return_value={'login':'fixture_scout','id':'999'}):
                scout.irc.start();self.until(lambda:scout.authenticated and scout.reading)
                with self.assertRaises(RuntimeError):scout.send('Отправить один раз')
                self.until(lambda:len(connections)>1 and scout.authenticated and scout.reading,seconds=6)
                self.assertEqual(len(messages),1);self.assertEqual(scout.panel()['outbox'][0]['status'],'uncertain')
                scout.irc.close()
            server.shutdown()

    def test_unresponsive_open_connection_is_replaced(self):
        from websockets.sync.server import serve
        scout=self.configure();connections=[];pings=[]
        def handle(socket):
            connections.append(socket)
            for payload in socket:
                for line in payload.split('\r\n'):
                    if line.startswith('NICK '):socket.send('@user-id=999 :tmi.twitch.tv GLOBALUSERSTATE\r\n')
                    elif line.startswith('JOIN '):socket.send('@room-id=12 :tmi.twitch.tv ROOMSTATE #fixture\r\n')
                    elif line.startswith('PING '):pings.append(line)  # Deliberately no PONG, but leave TCP open.
        with serve(handle,'127.0.0.1',0,subprotocols=['irc']) as server:
            threading.Thread(target=server.serve_forever,daemon=True).start();port=server.socket.getsockname()[1]
            with patch('repostbot.scout_twitch_irc.IRC_URL','ws://127.0.0.1:'+str(port)), \
                 patch.object(scout.irc,'credentials',return_value='fixture-token'), \
                 patch('repostbot.scout_twitch_irc.validate_session',return_value={'login':'fixture_scout','id':'999'}), \
                 patch('repostbot.scout_twitch_irc.PING_INTERVAL',.05),patch('repostbot.scout_twitch_irc.IDLE_TIMEOUT',.4):
                scout.irc.start();self.until(lambda:scout.authenticated and scout.reading)
                self.until(lambda:len(connections)>1 and scout.authenticated and scout.reading,seconds=6)
                self.assertTrue(pings);scout.irc.close()
            server.shutdown()

    def test_invalid_session_stops_before_chat_socket_is_opened(self):
        scout=self.configure()
        with patch.object(scout.irc,'credentials',return_value='fixture-private-token'), \
             patch('repostbot.scout_twitch_irc.validate_session',side_effect=InvalidSession('Сессия недействительна')), \
             patch('websocket.create_connection') as socket:
            scout.irc.start();self.until(lambda:scout.irc.thread is None)
        socket.assert_not_called();self.assertEqual(scout.phase,'error');self.assertFalse(scout.authenticated)
        self.assertEqual(scout.panel()['outbox'],[])

    def test_channel_or_generation_change_before_write_cancels_the_job(self):
        from concurrent.futures import Future
        scout=self.configure();scout.actor={'id':'999','login':'fixture_scout'};scout.authenticated=True;scout.reading=True
        job={'id':'job','text':'Текст','channel':'fixture','owner':'999','generation':scout.generation,'future':Future(),'transport':'irc','wired':False}
        scout.pending['job']=job;prepare=scout.prepare_send
        def change(key):
            text=prepare(key);scout.generation+=1;return text
        sock=Mock()
        with patch.object(scout,'prepare_send',side_effect=change):self.assertTrue(scout.irc.write_job(sock,'job'))
        sock.send.assert_not_called();self.assertIsInstance(job['future'].exception(),RuntimeError)

    def test_disconnect_does_not_reclassify_a_confirmed_send(self):
        from concurrent.futures import Future
        scout=self.configure();future=Future();future.set_result('confirmed')
        scout.pending['sent']={'id':'sent','future':future,'transport':'irc'}
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT INTO scout_twitch_outbox VALUES (?,?,?,?,?,?,?,?)',('sent','fixture','999','Text','','sent',1,'confirmed'))
        scout.irc.disconnected()
        self.assertEqual(scout.panel()['outbox'][0]['status'],'sent')

"""IRC server negotiation and safe, stage-specific connection failures."""
import json
import ssl
import threading
import unittest
from unittest.mock import patch

from tests import test_scout_twitch_irc as fixture


class ScoutConnectionTests(unittest.TestCase):
    setUp=fixture.DirectScoutTests.setUp
    tearDown=fixture.DirectScoutTests.tearDown
    configure=fixture.DirectScoutTests.configure
    until=fixture.DirectScoutTests.until

    def test_protocol_negotiation_and_separate_commands_wait_for_account_before_join(self):
        from websockets.sync.server import serve
        scout=self.configure();commands=[];connections=[];identified=threading.Event();authorize=threading.Event()
        def handle(socket):
            connections.append(socket)
            for payload in socket:
                self.assertNotIn('\r',payload);self.assertNotIn('\n',payload)
                commands.append(payload)
                if payload.startswith('CAP '):socket.send(':tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands\r\n')
                elif payload.startswith('NICK '):
                    identified.set();authorize.wait(timeout=3)
                    socket.send('@user-id=999 :tmi.twitch.tv GLOBALUSERSTATE\r\n')
                elif payload.startswith('JOIN '):socket.send('@room-id=12 :tmi.twitch.tv ROOMSTATE #fixture\r\n')
                elif payload.startswith('PRIVMSG '):socket.send('@id=confirmed :tmi.twitch.tv USERSTATE #fixture\r\n')
        with serve(handle,'127.0.0.1',0,subprotocols=['irc']) as server:
            threading.Thread(target=server.serve_forever,daemon=True).start();port=server.socket.getsockname()[1]
            with patch('repostbot.scout_twitch_irc.IRC_URL','ws://127.0.0.1:'+str(port)), \
                 patch.object(scout.irc,'credentials',return_value='fixture-private-token'), \
                 patch('repostbot.scout_twitch_irc.validate_session',return_value={'login':'fixture_scout','id':'999'}):
                try:
                    scout.irc.start();self.until(identified.is_set)
                    self.assertEqual(connections[0].subprotocol,'irc')
                    self.assertFalse(scout.authenticated)
                    with self.assertRaises(TimeoutError):connections[0].recv(timeout=.1)
                    authorize.set();self.until(lambda:scout.authenticated and scout.reading)
                    self.assertEqual(scout.send('Только одна команда'),'confirmed')
                    self.assertEqual(sum(item.startswith('JOIN ') for item in commands),1)
                    connections[0].send('@id=control-words;user-id=7 :alice!alice@tmi.twitch.tv PRIVMSG #fixture :Login authentication failed GLOBALUSERSTATE RECONNECT CAP * NAK USERSTATE #fixture\r\n')
                    self.until(lambda:scout.panel()['messages']==1)
                    self.assertTrue(scout.authenticated);self.assertEqual(scout.user_id,'999')
                finally:authorize.set();scout.irc.close()
            server.shutdown()

    def test_network_failures_keep_safe_cause_and_stage_between_retries(self):
        from websocket import WebSocketBadStatusException,WebSocketProxyException,WebSocketAddressException,WebSocketTimeoutException
        scout=self.configure();secret='fixture-private-token'
        cases=[(WebSocketBadStatusException(secret,403,resp_headers={'Secret':secret},resp_body=secret.encode()),'WS_HTTP_403'),
               (ssl.SSLCertVerificationError(secret),'TLS_CERTIFICATE'),
               (WebSocketProxyException(secret),'PROXY_ERROR'),
               (WebSocketAddressException(secret),'DNS_ERROR'),
               (WebSocketTimeoutException(secret),'CONNECT_TIMEOUT'),
               (ConnectionRefusedError(secret),'CONNECTION_REFUSED'),
               (RuntimeError(secret),'CONNECTION_ERROR')]
        for error,code in cases:
            with self.subTest(code=code),patch.object(scout.irc,'credentials',return_value=secret), \
                 patch('repostbot.scout_twitch_irc.validate_session',return_value={'login':'fixture_scout','id':'999'}), \
                 patch('websocket.create_connection',side_effect=error):
                scout.irc.start();self.until(lambda:scout.panel().get('direct_chat',{}).get('failure',{}).get('code')==code)
                status=scout.panel();direct=status['direct_chat']
                self.assertEqual(direct['failure']['stage'],'websocket');self.assertTrue(direct['session_verified'])
                self.assertGreater(direct['retry_at'],0);self.assertIn(code,status['error'])
                self.assertNotIn(secret,json.dumps(status,ensure_ascii=False));self.assertFalse(scout.authenticated)
                scout.irc.close()

    def test_welcome_without_account_id_times_out_as_authentication_not_socket_failure(self):
        from websockets.sync.server import serve
        scout=self.configure()
        def handle(socket):
            for payload in socket:
                if payload.startswith('NICK '):socket.send(':tmi.twitch.tv 001 fixture_scout :Welcome\r\n')
        with serve(handle,'127.0.0.1',0,subprotocols=['irc']) as server:
            threading.Thread(target=server.serve_forever,daemon=True).start();port=server.socket.getsockname()[1]
            with patch('repostbot.scout_twitch_irc.IRC_URL','ws://127.0.0.1:'+str(port)), \
                 patch.object(scout.irc,'credentials',return_value='fixture-token'), \
                 patch('repostbot.scout_twitch_irc.validate_session',return_value={'login':'fixture_scout','id':'999'}), \
                 patch('repostbot.scout_twitch_irc.AUTH_TIMEOUT',.15):
                scout.irc.start();self.until(lambda:scout.panel()['direct_chat']['failure'].get('code')=='AUTH_TIMEOUT')
                self.assertTrue(scout.panel()['direct_chat']['session_verified']);self.assertFalse(scout.authenticated)
                self.assertEqual(scout.panel()['direct_chat']['failure']['stage'],'authenticate');scout.irc.close()
            server.shutdown()

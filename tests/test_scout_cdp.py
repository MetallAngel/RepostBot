import json
import threading
import unittest
from unittest.mock import patch

from repostbot.scout_cdp import Connection,CDPError,Page


class ScoutCDPTests(unittest.TestCase):
    def page(self):
        page=Page.__new__(Page);page.handlers={};page.sockets={};page.loaded=threading.Event()
        page.diagnostic_lock=threading.Lock();page.login_diagnostics=[];page.response_rows={};page.ready_responses=[]
        return page

    def test_diagnostics_keep_only_fixed_endpoint_status_and_timestamp(self):
        page=self.page()
        response={'url':'https://passport.twitch.tv/login?token=private-query', 'status':403,
                  'headers':{'Set-Cookie':'private-cookie'},'body':'private-response'}
        page.event('Network.responseReceived',{'response':response,'requestId':'private-id'})
        result=page.diagnostics()
        self.assertEqual(result[0]['endpoint'],'passport.twitch.tv/login');self.assertEqual(result[0]['status'],403)
        self.assertEqual(set(result[0]),{'endpoint','status','at'})
        self.assertNotIn('private',json.dumps(result))
        for url in ('https://gql.twitch.tv/gql','https://passport.twitch.tv/users/private-account','https://untrusted.test/login'):
            page.event('Network.responseReceived',{'response':{'url':url,'status':200}})
        self.assertEqual(len(page.diagnostics()),1)
        page.event('Network.responseReceived',{'response':{'url':'https://gql.twitch.tv/integrity?private=query','status':403}})
        self.assertEqual(page.diagnostics()[-1]['endpoint'],'gql.twitch.tv/integrity')
        self.assertNotIn('private',json.dumps(page.diagnostics()))
        for status in range(400,414):page.event('Network.responseReceived',{'response':dict(response,status=status)})
        self.assertEqual(len(page.diagnostics()),8)

    def test_socket_frames_keep_direction_and_ignore_binary_frames(self):
        page=self.page();events=[]
        def socket_open(socket):
            socket.on('framesent',lambda text:events.append(('sent',text)))
            socket.on('framereceived',lambda text:events.append(('received',text)))
            socket.on('close',lambda socket:events.append(('closed',socket.url)))
        page.on('websocket',socket_open)
        page.event('Network.webSocketCreated',{'requestId':'a','url':'wss://irc-ws.chat.twitch.tv'})
        page.event('Network.webSocketFrameSent',{'requestId':'a','response':{'opcode':1,'payloadData':'NICK fixture'}})
        page.event('Network.webSocketFrameReceived',{'requestId':'a','response':{'opcode':1,'payloadData':'PRIVMSG #fixture :hello'}})
        page.event('Network.webSocketFrameReceived',{'requestId':'a','response':{'opcode':2,'payloadData':'binary'}})
        page.event('connectionClosed',{})
        self.assertEqual(events,[('sent','NICK fixture'),('received','PRIVMSG #fixture :hello'),('closed','wss://irc-ws.chat.twitch.tv')])
        self.assertFalse(page.sockets)

    def test_runtime_commands_are_rejected_before_any_socket_or_parameters(self):
        connection=Connection.__new__(Connection)
        for method in ('Runtime.enable','Runtime.evaluate','Runtime.addBinding'):
            with self.assertRaisesRegex(CDPError,'Runtime'):connection.call(method,{'expression':'fixture'})

    def test_debugging_endpoint_must_be_our_literal_loopback_address(self):
        for url in ('ws://example.test/page/a','ws://127.0.0.1.evil.test/page/a','wss://127.0.0.1/page/a'):
            with self.assertRaisesRegex(CDPError,'локальный адрес'):Connection(url)

    def test_idle_controller_detects_a_closed_browser_connection(self):
        from types import SimpleNamespace
        page=self.page();page.connection=SimpleNamespace(closed=True)
        with self.assertRaisesRegex(CDPError,'прервалась'):page.wait_for_timeout(0)

    def test_top_level_navigation_updates_url_without_a_renderer_query(self):
        page=self.page();page.current_url='about:blank';page.main_frame=None
        page.event('Page.frameNavigated',{'frame':{'id':'main','url':'https://www.twitch.tv/login'}})
        page.event('Page.frameNavigated',{'frame':{'id':'child','parentId':'main','url':'https://other.test/'}})
        self.assertEqual(page.url,'https://www.twitch.tv/login')
        page.event('Page.navigatedWithinDocument',{'frameId':'main','url':'https://www.twitch.tv/example_channel'})
        self.assertEqual(page.url,'https://www.twitch.tv/example_channel')

    def test_integrity_body_is_reduced_to_booleans_and_numeric_error_code(self):
        from repostbot.scout_cdp import login_result
        raw=json.dumps({'token':'fixture-private-token','error_code':5025,'is_bad_bot':True,
                        'error_description':'private text','user':{'login':'private-login'}})
        self.assertEqual(login_result(raw,True),{'integrity_token_received':True,'rejected_client':True,'error_code':5025})
        self.assertEqual(login_result(raw,False),{'error_code':5025})
        self.assertEqual(login_result('not JSON',True),{})

    def test_response_body_is_read_after_loading_and_never_stored_in_diagnostics(self):
        page=self.page()
        page.event('Network.responseReceived',{'requestId':'fixture-request','response':{'url':'https://gql.twitch.tv/integrity','status':200}})
        page.event('Network.loadingFinished',{'requestId':'fixture-request'})
        from unittest.mock import Mock
        page.call=Mock(return_value={'body':json.dumps({'token':'private-token','error_code':5025})})
        page.inspect_login_results()
        result=page.diagnostics()[0]
        self.assertTrue(result['integrity_token_received']);self.assertEqual(result['error_code'],5025)
        self.assertNotIn('private-token',json.dumps(page.diagnostics()))
        self.assertFalse(page.response_rows);self.assertFalse(page.ready_responses)

"""Opt-in browser smoke test with an empty, synthetic installation."""
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from repostbot.app import Handler,admin_session_secret
from repostbot.core import Store,Worker

@unittest.skipUnless(os.environ.get('REPOSTBOT_BROWSER_TESTS')=='1' and shutil.which('chromium'),'Opt-in Chromium check')
class PublicPanelTests(unittest.TestCase):
    def test_navigation_support_and_log_controls(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'test.sqlite3');worker=Worker(store)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
            server.store,server.worker=store,worker
            server.password='synthetic-test-password';server.secret=admin_session_secret(store,server.password)
            server.data_dir=Path(directory);server.windows_launcher=False;server.linux_launcher=True;server.container_launcher=False
            server.local_setup={'state':'idle','error':'','progress':'','model':'','lock':threading.Lock()}
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1280,'height':1000});errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    base='http://127.0.0.1:'+str(server.server_port)
                    self.assertEqual(page.request.post(base+'/api/login',data={'password':server.password},headers={'X-Repostbot-Request':'1'}).status,200)
                    for path in ('/social','/scout','/routes','/conversation','/updates'):
                        page.goto(base+path);page.wait_for_load_state('networkidle')
                        self.assertEqual(page.locator('footer.project-support a').get_attribute('href'),'https://pay.cloudtips.ru/p/f8e5cb75')
                        self.assertEqual(page.locator('footer.project-support a').get_attribute('rel'),'noopener noreferrer')
                        self.assertFalse(page.locator('#toast').inner_text().strip().lower().startswith('ошибка'))
                    page.goto(base+'/social');page.wait_for_load_state('networkidle')
                    page.get_by_role('tab',name='Связь аккаунтов',exact=True).click()
                    self.assertTrue(page.locator('#participantLinkForm').is_visible())
                    page.screenshot(path='/tmp/repostbot-public-social.png',full_page=True)
                    page.goto(base+'/scout');page.wait_for_load_state('networkidle')
                    page.get_by_role('tab',name='Twitch',exact=True).click()
                    page.get_by_role('tab',name='Сообщения и награды',exact=True).click()
                    self.assertTrue(page.locator('#twitchLogFilter').is_visible())
                    self.assertFalse(worker.twitch_scout.settings()['roll_enabled'])
                    page.screenshot(path='/tmp/repostbot-public-twitch.png',full_page=True)
                    self.assertFalse(errors,errors);browser.close()
            finally:
                server.shutdown();server.server_close();thread.join(3)
                worker.stop.set();worker.twitch_scout.close();worker.scout.close();worker.presence.close()
                worker.telegram_pool.shutdown(wait=True);worker.conversation.pool.shutdown(wait=True)
                worker.twitch.pool.shutdown(wait=True);worker.models.pool.shutdown(wait=True)
                store.db.close()

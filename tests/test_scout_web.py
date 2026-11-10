import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from repostbot.core import Store,Worker
from tests.test_web import WebTests
from tests.test_reply_context import ReplyContextTests
class ScoutWebTests(unittest.TestCase):
    call=WebTests.call
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();store=Store(Path(self.tmp.name)/'bot.db')
        self.server=SimpleNamespace(store=store,worker=Worker(store),password='long-password-here',secret=b'test-secret',data_dir=Path(self.tmp.name),windows_launcher=True)
        self.scout=self.server.worker.scout
        _,headers,_=self.call('POST','/api/login',{'password':self.server.password});self.cookie=headers['Set-Cookie'].split(';')[0]
    def tearDown(self):
        worker=self.server.worker;worker.stop.set();worker.twitch_scout.close();worker.scout.close();worker.presence.close()
        worker.telegram_pool.shutdown(wait=True);worker.conversation.pool.shutdown(wait=True);worker.twitch.pool.shutdown(wait=True);worker.models.pool.shutdown(wait=True);self.server.store.db.close();self.tmp.cleanup()

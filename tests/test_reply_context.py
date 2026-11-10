import tempfile,unittest
from pathlib import Path
from repostbot.core import Store,Worker
class ReplyContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'bot.db');self.worker=Worker(self.store)
        self.worker.bot_id=99;self.worker.bot_username='assistant_bot'
        self.store.set_settings({'ai_provider':'openai','openai_api_key':'mock-key','openai_model':'mock','reply_names':'помощник','community_ai_scan_enabled':'0','memory_days':'-1'})
    def tearDown(self):
        self.worker.stop.set();self.worker.twitch_scout.close();self.worker.scout.close();self.worker.presence.close()
        self.worker.telegram_pool.shutdown(wait=True);self.worker.conversation.pool.shutdown(wait=True)
        self.worker.twitch.pool.shutdown(wait=True);self.worker.models.pool.shutdown(wait=True);self.store.db.close();self.temp.cleanup()

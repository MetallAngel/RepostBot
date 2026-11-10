import time
import unittest
from unittest.mock import patch
from tests import test_reply_context as fixture

class RepostStatisticsTests(unittest.TestCase):
    setUp=fixture.ReplyContextTests.setUp
    tearDown=fixture.ReplyContextTests.tearDown
    def post(self,number,games,chat='-10077'):
        body='Сегодня в 18:00 стрим. https://www.twitch.tv/example'
        self.store.remember_route_post(1,str(number),chat,number,{'text':body,'published_at':int(time.time())})
        with self.store.lock,self.store.db:
            for game in games:
                self.store.db.execute('INSERT INTO stream_series_posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (chat,1,str(number),'source','example',game,game,None,'','none',0,str(number),int(time.time()),body,''))

    def test_counts_are_unique_by_publication_and_unknown_genres_are_not_non_gacha(self):
        self.post(1,['hsr']);self.post(2,['xcom','xcom2']);self.post(3,['hsr','xcom']);self.post(4,['unknown']);self.post(5,[])
        counts=self.worker.repost_stats.report('-10077')['counts']
        self.assertEqual(counts,{'reposts':5,'announcements':5,'gacha':1,'non_gacha':1,'mixed':1,'unknown':2})
        self.assertEqual(self.worker.repost_stats.report('-10077')['genres'][0]['posts'],2)

    def test_answer_uses_delivered_local_data_without_requesting_a_model(self):
        self.post(1,['xcom']);self.post(2,['hsr']);self.post(3,['xcom'],chat='-10088')
        self.store.set_settings({'repost_stats_main_chat':'-10077'})
        with patch('repostbot.ai.response_text') as model:
            answer=self.worker.answer('Сколько анонсов в основной группе не являются гачей? Сравни с другими жанрами',chat_id='17')
        model.assert_not_called();self.assertIn('анонсов 2',answer);self.assertIn('Подтверждённо без гачи: 1',answer)
        self.assertNotIn('Пришли посты',answer)

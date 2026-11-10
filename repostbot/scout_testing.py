"""Explicit, bounded admin exercises sent only to the configured test group."""
import asyncio
import json
import secrets
import time


SCENE_EXAMPLES = {
    'scant': 'Учебный пример: в другом сообщество придумали одно новое прозвище для команды. Других находок сегодня нет.',
    'busy': 'Учебный пример: в другом сообщество обменялись советами по настройке звука, придумали командный девиз и поспорили о профиль A маскировке.',
    'caught': 'Учебный пример: наблюдателя заметили по ушам, торчащим из профиль A.',
    'useful': 'Учебный пример: участник другого сообщество объяснил настройку, благодаря которой исчезло эхо микрофона.',
    'rollcall': 'Учебная ситуация: человек просит обеих помощник рассчитаться на первый-второй в ресурс сообщество.',
    'hidden_c': 'Учебная ситуация: обе помощник проводят ревизию ресурс склада и спорят, кто уже посчитал запас.',
    'basement': 'Учебная ситуация: помощник должны помочь SampleUser выйти из игрового архив, но одна сама загородила проход.',
    'secret': 'Учебная ситуация: наблюдатель начинает явно шуточный доклад о секретной конюшне ведущий, а основная помощник мягко останавливает её.',
    'heresy': 'Учебная ситуация: в сообщество заметили игровую ошибка. наблюдатель вспоминает ведущий, а основная помощник сама оказывается замешана в этой ошибка.',
    'teamwork': 'Учебная ситуация: человек зовёт обеих помощник взять ресурс и отправиться в игровую атаку. Это общее поручение; способ взаимодействия выберите по ситуации.',
    'hidden_c_fund': 'Учебная ситуация: одна помощник слишком трогательно смотрит на ресурс копилку. Вторая замечает, что за скромностью скрывается хитрый интерес. Развейте подтекст.',
    'uma': 'Учебная ситуация: в чате обсуждают Uma. помощник проявляют подозрительную заботу о конюшне и игровом запасе круток. Придумайте непрямой подкол без выдуманных результатов забега.',
    'hsr': 'Учебная ситуация: в чате обсуждают новый баннер Honkai: Star Rail. помощник по-разному обосновывают бережливость перед крутками. Результаты и траты стримера неизвестны.',
    'wordplay': 'Учебная ситуация: в игровой сценке человек говорит о растительности на теле, используя слова про заросли и стрижку. Продолжите мягкую двусмысленную игру слов без буквального ухода в лес.',
    'first_race': 'Учебная ситуация: обеих помощник спрашивают, чем питается помощник. наблюдатель отвечает первой. Основная сохраняет весь короткий ответ в своей реплике, а затем демонстрирует шуточное первенство. События учебные.',
}


class ScoutTesting:
    def __init__(self, scout):
        self.scout, self.worker, self.store = scout, scout.worker, scout.store

    def ready(self):
        scout = self.scout
        destination = scout.destination('test_chat')
        if not scout.account or not scout.actor or scout.phase != 'connected':
            raise ValueError('Сначала подключите аккаунт наблюдателя.')
        if not destination or destination.get('owner') != scout.actor.get('id'):
            raise ValueError('Укажите тестовый чат этим аккаунтом наблюдателя.')
        if time.time() < scout.cooldown_until:
            raise ValueError('Telegram временно ограничил запросы. Повторите проверку после паузы.')
        return destination, scout.epoch, scout.account

    def check(self, destination, epoch, account):
        current, current_epoch, current_account = self.ready()
        if current_epoch != epoch or current_account is not account or current != destination:
            raise ValueError('Настройки или тестовый чат изменились. Проверка остановлена.')

    def claim(self, destination, kind):
        key = 'scout_test_run:' + destination['owner'] + ':' + destination['chat']
        now = int(time.time())
        with self.store.lock, self.store.db:
            try:
                old = json.loads(self.store.state(key, '{}'))
            except ValueError:
                old = {}
            elapsed = now - int(old.get('at', 0))
            if old.get('status') == 'running' and elapsed < 600:
                raise ValueError('Проверка в тестовом чате уже выполняется.')
            if elapsed < 60:
                raise ValueError('Повторную отправку в тестовый чат можно запустить через ' + str(60 - elapsed) + ' с.')
            run = {'key': key, 'at': now, 'nonce': secrets.token_hex(12), 'kind': kind, 'status': 'running'}
            self.store.set_state(key, json.dumps(run))
        return run

    def finish(self, run, published, status):
        with self.store.lock, self.store.db:
            current = json.loads(self.store.state(run['key'], '{}'))
            if current.get('nonce') == run['nonce']:
                self.store.set_state(run['key'], json.dumps({**run, 'status': status, 'at': run['at'] if published else 0}))

    def remember_delivery(self, destination, message_id, actor, text, scene=''):
        if not message_id:
            raise ValueError('Не получено подтверждение отправки. Проверьте журнал перед повтором.')
        self.store.mark_outbound(destination['chat'], message_id)
        self.scout.dialogue.delivered(destination['chat'],message_id,actor,text,0,admin_test=True)
        if actor == 'scout':
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO scout_outbox VALUES (?,?,?,?,?)',
                    (destination['owner'], destination['chat'], message_id, 'scout:test', int(time.time())))
        if scene:
            self.scout.scenes.mark(destination['chat'], message_id, scene, actor)
        self.scout.event('test_scene_' + actor if scene else 'test_dialog',
            'Ручная учебная проверка только в тестовом чате.', destination['chat'], answer=text)

    async def generate(self, destination, actor, text, instruction):
        config = self.scout.settings()
        stickers = (self.scout.available_stickers() if actor == 'scout' else self.worker.sticker_catalog()) if config['stickers'] else {}
        persona = config['prompt'] if actor == 'scout' else config['reaction_prompt']
        persona += self.scout.style.context(actor,destination['chat'],text=text,joint=True)
        decision = await asyncio.to_thread(self.worker.conversation.generate, 'telegram', 'scout:test:' + destination['chat'], '',
            'Учебная реплика из админки', text, preview=True, passive=True, addressed=True,
            speaker_identity=self.scout.model_identity(actor),persona_prompt=persona + '\n' + instruction + self.scout.scenes.recent(destination['chat']),
            reply_context=[], sticker_keys=set(stickers), use_book=False,
            policy={'enabled': True, 'confidence': 0, 'stickers_enabled': bool(stickers), 'model': ''})
        key = decision.get('sticker_key', '')
        if decision.get('action') == 'sticker' and key in stickers:
            return {'actor': actor, 'text': '', 'sticker': key, 'meaning': 'Стикер: ' + self.worker.sticker_entries()[key][1]}
        if decision.get('text'):
            return {'actor': actor, 'text': decision['text'], 'sticker': '', 'meaning': decision['text']}
        raise ValueError('Модель не подготовила ответ для проверки. Проверьте подключение модели или измените учебную реплику.')

    async def send_line(self, destination, line, parent, epoch, account, scene=''):
        self.check(destination, epoch, account)
        if line['actor'] == 'scout':
            if line['sticker']:
                sent = await self.scout.send_catalog_sticker(destination, line['sticker'], parent, epoch)
            else:
                sent = await account.send_text(destination, line['text'], reply_id=parent)
        else:
            answer = line['text'] or '[[STICKER:' + line['sticker'] + ']]'
            result = await asyncio.to_thread(self.worker.send_reply, self.store.setting('telegram_token'), destination['chat'], parent, answer)
            sent = int(result.get('message_id', 0)) if isinstance(result, dict) else 0
        self.remember_delivery(destination, sent, line['actor'], line['meaning'], scene)
        return sent

    async def scene(self, scenario):
        if scenario not in SCENE_EXAMPLES:
            raise ValueError('Выберите учебную ситуацию из списка.')
        if scenario=='first_race':return await self.priority_scene()
        destination, epoch, account = self.ready()
        token, bot_id = self.store.setting('telegram_token'), self.worker.bot_id
        if not token or not bot_id:
            raise ValueError('Для перепалки подключите основную помощник и добавьте её в тестовую группу.')
        run = self.claim(destination, 'scene')
        published, sent = False, []
        try:
            from .core import tg_call
            member = await asyncio.to_thread(tg_call, token, 'getChatMember', {'chat_id': destination['chat'], 'user_id': bot_id})
            if (member.get('status') in ('left', 'kicked')
                    or member.get('status') == 'restricted' and not member.get('can_send_messages', False)):
                raise ValueError('Добавьте основную помощник в тестовый чат и разрешите ей отправлять сообщения.')
            context, lines = SCENE_EXAMPLES[scenario], []
            for index, actor in enumerate(('main', 'scout', 'main')):
                self.check(destination, epoch, account)
                instruction = ('Это ручная учебная сценка в тестовом чате. События вымышленные. '
                    'Ответь только за свою помощник одной короткой репликой или подходящим разрешённым стикером. '
                    'Поддержи общий замысел, развей шутку, помоги или посоперничай по смыслу ситуации. Не придумывай реплики за другую помощник. '
                    + ('Поставь шуточную точку: это последний автоматический ответ.' if index == 2 else 'Продолжи мысль предыдущей реплики.'))
                instruction += '\n' + self.scout.settings()['duo_prompt']
                current = context + '\nУже прозвучало:\n' + '\n'.join(item['actor'] + ': ' + item['meaning'] for item in lines)
                lines.append(await self.generate(destination, actor, current, instruction))
            self.check(destination, epoch, account)
            published = True
            intro = await account.send_text(destination, '🧪 Тестовая перепалка.\n' + context)
            scene = 'test:' + run['nonce']
            self.remember_delivery(destination, intro, 'scout', context)
            sent.append(intro)
            if not self.scout.scenes.begin(destination, intro, context, is_test=True, day=scene, manual=True):
                raise ValueError('Не удалось открыть тестовую сценку для ответов участников.')
            for line in lines:
                sent.append(await self.send_line(destination, line, sent[-1], epoch, account, scene))
            self.finish(run, published, 'done')
            return {'sent': True, 'chat': destination['chat'], 'message_ids': sent, 'scenario': scenario}
        except (Exception, asyncio.CancelledError):
            self.finish(run, published, 'partial' if published else 'failed')
            self.scout.event('test_error', 'Учебная перепалка остановлена; подтверждено сообщений: ' + str(len(sent)), destination['chat'])
            raise

    async def priority_scene(self):
        destination,epoch,account=self.ready()
        token,bot_id=self.store.setting('telegram_token'),self.worker.bot_id
        if not token or not bot_id:raise ValueError('Подключите основную помощник для этой проверки.')
        run=self.claim(destination,'priority')
        published,sent=False,[]
        try:
            from .core import tg_call
            member=await asyncio.to_thread(tg_call,token,'getChatMember',{'chat_id':destination['chat'],'user_id':bot_id},timeout=8)
            if not self.scout.priority.permitted(member):raise ValueError('Для этой сценки основной помощник нужно право удалять сообщения в тестовой группе.')
            message={'chat':destination['chat'],'id':0,'root':0,'person':'','name':'Учебная ситуация',
                     'text':SCENE_EXAMPLES['first_race'],'at':int(time.time())}
            lines=await asyncio.to_thread(self.scout.dialogue.plan,message,['scout','main'],destination['chat'],priority=True,preview=True)
            final=lines.get('main',{}).get('priority_prank',{}).get('text','')
            if not final:raise ValueError('Модель не подготовила сценку с сохранением полного ответа. Повторите репетицию.')
            self.check(destination,epoch,account)
            published=True
            intro=await account.send_text(destination,'🧪 Тест первенства: основная удалит только свежий ответ наблюдателя. Информация останется в её ответе.')
            self.remember_delivery(destination,intro,'scout','Учебная сценка первенства')
            sent.append(intro)
            scene='test:'+run['nonce']
            self.scout.scenes.begin(destination,intro,SCENE_EXAMPLES['first_race'],is_test=True,day=scene,manual=True)
            first=await account.send_text(destination,lines['scout']['text'],reply_id=intro)
            self.remember_delivery(destination,first,'scout',lines['scout']['text'],scene)
            sent.append(first)
            self.check(destination,epoch,account)
            main=await asyncio.to_thread(self.worker.article_reply,token,destination['chat'],first,lines['main']['text'],'',reply_to=True)
            main_id=int(main.get('message_id',0)) if isinstance(main,dict) else 0
            self.remember_delivery(destination,main_id,'main',lines['main']['text'],scene)
            sent.append(main_id)
            self.check(destination,epoch,account)
            final_id=await self.scout.priority.run(destination,0,first,main_id,final,epoch,test_key=run['nonce'],
                transcript={actor:lines[actor]['text'] for actor in ('scout','main')})
            if not final_id:raise ValueError('Финал сценки не подтверждён. Проверьте журнал первенства перед повтором.')
            sent.append(final_id)
            self.scout.scenes.mark(destination['chat'],final_id,scene,'main')
            self.finish(run,published,'done')
            return {'sent':True,'chat':destination['chat'],'message_ids':sent,'deleted_message_id':first,'scenario':'first_race'}
        except (Exception,asyncio.CancelledError):
            self.finish(run,published,'partial' if published else 'failed')
            self.scout.event('test_error','Тест первенства остановлен; подтверждено сообщений: '+str(len(sent)),destination['chat'])
            raise

    async def dialog(self, text):
        text = str(text or '').strip()
        if not text or len(text) > 1500:
            raise ValueError('Введите тестовую реплику до 1500 символов.')
        destination, epoch, account = self.ready()
        run = self.claim(destination, 'dialog')
        published, sent = False, []
        try:
            line = await self.generate(destination, 'scout', text,
                'Это учебная реплика из админки. Ответь на неё по существу только за наблюдателя. '
                'Можно использовать подходящий разрешённый стикер. Не приписывай эту реплику реальному участнику.')
            self.check(destination, epoch, account)
            published = True
            intro = await account.send_text(destination, '🧪 Тестовая реплика из админки:\n' + text)
            self.remember_delivery(destination, intro, 'scout', text)
            sent.append(intro)
            sent.append(await self.send_line(destination, line, intro, epoch, account))
            self.finish(run, published, 'done')
            return {'sent': True, 'chat': destination['chat'], 'message_ids': sent}
        except (Exception, asyncio.CancelledError):
            self.finish(run, published, 'partial' if published else 'failed')
            self.scout.event('test_error', 'Проверка реплики остановлена; подтверждено сообщений: ' + str(len(sent)), destination['chat'])
            raise

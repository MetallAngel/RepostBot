"""Account-wide scout disguises; durable intent, acknowledged stages, no daily cap."""
import asyncio
import json
import re
import secrets
import time
from .scout_avatar_assets import ASSETS
from .scout_transport import rpc_error
MODES = ('scout', 'box', 'profile_b', 'hidden_c')
NAMES = {'scout': 'Assistant: стиль 1', 'box': 'Assistant: стиль 2', 'profile_b': 'Assistant: стиль 3', 'hidden_c': 'Assistant: стиль 4'}
ACTION_GUIDE = 'Ты помощник сообщества. Отвечай по-русски, кратко, по текущему вопросу. Используй только подтверждённый контекст. Не выдумывай биографии, события, права или действия участников.'

def clear_action(text):
    """Fast path for unambiguous direct human requests; the model handles nuance."""
    text = re.sub('[«»"“”].*?[«»"“”]', ' ', str(text).casefold()).replace('ё', 'е')
    if re.search('\\b(?:если|когда-нибудь|представь|допустим|цитата|сказал[аи]?)\\b', text):
        return ''
    if re.search('\\b(?:не|никто\\s+не)\\s+(?:раскрыл|распознал|узнал|обнаружил|заметил)|\\bуши\\s+не\\s+торчат', text):
        return ''
    if re.search('\\bу\\s+(?:кота|кошки|ведущий|ведущий|меня|него|нее|них|соседа)\\b|\\b(?:его|ее|их)\\s+уши', text):
        return ''
    hide = re.search('\\b(?:спрячься|прячься|укройся|замаскируйся|залезай|лезь|ныряй)\\b', text)
    if hide:
        tail = text[hide.start():]
        if re.search('\\b(?:не|нельзя)\\s+(?:прячься|спрячься|маскируйся)', text):
            return ''
        if re.search('профиль B|листв', tail):
            return 'profile_b'
        if re.search('ресурс', tail):
            return 'hidden_c'
        if re.search('профиль A', tail):
            return 'box'
    if re.search('\\b(?:сними|снимай|хватит)\\s+маскиров|\\bвылезай\\s+из\\s+(?:профиль A|профиль B|ресурс)', text):
        return 'scout'
    if re.search('\\bтебя\\s+(?:уже\\s+)?(?:раскрыли|раскусили|распознали|обнаружили|заметили|узнали|нашли|спалили)|\\b(?:я|мы)\\s+(?:тебя\\s+(?:раскрыл[аи]?|раскусил[аи]?|распознал[аи]?|узнал[аи]?|наш[её]л|нашли|спалил[аи]?)|(?:раскрыл[аи]?|раскусил[аи]?|распознал[аи]?|узнал[аи]?|наш[её]л|нашли|спалил[аи]?)\\s+тебя)|\\b(?:твои\\s+)?уши\\s+(?:опять\\s+|все\\s+еще\\s+)?(?:торчат|видны|выдают)|\\b(?:торчат|видны)\\s+(?:твои\\s+)?уши|\\bиз\\s+(?:профиль A|профиль B|ресурс).{0,25}уши', text):
        return 'scout'
    return ''

class ScoutDisguise:

    def __init__(self, scout):
        self.scout, self.store = (scout, scout.store)
        self.lock = None
        with self.store.lock, self.store.db:
            self.store.db.executescript("\n                CREATE TABLE IF NOT EXISTS scout_disguise (\n                    owner TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT '', desired TEXT NOT NULL DEFAULT '',\n                    name TEXT NOT NULL DEFAULT '', photo TEXT NOT NULL DEFAULT '', photo_hash TEXT NOT NULL DEFAULT '',\n                    clean INTEGER NOT NULL DEFAULT 0, next_hide_at INTEGER NOT NULL DEFAULT 0,\n                    retry_at INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,\n                    error TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '{}', changed_at INTEGER NOT NULL DEFAULT 0,\n                    changed_source TEXT NOT NULL DEFAULT '{}', next_auto_at INTEGER NOT NULL DEFAULT 0);\n                CREATE TABLE IF NOT EXISTS scout_disguise_jobs (\n                    owner TEXT NOT NULL, chat TEXT NOT NULL, source_id INTEGER NOT NULL, action TEXT NOT NULL,\n                    at INTEGER NOT NULL, sticker INTEGER NOT NULL DEFAULT 0,\n                    PRIMARY KEY(owner,chat,source_id));\n            ")
            if 'next_auto_at' not in {row[1] for row in self.store.db.execute('PRAGMA table_info(scout_disguise)')}:
                self.store.db.execute('ALTER TABLE scout_disguise ADD COLUMN next_auto_at INTEGER NOT NULL DEFAULT 0')

    def state(self, owner=None):
        owner = owner or self.scout.actor.get('id', '')
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM scout_disguise WHERE owner=?', (owner,)).fetchone()
        return dict(row) if row else {'mode': '', 'desired': '', 'next_hide_at': 0, 'retry_at': 0, 'error': ''}

    def panel(self):
        state = self.state()
        return {key: state.get(key, '') for key in ('mode', 'desired', 'next_hide_at', 'next_auto_at', 'retry_at', 'error', 'changed_at')}

    def next_auto(self):
        minutes = self.scout.settings()['disguise_interval_minutes']
        return int(time.time()) + secrets.randbelow(minutes * 45 + 1) + minutes * 45

    def arm(self):
        if not self.scout.settings()['disguise_auto']:
            return
        owner = self.scout.actor['id']
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO scout_disguise(owner) VALUES (?)', (owner,))
            self.store.db.execute('UPDATE scout_disguise SET next_auto_at=? WHERE owner=? AND next_auto_at=0', (self.next_auto(), owner))

    def auto_available(self):
        return bool(self.scout.test_target() or self.scout.home_target() or (self.scout.settings()['enabled'] and self.scout.targets(active=True)) or self.scout.joined.active())

    def aliases(self):
        config = self.scout.settings()
        return tuple(dict.fromkeys([*NAMES.values(), *(config['disguise_name_' + mode] for mode in MODES)]))

    def cover_named(self, text):
        short = {'box': 'профиль A|профиль A', 'profile_b': 'профиль B|профиль B', 'hidden_c': 'ресурс'}.get(self.state()['mode'])
        return bool(short and re.search('(?:^|[,;!])\\s*(?:эй\\s+)?(?:' + short + ')\\s*(?:[,!?]|$)', str(text), re.I))

    def context(self):
        if not self.scout.settings()['disguise_enabled']:
            return '\nАвтоматическая смена профиля наблюдателя выключена.'
        state = self.state()
        name = self.scout.actor.get('name', '')
        return '\nФактический профиль наблюдателя во всех чатах: ' + json.dumps({'last_completed_mode': state['mode'] or 'профиль ещё не менялся', 'name': name, 'installed_photo': next((mode for mode in MODES if ASSETS[mode][0] == state.get('photo_hash')), 'ещё не менялось'), 'pending': state['desired'], 'deferred': bool(state['retry_at'] > time.time()), 'next_hide_at': state['next_hide_at']}, ensure_ascii=False) + '. Профиль один для всех чатов. Учитывай только подтверждённые изменения имени и изображения, не выдумывай завершение ожидающего действия.'

    def accepts(self, message, target, epoch):
        return bool(self.scout.settings()['disguise_enabled'] and self.scout.live(target, epoch) and (not message.get('forwarded')) and (not message.get('bot')) and (not message.get('text', '').lstrip().startswith('/')) and (message.get('person') not in (self.scout.actor.get('id'), str(self.scout.worker.bot_id))) and (-30 <= time.time() - message.get('at', 0) <= 300) and self.scout.addressed(message) and (not re.search('\\b(?:только|лишь)\\s+(?:основн|главн)|\\bбез\\s+(?:наблюдатель|шпионк)', message.get('text', ''), re.I)))

    async def before_reply(self, message, target, epoch):
        if not self.accepts(message, target, epoch):
            return
        if self.scout.dialogue.requests_sticker(message):
            return
        action = clear_action(message.get('user_text', message['text']))
        if action:
            await self.request(action, message, target, epoch)

    async def after_reply(self, message, target, epoch, decision):
        if not self.accepts(message, target, epoch):
            return
        self.arm()
        action = decision.get('disguise_action')
        text = message.get('user_text', message['text'])
        cited = bool(re.search('[«»"“”]|\\b(?:если|представь|допустим|цитата)\\b', text, re.I))
        if action in MODES and (not cited or clear_action(text)) and (not self.scout.dialogue.requests_sticker(message)):
            await self.request(action, message, target, epoch)
        await self.transition_sticker(message, target, epoch, decision)

    async def request(self, mode, message=None, target=None, epoch=None):
        if mode not in MODES:
            raise ValueError('Неизвестная маскировка.')
        owner = self.scout.actor.get('id')
        if not owner or not self.scout.account or self.scout.phase != 'connected':
            raise ValueError('Сначала подключите аккаунт наблюдателя.')
        if not self.scout.settings()['disguise_enabled']:
            raise ValueError('Сначала включите смену маскировки и сохраните настройки.')
        if message and (not self.accepts(message, target, epoch)):
            return self.panel()
        source = {'target': target} if message else {'manual': True}
        if message:
            source.update(chat=message['chat'], id=message['id'], at=message['at'])
        now = int(time.time())
        with self.store.lock, self.store.db:
            if message:
                added = self.store.db.execute('INSERT OR IGNORE INTO scout_disguise_jobs(owner,chat,source_id,action,at) VALUES (?,?,?,?,?)', (owner, message['chat'], message['id'], mode, now)).rowcount
                if not added:
                    return self.panel()
                current = self.state(owner)
                if current['mode'] == mode and (not current['desired']) and current.get('clean') and (current.get('name') == self.scout.settings()['disguise_name_' + mode]) and (current.get('photo_hash') == ASSETS[mode][0]):
                    return self.panel()
            self.store.db.execute('INSERT OR IGNORE INTO scout_disguise(owner) VALUES (?)', (owner,))
            self.store.db.execute("UPDATE scout_disguise SET desired=?, source=?, attempts=0, error='' WHERE owner=?", (mode, json.dumps(source, ensure_ascii=False), owner))
        self.scout.event('disguise_requested', 'Запрошен облик: ' + mode, message['chat'] if message else '')
        await self.tick()
        return self.panel()

    async def tick(self):
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            account, owner, epoch = (self.scout.account, self.scout.actor.get('id'), self.scout.epoch)

            def live():
                return bool(account and owner and (self.scout.account is account) and (self.scout.actor.get('id') == owner) and (self.scout.phase == 'connected') and (epoch == self.scout.epoch) and self.scout.settings()['disguise_enabled'])
            if not live():
                return
            state = self.state(owner)
            mode, now = (state['desired'], int(time.time()))
            if state['retry_at'] > now:
                return
            if not mode:
                config = self.scout.settings()
                if not (config['disguise_auto'] and state.get('next_auto_at', 0) and (state['next_auto_at'] <= now) and self.auto_available()):
                    return
                mode = secrets.choice([item for item in MODES if item not in ('scout', state['mode'])])
                self.save(owner, desired=mode, source=json.dumps({'automatic': True}), attempts=0)
                state = self.state(owner)
                self.scout.event('disguise_auto', 'Плановая тихая смена укрытия: ' + mode)
            source = json.loads(state.get('source') or '{}')
            if source.get('automatic') and (not self.scout.settings()['disguise_auto'] or not self.auto_available()):
                self.save(owner, desired='', next_auto_at=self.next_auto())
                return
            if mode != 'scout' and (not source.get('manual')) and (not source.get('automatic')) and (now - source.get('at', 0) > 1800 or not self.scout.live(source.get('target') or {}, epoch)):
                self.save(owner, desired='')
                self.scout.event('disguise_cancelled', 'Отложенная сценка устарела или чат выключен.')
                return
            if mode != 'scout' and state['next_hide_at'] > now and (state['mode'] != mode):
                return
            config = self.scout.settings()
            name, digest = (config['disguise_name_' + mode], ASSETS[mode][0])
            try:
                actor = await account.identity()
                if not live():
                    return
                if actor['id'] != owner:
                    raise ValueError('Подключён другой аккаунт; обновите состояние.')
                if actor.get('name') != name or state.get('name') != name:
                    await account.set_profile_name(name)
                    self.save(owner, name=name)
                    if not live():
                        return
                    self.scout.actor = {**self.scout.actor, 'name': name}
                photo = json.loads(state.get('photo') or '{}')
                new_photo = state.get('photo_hash') != digest or not photo
                if not new_photo:
                    new_photo = not await account.profile_avatar_is(photo)
                if not live():
                    return
                if new_photo:
                    photo = await account.upload_profile_avatar(mode, guard=live)
                    self.save(owner, photo=json.dumps(photo), photo_hash=digest, clean=0)
                    if not live():
                        return
                if not state.get('clean') or new_photo:
                    complete = await account.clear_profile_history(photo, guard=live)
                    if not live():
                        return
                    if not complete:
                        self.save(owner, retry_at=now + 5, clean=0)
                        return
                    self.save(owner, clean=1)
                if not live():
                    return
                changed = state['mode'] != mode or new_photo
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE scout_disguise SET mode=?, desired=CASE WHEN desired=? THEN '' ELSE desired END, retry_at=0,attempts=0,error='',changed_at=?,next_hide_at=?,changed_source=?,next_auto_at=? WHERE owner=?", (mode, mode, now if changed else state['changed_at'], now + config['disguise_pause_seconds'] if changed else state['next_hide_at'], json.dumps(source) if changed else state['changed_source'], self.next_auto() if changed else state.get('next_auto_at', 0), owner))
                if changed:
                    self.scout.event('disguise_applied', 'Имя и аватар обновлены, фамилия и старые фото очищены: ' + mode, source.get('chat', ''))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt = state.get('attempts', 0) + 1
                delay = max(1, int(exc.seconds)) if hasattr(exc, 'seconds') else min(3600, 15 * 2 ** min(attempt, 8))
                self.save(owner, attempts=attempt, retry_at=int(time.time()) + delay, error=rpc_error(exc))
                self.scout.event('disguise_deferred', 'Смена профиля не завершена; повтор отложен: ' + rpc_error(exc), source.get('chat', ''))

    def save(self, owner, **values):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE scout_disguise SET ' + ','.join((key + '=?' for key in values)) + ' WHERE owner=?', (*values.values(), owner))

    async def transition_sticker(self, message, target, epoch, decision):
        if decision.get('sticker_key') or not self.scout.settings()['stickers']:
            return
        state, owner = (self.state(), self.scout.actor['id'])
        if state['desired'] or time.time() - state.get('changed_at', 0) > 60:
            return
        source = json.loads(state.get('changed_source') or '{}')
        if source.get('chat') != message['chat'] or source.get('id') != message['id']:
            return
        mode = state['mode']
        if mode not in MODES:
            return
        from .sticker_context import categories
        context = categories(self.scout.worker._sticker_json_map('reply_sticker_key_aliases'))
        key = next((key for key in self.scout.available_stickers() if context.get(key) == ('scout' if mode == 'scout' else 'scout_' + mode)), '')
        if not key or not self.scout.live(target, epoch):
            return
        with self.store.lock, self.store.db:
            claimed = self.store.db.execute('UPDATE scout_disguise_jobs SET sticker=1 WHERE owner=? AND chat=? AND source_id=? AND action=? AND sticker=0', (owner, message['chat'], message['id'], mode)).rowcount
        if not claimed:
            return
        try:
            sent = await self.scout.send_catalog_sticker(target, key, message['id'], epoch)
            if not sent:
                return
            self.store.mark_outbound(message['chat'], sent)
            self.scout.dialogue.delivered(message['chat'], sent, 'scout', '[[STICKER:' + key + ']]', message['id'], root=message.get('root', 0), key=key)
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO scout_outbox VALUES (?,?,?,?,?)', (owner, message['chat'], sent, 'scout:disguise', int(time.time())))
            self.scout.event('disguise_sticker', 'Стикер завершает смену облика: ' + mode, message['chat'], message.get('root', 0))
        except Exception as exc:
            self.scout.event('disguise_sticker_failed', 'Стикер смены облика не подтверждён: ' + rpc_error(exc), message['chat'])

    async def loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.scout.event('disguise_error', 'Проверка отложенной маскировки не завершена: ' + rpc_error(exc))

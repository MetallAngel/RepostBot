"""Durable reversible identity for the main bot's temporary Telegram glitch."""
import hashlib
import json
import threading
import time
from .bot_profile_api import BotProfileAPI, ProfileError
NAMES = {'casual_style': 'Assistant: стиль 1', 'technical_style': 'Assistant: стиль 2', 'narrative_style': 'Assistant: стиль 3', 'dnd': 'Assistant: стиль 4', 'formal_style': 'Assistant: стиль 5', 'warm_style': 'Assistant: стиль 6', 'custom': 'Assistant: стиль 7', 'calm_style': 'Assistant: стиль 8'}

def original_name(name):
    return str(name)[:64] if str(name).startswith('Assistant') else ('Assistant — ' + str(name))[:64]

class GlitchProfile:

    def __init__(self, worker):
        self.worker, self.store = (worker, worker.store)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.api_factory = BotProfileAPI
        self.identity_token = ''
        self.bot = ''
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TABLE IF NOT EXISTS glitch_profiles (\n                bot TEXT PRIMARY KEY,original_name TEXT NOT NULL,original_photo BLOB NOT NULL,\n                dirty INTEGER NOT NULL DEFAULT 0,photo_mode TEXT NOT NULL DEFAULT 'normal',\n                name_value TEXT NOT NULL,photo_pending TEXT NOT NULL DEFAULT '',name_pending TEXT NOT NULL DEFAULT '',\n                retry_at INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,\n                error TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL DEFAULT 0)")

    def row(self, bot):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM glitch_profiles WHERE bot=?', (str(bot),)).fetchone()
        return dict(row) if row else None

    def update(self, bot, **values):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE glitch_profiles SET ' + ','.join((key + '=?' for key in values)) + ' WHERE bot=?', (*values.values(), str(bot)))

    def desired(self, bot=''):
        config = self.worker.conversation.settings()
        episode = self.worker.conversation.global_scope.current()
        if config['glitch_profile_enabled'] and episode and (episode.get('platform') == 'telegram') and (episode.get('phase') in ('active', 'sending')) and episode.get('bot_message_id') and (not bot or not episode.get('bot_id') or episode['bot_id'] == str(bot)):
            return episode['style'] if episode['style'] in NAMES else 'custom'
        return 'normal'

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return self.panel()
        token = self.store.setting('telegram_token')
        retry = {}
        retry_key = ''
        operation = ''
        try:
            if not token:
                return self.panel()
            fingerprint = hashlib.sha256(token.encode()).hexdigest()[:24]
            retry_key = 'glitch_profile_retry:' + fingerprint
            retry = json.loads(self.store.state(retry_key, '{}'))
            if retry.get('at', 0) > time.time():
                return self.panel()
            with self.store.lock:
                dirty = self.store.db.execute('SELECT 1 FROM glitch_profiles WHERE dirty=1 LIMIT 1').fetchone()
            if self.desired() == 'normal' and (not dirty):
                return self.panel()
            api = self.api_factory(token)
            if self.identity_token != fingerprint:
                self.bot = str(api.identity()['id'])
                self.identity_token = fingerprint
            bot = self.bot
            row = self.row(bot)
            if row and row['retry_at'] > time.time():
                return self.panel()
            for _ in range(7):
                if token != self.store.setting('telegram_token'):
                    return self.panel()
                mode = self.desired(bot)
                row = self.row(bot)
                if mode == 'normal' and (not row or not row['dirty']):
                    return self.panel()
                if mode != 'normal' and (not row or not row['dirty']):
                    backup = api.snapshot(bot)
                    if token != self.store.setting('telegram_token') or mode != self.desired(bot):
                        continue
                    with self.store.lock, self.store.db:
                        self.store.db.execute("INSERT INTO glitch_profiles(bot,original_name,original_photo,name_value,updated_at)\n                            VALUES (?,?,?,?,?) ON CONFLICT(bot) DO UPDATE SET original_name=excluded.original_name,\n                            original_photo=excluded.original_photo,name_value=excluded.name_value,photo_mode='normal',\n                            photo_pending='',name_pending='',error='',attempts=0,retry_at=0,updated_at=excluded.updated_at", (bot, backup['name'], backup['photo'], backup['name'], int(time.time())))
                    row = self.row(bot)
                name = original_name(row['original_name']) if mode == 'normal' else self.worker.conversation.settings()['glitch_name_' + mode]
                if row['photo_mode'] != mode or row['photo_pending']:
                    from .glitch_avatar_assets import avatar
                    raw = row['original_photo'] if mode == 'normal' else avatar(mode)
                    if mode != self.desired(bot):
                        continue
                    self.update(bot, dirty=1, photo_pending=mode, retry_at=int(time.time()) + 60)
                    operation = 'photo'
                    api.set_photo(raw)
                    self.update(bot, photo_mode=mode, photo_pending='', retry_at=0, updated_at=int(time.time()))
                    continue
                if row['name_value'] != name or row['name_pending']:
                    self.update(bot, dirty=1, name_pending=name, retry_at=int(time.time()) + 60)
                    operation = 'name'
                    api.set_name(name)
                    self.update(bot, name_value=name, name_pending='', retry_at=0, updated_at=int(time.time()))
                    if str(self.worker.bot_id) == bot:
                        self.worker.bot_name = name
                    continue
                if mode != self.desired(bot):
                    continue
                was_dirty = bool(row['dirty'])
                self.update(bot, dirty=int(mode != 'normal'), error='', attempts=0, retry_at=0)
                if mode == 'normal' and was_dirty:
                    self.store.event('info', 'Исходный аватар и имя основной помощник восстановлены.')
                self.store.set_state(retry_key, '{}')
                return self.panel()
            return self.panel()
        except Exception as exc:
            row = self.row(self.bot) if self.bot else None
            if operation == 'photo' and row and row['dirty'] and (token == self.store.setting('telegram_token')) and ('api' in locals()):
                mode = self.desired(self.bot)
                confirmed = 'normal' if mode == 'normal' else row['photo_mode']
                name = self.worker.conversation.settings()['glitch_name_' + confirmed] if confirmed in NAMES else original_name(row['original_name'])
                if row['name_value'] != name or row['name_pending']:
                    try:
                        self.update(self.bot, name_pending=name)
                        api.set_name(name)
                        self.update(self.bot, name_value=name, name_pending='')
                        if str(self.worker.bot_id) == self.bot:
                            self.worker.bot_name = name
                    except Exception:
                        pass
                row = self.row(self.bot)
            attempts = (row['attempts'] if row else int(retry.get('attempts', 0))) + 1
            delay = getattr(exc, 'retry_after', 0) or min(3600, 30 * 2 ** min(attempts, 7))
            error = str(exc) if isinstance(exc, ProfileError) else 'Изменение профиля отложено; исходный профиль сохранён.'
            if row:
                self.update(self.bot, retry_at=int(time.time()) + delay, attempts=attempts, error=error)
            if not retry_key:
                return self.panel()
            self.store.set_state(retry_key, json.dumps({'at': int(time.time()) + delay, 'attempts': attempts, 'error': error}))
            self.store.event('info', 'Профиль сбоя: ' + error)
            return self.panel()
        finally:
            self.lock.release()

    def panel(self):
        bot = self.bot or str(self.worker.bot_id or '')
        row = self.row(bot)
        token = self.store.setting('telegram_token')
        try:
            retry = json.loads(self.store.state('glitch_profile_retry:' + hashlib.sha256(token.encode()).hexdigest()[:24], '{}')) if token else {}
        except (ValueError, TypeError):
            retry = {}
        return {'bot': bot, 'mode': row['photo_mode'] if row else 'normal', 'name': row['name_value'] if row else '', 'desired': self.desired(bot), 'enabled': self.worker.conversation.settings()['glitch_profile_enabled'], 'original_saved': bool(row), 'original_has_photo': bool(row and row['original_photo']), 'dirty': bool(row and row['dirty']), 'pending': bool(row and (row['photo_pending'] or row['name_pending'])), 'retry_at': row['retry_at'] if row else retry.get('at', 0), 'error': row['error'] if row else retry.get('error', '')}

    def loop(self):
        while not self.worker.stop.is_set():
            try:
                self.tick()
            except Exception:
                self.store.event('info', 'Проверка профиля сбоя отложена.')
            self.wake.wait(5)
            self.wake.clear()

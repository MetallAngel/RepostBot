"""Twitch scout: browser transport, real IRC identities, shared community ledger."""
import json
import re
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from .twitch import TwitchIntegration, parse_irc_line
PROMPT = 'Ты помощник сообщества. Отвечай по-русски, кратко, по текущему вопросу. Используй только подтверждённый контекст. Не выдумывай биографии, события, права или действия участников.'

class TwitchScout(TwitchIntegration):
    is_scout = True

    def __init__(self, worker):
        super().__init__(worker, worker.community)
        self.lock = threading.RLock()
        self.ingest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='twitch_scout_read')
        self.book_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='twitch_scout_book')
        self.book_queued = set()
        self.actor = {}
        self.nick = ''
        self.authenticated = False
        self.reading = False
        self.last_read = 0
        self.pending = {}
        self.phase = 'stopped'
        self.error = ''
        self.closed = False
        self.generation = 0
        self.last_analysis = 0
        self.last_send = 0
        self.last_live = 0
        self.send_local = threading.local()
        self.auto_seed_started = False
        self.last_resume = 0
        from .scout_twitch_browser import ScoutBrowser
        self.browser = ScoutBrowser(self)
        from .scout_twitch_irc import ScoutIRC
        self.irc = ScoutIRC(self)
        from .twitch_emotes import TwitchEmotes
        self.emotes = TwitchEmotes(self)
        with self.store.lock, self.store.db:
            self.store.db.executescript("\n                CREATE TABLE IF NOT EXISTS scout_twitch_messages(id TEXT PRIMARY KEY,channel TEXT NOT NULL,person TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT NULL DEFAULT '',processed INTEGER NOT NULL DEFAULT 0);\n                CREATE TABLE IF NOT EXISTS scout_twitch_outbox(id TEXT PRIMARY KEY,channel TEXT NOT NULL,owner TEXT NOT NULL,text TEXT NOT NULL,reply_to TEXT NOT NULL,status TEXT NOT NULL,at INTEGER NOT NULL,message_id TEXT NOT NULL DEFAULT '');\n            ")
            columns = {row['name'] for row in self.store.db.execute('PRAGMA table_info(scout_twitch_messages)')}
            if 'payload' not in columns:
                self.store.db.execute("ALTER TABLE scout_twitch_messages ADD COLUMN payload TEXT NOT NULL DEFAULT ''")
            if 'processed' not in columns:
                self.store.db.execute('ALTER TABLE scout_twitch_messages ADD COLUMN processed INTEGER NOT NULL DEFAULT 0')
            self.store.db.execute("UPDATE scout_twitch_outbox SET status='uncertain' WHERE status IN ('queued','sending')")
        from .twitch_ledger import TwitchLedger
        self.ledger = TwitchLedger(self)

    def ignored_login(self, login):
        from .twitch_bots import ignored
        return ignored(login, self.settings().get('ignored_bots', ''), (self.login, self.store.setting('twitch_chat_login'), self.store.setting('twitch_bot_login')))

    def model_identity(self):
        from .speaker_identity import build
        return build('twitch', 'scout', {'id': self.user_id, 'username': self.login})

    def settings(self):
        defaults = {'enabled': False, 'channel': 'example_channel', 'account_login': '', 'replies': False, 'initiative': False, 'prompt': PROMPT, 'chance': 10, 'cooldown': 300, 'confidence': 85, 'external_emotes': True, 'transport': 'browser', 'ignored_bots': '', 'reward_capture': True, 'roll_enabled': False}
        try:
            defaults.update(json.loads(self.store.setting('scout_twitch_settings', '{}')))
        except (ValueError, TypeError):
            pass
        return defaults

    @property
    def channel(self):
        return self.settings()['channel']

    @property
    def login(self):
        return self.settings()['account_login']

    @property
    def user_id(self):
        return str(self.actor.get('id') or '')

    def claims_channel(self, channel):
        c = self.settings()
        return bool(c['enabled'] and c['channel'] == channel)

    def configure(self, data):
        c = self.settings()
        for key in ('enabled', 'replies', 'initiative', 'external_emotes', 'reward_capture', 'roll_enabled'):
            if key in data:
                if not isinstance(data[key], bool):
                    raise ValueError('Переключатели Twitch должны быть логическими значениями')
                c[key] = data[key]
        if 'ignored_bots' in data:
            value = str(data['ignored_bots'])
            if len(value) > 4000:
                raise ValueError('Список ботов: не более 4000 символов.')
            c['ignored_bots'] = value
        for key in ('channel', 'account_login'):
            if key in data:
                value = str(data[key]).strip().lstrip('#@').casefold()
                if value and (not re.fullmatch('[a-z0-9_]{3,25}', value)):
                    raise ValueError('Укажите логин Twitch без ссылки')
                c[key] = value
        if c['enabled'] and (not c['channel'] or not c['account_login']):
            raise ValueError('Укажите канал и логин отдельного аккаунта')
        if 'prompt' in data:
            value = str(data['prompt']).strip()
            if not value or len(value) > 6000:
                raise ValueError('Промпт Twitch: от 1 до 6000 символов')
            c['prompt'] = value
        for key, low, high in (('chance', 0, 100), ('cooldown', 30, 86400), ('confidence', 0, 100)):
            if key in data:
                c[key] = max(low, min(high, int(data[key])))
        if 'transport' in data:
            if data['transport'] not in ('browser', 'irc'):
                raise ValueError('Неизвестный способ подключения Twitch')
            c['transport'] = data['transport']
        old = self.settings()
        with self.lock:
            self.generation += 1
            self.store.set_settings({'scout_twitch_settings': json.dumps(c, ensure_ascii=False)})
            if old['channel'] != c['channel'] or old['account_login'] != c['account_login'] or old.get('transport', 'browser') != c['transport']:
                self.authenticated = False
                self.reading = False
                self.actor = {}
        changed_identity = old['channel'] != c['channel'] or old['account_login'] != c['account_login'] or old.get('transport', 'browser') != c['transport']
        if c['enabled'] and (not old['enabled'] or changed_identity):
            self.resume_book()
            if c['transport'] == 'irc':
                if changed_identity:
                    self.irc.close()
                self.irc.start()
            else:
                self.irc.close()
                self.browser.start()
                if changed_identity and self.browser.running:
                    self.browser.request('chat', wait=False)
        elif not c['enabled']:
            self.irc.close()
            if self.browser.running:
                self.browser.request('pause', wait=False)
        return self.panel()

    def panel(self):
        c = self.settings()
        page = self.browser.page
        with self.store.lock:
            stats = self.store.db.execute('SELECT COUNT(*),COUNT(DISTINCT person) FROM scout_twitch_messages WHERE channel=?', (c['channel'],)).fetchone()
            outgoing = [dict(row) for row in self.store.db.execute('SELECT id,status,at,message_id FROM scout_twitch_outbox WHERE channel=? ORDER BY at DESC,rowid DESC LIMIT 12', (c['channel'],))]
        return {'settings': c, 'phase': self.phase, 'error': self.error, 'account': dict(self.actor), 'authenticated': self.authenticated, 'reading': self.reading, 'last_read': self.last_read, 'browser_running': self.browser.running, 'dependencies': self.browser.dependencies(), 'browser_version': self.browser.browser_version if c['transport'] == 'browser' else '', 'login_warning': self.browser.login_warning if c['transport'] == 'browser' else '', 'login_diagnostics': page.diagnostics() if hasattr(page, 'diagnostics') else [], 'browser_mode': 'irc-websocket' if c['transport'] == 'irc' else 'native-cdp', 'direct_chat': self.irc.snapshot(), **self.ledger.summary(), 'outbox': outgoing, 'default_prompt': PROMPT}

    def start(self):
        self.ledger.start()
        if self.settings()['enabled']:
            self.resume_book()
            (self.irc if self.settings()['transport'] == 'irc' else self.browser).start()

    def open_browser(self, destination):
        if destination not in ('login', 'chat', 'restart', 'direct'):
            raise ValueError('Неизвестное окно Twitch')
        if destination == 'chat' and (not self.channel):
            raise ValueError('Сначала сохраните канал Twitch')
        if destination == 'direct':
            if not self.login or not self.channel:
                raise ValueError('Сначала сохраните канал и логин Twitch-наблюдателя')
            c = self.settings()
            c['transport'] = 'irc'
            with self.lock:
                self.generation += 1
                self.authenticated = False
                self.reading = False
                self.store.set_settings({'scout_twitch_settings': json.dumps(c, ensure_ascii=False)})
            self.irc.close()
            self.irc.start()
            return self.panel()
        if self.settings()['transport'] == 'irc':
            self.irc.close()
            c = self.settings()
            c['transport'] = 'browser'
            with self.lock:
                self.generation += 1
                self.store.set_settings({'scout_twitch_settings': json.dumps(c, ensure_ascii=False)})
        if destination == 'restart':
            resume = 'chat' if self.authenticated and self.reading else 'login'
            with self.lock:
                self.generation += 1
            if self.browser.thread and self.browser.thread.is_alive():
                self.browser.request('pause')
            destination = resume
        self.browser.start()
        self.browser.request(destination, wait=False)
        return self.panel()

    def import_session(self, token):
        value = str(token or '').strip()
        if not re.fullmatch('[A-Za-z0-9._~-]{20,512}', value):
            raise ValueError('Вставьте только значение cookie auth-token своего аккаунта Twitch.')
        if not self.login or not self.channel:
            raise ValueError('Сначала сохраните канал и логин Twitch-наблюдателя.')
        with self.lock:
            self.generation += 1
            self.authenticated = False
            self.reading = False
        self.irc.close()
        self.browser.start()
        result = self.browser.request('session', {'token': value})
        if self.settings()['transport'] == 'irc':
            self.browser.request('pause')
            self.irc.start()
        else:
            self.browser.request('chat', wait=False)
        return {**self.panel(), 'transferred': bool(result.get('transferred'))}

    def disconnect(self):
        c = self.settings()
        c.update(enabled=False, replies=False, initiative=False)
        with self.lock:
            self.generation += 1
            self.store.set_settings({'scout_twitch_settings': json.dumps(c, ensure_ascii=False)})
        self.irc.close()
        self.browser.forget()
        self.actor = {}
        self.authenticated = False
        self.reading = False
        return self.panel()

    def set_identity(self, user_id):
        if self.nick != self.login or not str(user_id).isdigit():
            self.authenticated = False
            return
        self.actor = {'id': str(user_id), 'login': self.nick}
        self.authenticated = True
        self.error = ''
        self.seed_example_channel()

    def seed_example_channel(self):
        return

    def wire(self, payload, outgoing=False):
        if not isinstance(payload, str) or len(payload) > 100000:
            return
        for line in payload.split('\r\n'):
            if outgoing:
                if line.startswith('NICK '):
                    self.nick = line[5:].strip().casefold()
                    self.authenticated = False
                    if self.nick != self.login:
                        self.actor = {}
                match = re.search('(?:^| )PRIVMSG #([^ ]+) :(.*)$', line)
                if match and match[1].casefold() == self.channel:
                    with self.lock:
                        for job in self.pending.values():
                            if job.get('started') and (not job['future'].done()) and (job['text'] == match[2]) and (job['generation'] == self.generation):
                                job['wired'] = True
                                break
                continue
            tags = {}
            if line.startswith('@'):
                for part in line.partition(' ')[0][1:].split(';'):
                    key, _, value = part.partition('=')
                    tags[key] = value
            body = line.partition(' ')[2] if line.startswith('@') else line
            if re.match(':[^ ]+ GLOBALUSERSTATE(?: |$)', body):
                self.set_identity(tags.get('user-id', ''))
            match = re.match(':[^ ]+ (ROOMSTATE|USERSTATE|NOTICE) #([^ ]+)', body)
            if match and match[2].casefold() == self.channel:
                if match[1] in ('ROOMSTATE', 'USERSTATE'):
                    self.reading = True
                    self.phase = 'connected'
                if match[1] == 'ROOMSTATE':
                    room_id = tags.get('room-id', '')
                    if room_id.isdigit():
                        self.store.set_state('twitch_scout_room:' + self.channel, room_id)
                    self.emotes.schedule(room_id)
                    self.seed_example_channel()
                if match[1] == 'USERSTATE':
                    self.ack(tags.get('id', ''))
                if match[1] == 'NOTICE' and tags.get('msg-id', '').startswith('msg_'):
                    self.reject('Twitch отклонил отправку: ' + tags['msg-id'][:70])
            event = parse_irc_line(line)
            if not event or event.get('channel_login') != self.channel or (not self.settings()['enabled']):
                continue
            self.reading = True
            self.last_read = int(time.time())
            self.phase = 'connected'
            if event['chatter_user_login'].casefold() == self.login:
                self.ack(event['message_id'], event['message']['text'])
                continue
            if self.ignored_login(event.get('chatter_user_login')):
                continue
            if not event['message_id'] or not event['chatter_user_id'].isdigit():
                continue
            self.ingest_pool.submit(self.ingest, event, self.generation)

    def ingest(self, event, generation):
        c = self.settings()
        if self.closed or not c['enabled'] or event.get('channel_login') != c['channel'] or self.ignored_login(event.get('chatter_user_login')):
            return
        if time.time() - self.last_resume > 60:
            self.last_resume = time.time()
            self.resume_book()
        event = {**self.emotes.enrich(event), '_scout_generation': generation}
        self.ledger.message(event)
        with self.store.lock, self.store.db:
            new = self.store.db.execute('INSERT OR IGNORE INTO scout_twitch_messages(id,channel,person,at,payload) VALUES (?,?,?,?,?)', (event['message_id'], c['channel'], event['chatter_user_id'], int(time.time()), json.dumps(event, ensure_ascii=False))).rowcount
        if not new:
            return
        if not self.community.record_chat(event):
            event['_scout_generation'] = -1
        self.queue_book(event)

    def queue_book(self, event):
        with self.lock:
            if event['message_id'] in self.book_queued or self.closed:
                return
            self.book_queued.add(event['message_id'])

        def run():
            try:
                self.send_local.generation = event.get('_scout_generation', -1)
                if event.get('channel_login') == self.channel:
                    self.community.record_chat(event)
                    self.on_chat(event, recorded=True)
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE scout_twitch_messages SET processed=1,payload='' WHERE id=?", (event['message_id'],))
            except Exception as exc:
                self.store.event('info', 'Twitch: учёт сообщения отложен (' + type(exc).__name__ + ')')
            finally:
                self.send_local.generation = None
                with self.lock:
                    self.book_queued.discard(event['message_id'])
        self.book_pool.submit(run)

    def resume_book(self):
        with self.store.lock:
            rows = list(self.store.db.execute("SELECT payload FROM scout_twitch_messages WHERE processed=0 AND payload!='' AND channel=? LIMIT 200", (self.channel,)))
        for row in rows:
            try:
                event = json.loads(row['payload'])
                event['_scout_generation'] = -1
            except (ValueError, TypeError):
                continue
            self.queue_book(event)

    def answer_question(self, event, question):
        c = self.settings()
        parent = event.get('reply_parent_message_body', '')
        refs = [{'kind': 'reply', 'text': parent, 'author': event.get('reply_parent_user_login', ''), 'bot': event.get('reply_parent_user_id') == self.user_id}] if parent else []
        frames = self.emotes.visuals(event)
        data = self.worker.conversation.generate('twitch', 'twitch:' + c['channel'], event['chatter_user_id'], event['chatter_user_name'], question, message_key=event['message_id'], reply_context=refs, speaker_identity=self.model_identity(), persona_prompt=c['prompt'] + self.emotes.context(event), glitch_immune=True, image_data=frames, emote_images=True, addressed=True, policy={'enabled': True, 'confidence': 0, 'stickers_enabled': False}, sticker_keys=set())
        return data['text'] if data['action'] != 'silent' else None

    def style_reply(self, text, *args, **kwargs):
        return text

    def initiative_enabled(self, chat):
        return self.settings()['initiative']

    def _reply(self, event, question):
        return

    def on_chat(self, event, recorded=False):
        if self.ignored_login(event.get('chatter_user_login')):
            return
        if not recorded and (not self.community.record_chat(event)):
            return
        person = str(event['chatter_user_id'])
        name = event.get('chatter_user_name') or event.get('chatter_user_login', '')
        self.community.observe('twitch', person, name, event['message_id'], event['message']['text'], notify_allowed=False, severe_notify_allowed=False, transition_notify_allowed=False)
        if re.match('^[!/]roll(?:\\s|$)', event['message']['text'], re.I) and self.settings()['roll_enabled']:
            self.roll_command(event)

    def roll_command(self, event):
        generation = event.get('_scout_generation', self.generation)
        if generation != self.generation or not self.settings()['roll_enabled'] or (not self.authenticated):
            return
        key = 'twitch_roll:' + self.channel + ':' + str(event['chatter_user_id'])
        if int(self.store.state(key, '0')) > time.time() - 60:
            return
        self.store.set_state(key, int(time.time()))
        self.send_local.generation = generation
        try:
            from .dice import parse_roll, roll, format_result
            spec, _ = parse_roll(re.sub('^!roll', '/roll', event['message']['text'], flags=re.I))
            self.send('@' + event['chatter_user_login'] + ' ' + format_result(roll(spec)), event['message_id'])
        finally:
            self.send_local.generation = None

    def _live_reply(self, event):
        return

    def can_send(self, generation):
        c = self.settings()
        return bool(not self.closed and c['enabled'] and (c['replies'] or c['roll_enabled']) and self.authenticated and self.reading and (generation == self.generation))

    def send(self, message, reply_id=''):
        generation = getattr(getattr(self, 'send_local', None), 'generation', None)
        if generation is None:
            generation = self.generation
        if not self.can_send(generation):
            return None
        text = self.community.redact_terms(str(message)).replace('\r', ' ').replace('\n', ' ')[:500].strip()
        if not text:
            return None
        if text.startswith('/'):
            raise ValueError('наблюдатель отправляет обычный текст, без команд модерации')
        key = secrets.token_hex(16)
        future = Future()
        job = {'id': key, 'text': text, 'reply_to': str(reply_id), 'generation': generation, 'wired': False, 'transport': self.settings()['transport'], 'future': future, 'owner': self.user_id, 'channel': self.channel}
        with self.lock:
            if len(self.pending) >= 4:
                raise RuntimeError('Очередь ответов Twitch занята')
            self.pending[key] = job
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO scout_twitch_outbox VALUES (?,?,?,?,?,?,?,?)', (key, self.channel, self.user_id, text, str(reply_id), 'queued', int(time.time()), ''))
        try:
            if self.settings()['transport'] == 'irc':
                self.irc.request_send(key)
            else:
                self.browser.request('send', {'id': key}, wait=False)
            result = future.result(timeout=25)
            self.worker.conversation.note_output('twitch:' + job['channel'], result, text)
            with self.store.lock:
                author = self.store.db.execute('SELECT user_id FROM twitch_messages WHERE id=?', (str(reply_id),)).fetchone()
            if author:
                self.store.remember_ai_context('twitch:' + job['channel'], 'twitch:' + author[0], self.store.db.execute('SELECT text FROM twitch_messages WHERE id=?', (str(reply_id),)).fetchone()[0], text)
            return result
        except TimeoutError:
            self.finish_job(key, 'uncertain')
            raise RuntimeError('Нет подтверждения Twitch; автоматического повтора не будет') from None
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def prepare_send(self, key):
        with self.lock:
            job = self.pending.get(key)
        if not job or job['future'].done() or (not self.can_send(job['generation'])) or (job['owner'] != self.user_id) or (job['channel'] != self.channel):
            if job:
                self.finish_job(key, 'cancelled', error='Настройки или аккаунт изменились')
            return None
        if time.time() - self.last_send < 2:
            return False
        self.last_send = time.time()
        job['started'] = True
        self.finish_job(key, 'sending')
        return job['text']

    def finish_job(self, key, status, message_id='', error=''):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE scout_twitch_outbox SET status=?,message_id=? WHERE id=?', (status, str(message_id), key))
        with self.lock:
            job = self.pending.get(key)
        if job and (not job['future'].done()):
            if error:
                job['future'].set_exception(RuntimeError(error))
            elif status == 'sent':
                job['future'].set_result(str(message_id) or self.settings()['transport'] + ':' + key)

    def ack(self, message_id='', text=None):
        with self.lock:
            job = next((j for j in self.pending.values() if j['wired'] and (not j['future'].done()) and (j['owner'] == self.user_id) and (text is None or j['text'] == text)), None)
        if job and self.can_send(job['generation']):
            self.finish_job(job['id'], 'sent', message_id)

    def reject(self, error):
        with self.lock:
            job = next((j for j in self.pending.values() if j['wired'] and (not j['future'].done())), None)
        if job:
            self.finish_job(job['id'], 'failed', error=error)

    def close(self):
        self.closed = True
        self.generation += 1
        self.ledger.close()
        self.irc.close()
        self.browser.close()
        with self.lock:
            keys = list(self.pending)
        for key in keys:
            self.finish_job(key, 'uncertain', error='Соединение закрыто; автоматического повтора нет')
        self.ingest_pool.shutdown(wait=True, cancel_futures=True)
        self.book_pool.shutdown(wait=True, cancel_futures=True)
        self.pool.shutdown(wait=True, cancel_futures=True)

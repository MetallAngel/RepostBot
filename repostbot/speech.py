"""Opt-in, command-only Telegram speech. No torch import in the bot process."""
import functools
import hashlib
import html
import json
import os
from pathlib import Path
import queue
import re
import signal
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from .speech_voices import PRESETS, SPEAKERS, VOICE_KEYS, voice_settings, voice_test_caption
from .reply_behavior import GrudgeReply, StandaloneReply, TransitionReply, UnavailableReply


DEFAULTS = {'enabled': False, 'chats': '', 'speaker': 'xenia', 'preset': 'xenia',
            'rate': 'medium', 'pitch': 'medium', 'max_chars': 300,
            'automatic_enabled': False, 'automatic_chats': '', 'automatic_chance': 15,
            'automatic_pause_seconds': 600, 'automatic_user_pause_seconds': 1800,
            'automatic_daily_limit': 12, 'automatic_max_chars': 240,
            'max_seconds': 30, 'timeout_seconds': 120, 'queue_limit': 2,
            'user_pause_seconds': 60, 'chat_pause_seconds': 30,
            'cpu_percent': 25, 'memory_mb': 512,
            'error_message': 'Не получилось озвучить эту строку летописи. Продолжим беседу текстом.',
            'pronunciation': 'Assistant|помощник\nExampleChannel|ведущий\nExampleChannel|ведущий\nSampleUser|Металл Ангел'}
MODEL_URL = 'https://models.silero.ai/models/tts/ru/v5_3_ru.pt'


def available_memory_mb():
    """Respect both the NAS and Docker's memory limit when Linux exposes them."""
    if not sys.platform.startswith('linux'):
        return None
    values = []
    try:
        match = re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)
        if match:
            values.append(int(match.group(1)) / 1024)
    except OSError:
        pass
    for limit, used in (('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
                        ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes')):
        try:
            maximum = int(Path(limit).read_text().strip())
            if 0 < maximum < 2**60:
                values.append(max(0, maximum - int(Path(used).read_text().strip())) / (1024 * 1024))
        except (OSError, ValueError):
            pass
    return min(values) if values else None


def speech_scope(fn):
    """A request cannot leak into the next task on a reused Telegram pool thread."""
    @functools.wraps(fn)
    def wrapped(worker, *args, **kwargs):
        previous = getattr(worker.speech.local, 'request', None)
        previous_auto = getattr(worker.speech.local, 'auto_request', None)
        previous_approved = getattr(worker.speech.local, 'auto_approved', False)
        worker.speech.local.request = None
        worker.speech.local.auto_request = None
        worker.speech.local.auto_approved = False
        try:
            return fn(worker, *args, **kwargs)
        finally:
            worker.speech.local.request = previous
            worker.speech.local.auto_request = previous_auto
            worker.speech.local.auto_approved = previous_approved
    return wrapped


class SpeechError(RuntimeError):
    pass


def plain_speech(text, config):
    text = re.sub(r'\[\[\s*STICKER\b[^\]]*\]\]', '', str(text), flags=re.I)
    text = re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+|tg://\S+', '', text)
    text = html.unescape(re.sub(r'<[^>]*>', '', text))
    for line in config['pronunciation'].splitlines():
        if '|' not in line:
            continue
        source, target = (x.strip() for x in line.split('|', 1))
        if source:
            text = re.sub(r'(?<!\w)' + re.escape(source) + r'(?!\w)', lambda _m: target, text, flags=re.I)
    text = re.sub(r'[^\w\s.,!?;:—–\-+ёЁ]', '', text)
    text = ' '.join(text.split())
    maximum = config['max_chars']
    if len(text) > maximum:
        # Prefer a complete sentence, otherwise cut on a word boundary.
        part = text[:maximum]
        stops = list(re.finditer(r'[.!?](?:\s|$)', part))
        text = part[:stops[-1].end()].strip() if stops and stops[-1].end() >= maximum // 2 else part.rsplit(' ', 1)[0].rstrip(' ,;:') + '.'
    return text


class SileroProcess:
    """One model, one CPU thread; Linux duty cycle also caps a single-core NAS."""
    def __init__(self, speech, required_revision=None):
        self.speech = speech
        self.required_revision = required_revision
        self.process = None
        self.responses = queue.Queue()
        self.closed = threading.Event()
        self.guard_reason = ''
        self.rss_mb = 0
        self.ready = False

    def start(self):
        if self.closed.is_set() or not self.authorized():
            raise SpeechError('Загрузка голоса отменена.')
        config = self.speech.settings()
        available = available_memory_mb()
        if available is not None and available < config['memory_mb'] + 128:
            raise SpeechError('Для голоса недостаточно свободной памяти NAS / Docker. Текстовые ответы продолжают работать.')
        self.process = subprocess.Popen(self.speech.engine_command(), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', bufsize=1, env=self.speech.engine_env())
        if self.closed.is_set() or not self.authorized():
            self.process.kill()
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            raise SpeechError('Загрузка голоса отменена.')
        threading.Thread(target=self._read, daemon=True, name='silero-protocol').start()
        threading.Thread(target=self._guard, args=(config,), daemon=True, name='silero-cpu-budget').start()
        result = self.receive(config['timeout_seconds'])
        if not result.get('ready'):
            raise SpeechError('Не удалось загрузить Silero. Повторите подготовку в панели.')
        self.ready = True

    def authorized(self):
        return (not self.speech.closed.is_set() and
                not (self.speech.preparing and self.speech.prepare_stop.is_set()) and
                (self.required_revision is None or self.required_revision == self.speech.revision and self.speech.settings()['enabled']))

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 8192:
                    break
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    break
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put({'error': 'process_exit'})

    def _guard(self, config):
        cycle = .2
        duty = config['cpu_percent'] / 100
        can_throttle = sys.platform.startswith('linux') and hasattr(signal, 'SIGSTOP')
        try:
            while not self.closed.is_set() and self.process.poll() is None:
                if not self.authorized():
                    self.guard_reason = 'Загрузка или озвучка отменена после изменения настроек.'
                    self.process.kill()
                    break
                if sys.platform.startswith('linux'):
                    try:
                        status = Path('/proc/' + str(self.process.pid) + '/status').read_text()
                        match = re.search(r'^VmRSS:\s+(\d+)', status, re.M)
                        self.rss_mb = int(match.group(1)) / 1024 if match else 0
                        if self.rss_mb > config['memory_mb']:
                            self.guard_reason = 'Процесс озвучки превысил предел памяти и остановлен.'
                            self.process.kill()
                            break
                    except (OSError, ValueError):
                        pass
                    available = available_memory_mb()
                    if available is not None and available < 96:
                        self.guard_reason = 'Память NAS / Docker почти заполнена. Голос выгружен, чтобы сохранить работу бота.'
                        self.process.kill()
                        break
                if can_throttle:
                    os.kill(self.process.pid, signal.SIGCONT)
                    if self.closed.wait(cycle * duty):
                        break
                    os.kill(self.process.pid, signal.SIGSTOP)
                    if self.closed.wait(cycle * (1 - duty)):
                        break
                elif self.closed.wait(cycle):
                    break
        except (OSError, ProcessLookupError):
            pass
        finally:
            if can_throttle and self.process.poll() is None:
                try:
                    os.kill(self.process.pid, signal.SIGCONT)
                except OSError:
                    pass

    def receive(self, timeout):
        try:
            result = self.responses.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise SpeechError('Озвучка не уложилась в отведённое время. Текстовый ответ уже доступен.') from None
        if result.get('error'):
            raise SpeechError(self.guard_reason or 'Silero не смог озвучить эту строку летописи.')
        return result

    def render(self, text, path, config):
        ident = uuid.uuid4().hex
        self.process.stdin.write(json.dumps({'id': ident, 'text': text, 'output': str(path),
                                            **voice_settings(config), 'max_seconds': config['max_seconds']}, ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        result = self.receive(config['timeout_seconds'])
        if result.get('id') != ident:
            raise SpeechError('Процесс озвучки вернул неверный результат.')
        return result

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()


class Speech:
    def __init__(self, worker):
        self.worker, self.store = worker, worker.store
        self.root = self.store.data_dir / 'speech'
        self.local = threading.local()
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=5)
        self.reserved = set()
        self.reserved_people = {}
        self.active = None
        self.engine = None
        self.last_engine_use = 0
        self.preparing = False
        self.prepare_process = None
        self.prepare_stop = threading.Event()
        self.closed = threading.Event()
        self.engine_lock = threading.RLock()
        self.loop_thread = None
        self.revision = 0
        with self.store.lock, self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS speech_events (
                id INTEGER PRIMARY KEY,at INTEGER NOT NULL,chat TEXT NOT NULL,person TEXT NOT NULL,
                status TEXT NOT NULL,detail TEXT NOT NULL,queue_ms REAL NOT NULL DEFAULT 0,
                synthesis_ms REAL NOT NULL DEFAULT 0,duration REAL NOT NULL DEFAULT 0)''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS speech_dialogs (
                id INTEGER PRIMARY KEY,chat TEXT NOT NULL,person TEXT NOT NULL,question TEXT NOT NULL,
                answer TEXT NOT NULL,spoken_text TEXT NOT NULL,at INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,audio_message_id INTEGER NOT NULL,
                UNIQUE(chat,audio_message_id))''')
            self.store.db.execute('CREATE INDEX IF NOT EXISTS speech_dialogs_person ON speech_dialogs(chat,person,at DESC)')

    def settings(self):
        try:
            saved = json.loads(self.store.setting('speech_config', '{}'))
        except ValueError:
            saved = {}
        config = {key: saved.get(key, value) for key, value in DEFAULTS.items()}
        config.update(voice_settings({key: saved[key] for key in VOICE_KEYS if key in saved}))
        return config

    def configure(self, data):
        config = self.settings()
        for key in DEFAULTS:
            if key in data:
                config[key] = data[key]
        config['enabled'] = config['enabled'] is True
        config['automatic_enabled'] = config['automatic_enabled'] is True
        config['chats'] = '\n'.join(dict.fromkeys(re.split(r'[,;\s]+', str(config['chats']).strip())))
        chats = config['chats'].split()
        if len(chats) > 100 or any(not re.fullmatch(r'-?[1-9]\d{0,19}', chat) for chat in chats):
            raise ValueError('Укажите числовые ID разрешённых чатов, по одному на строку.')
        if config['enabled'] and not chats:
            raise ValueError('Для озвучки обязательно укажите разрешённые чаты. Пустой список запрещает все чаты.')
        config['automatic_chats'] = '\n'.join(dict.fromkeys(re.split(r'[,;\s]+', str(config['automatic_chats']).strip())))
        auto_chats = config['automatic_chats'].split()
        if len(auto_chats) > 100 or any(not re.fullmatch(r'-?[1-9]\d{0,19}', chat) for chat in auto_chats):
            raise ValueError('Для автоматической озвучки укажите числовые ID чатов, по одному на строку.')
        if config['automatic_enabled'] and not auto_chats:
            raise ValueError('Укажите чаты, в которых разрешены автоматические голосовые ответы.')
        allowed = {self.store.canonical_chat_id(chat) for chat in chats}
        if config['automatic_enabled'] and any(self.store.canonical_chat_id(chat) not in allowed for chat in auto_chats):
            raise ValueError('Чаты автоматической озвучки также должны входить в общий разрешённый список выше.')
        config.update(voice_settings({key: data[key] for key in VOICE_KEYS if key in data}, self.settings()))
        bounds = {'max_chars': (50, 1000), 'max_seconds': (5, 60), 'timeout_seconds': (30, 300),
                  'queue_limit': (1, 5), 'user_pause_seconds': (10, 3600), 'chat_pause_seconds': (5, 3600),
                  'cpu_percent': (10, 50), 'memory_mb': (256, 2048), 'automatic_chance': (0, 100),
                  'automatic_pause_seconds': (30, 86400), 'automatic_user_pause_seconds': (30, 86400),
                  'automatic_daily_limit': (1, 100), 'automatic_max_chars': (50, 1000)}
        for key, (low, high) in bounds.items():
            try:
                number = float(config[key])
                if not number.is_integer() or not low <= number <= high:
                    raise ValueError()
                config[key] = int(number)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f'{key}: допустимо целое значение от {low} до {high}.') from None
        config['pronunciation'] = str(config['pronunciation'])
        config['error_message'] = str(config['error_message']).strip()
        if not 1 <= len(config['error_message']) <= 500:
            raise ValueError('Сообщение об ошибке озвучки должно содержать от 1 до 500 символов.')
        if len(config['pronunciation']) > 4000 or any('|' not in line or not all(x.strip() for x in line.split('|', 1)) for line in config['pronunciation'].splitlines() if line.strip()):
            raise ValueError('Произношение: исходное имя|как произнести, по одному на строку.')
        with self.lock:
            self.store.set_settings({'speech_config': json.dumps(config, ensure_ascii=False)})
            self.revision += 1
        # Apply a changed CPU/RAM budget promptly, also stopping a disabled voice.
        self.close_engine()
        if not config['enabled']:
            self.prepare_stop.set()
            if self.prepare_process and self.prepare_process.poll() is None:
                self.prepare_process.kill()
            self.clear_queue()
            self.event('', '', 'unloaded', 'Озвучка выключена. Процесс Silero завершён, очередь очищена.')
        elif self.ready() and not self.preparing:
            self.activate()
        return config

    def allowed(self, chat):
        config = self.settings()
        actual = self.store.canonical_chat_id(str(chat))
        return config['enabled'] and any(self.store.canonical_chat_id(item) == actual for item in config['chats'].split())

    def ready(self):
        return (self.root / 'ready.json').is_file() and (self.root / 'v5_3_ru.pt').is_file()

    def event(self, chat, person, status, detail, **timings):
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO speech_events(at,chat,person,status,detail,queue_ms,synthesis_ms,duration) VALUES (?,?,?,?,?,?,?,?)',
                                  (int(time.time()), str(chat), str(person), status, detail[:250], timings.get('queue_ms', 0), timings.get('synthesis_ms', 0), timings.get('duration', 0)))
            self.store.db.execute('DELETE FROM speech_events WHERE id<=(SELECT MAX(id)-100 FROM speech_events)')

    def panel(self):
        with self.store.lock:
            events = [dict(row) for row in self.store.db.execute('SELECT * FROM speech_events ORDER BY id DESC LIMIT 30')]
        with self.lock:
            active = {'chat': self.active['chat'], 'person': self.active['person']} if self.active else None
            preparing = self.preparing
        # The loader can replace self.engine while this HTTP request runs.
        # Read one snapshot, otherwise a concurrent unload can break GET /speech.
        engine = self.engine
        process = engine.process if engine else None
        running = bool(process and process.poll() is None)
        return {'settings': self.settings(), 'ready': self.ready(), 'preparing': preparing,
                'waiting': self.jobs.qsize(), 'active': active, 'events': events,
                'loaded': bool(engine and engine.ready and running),
                'rss_mb': round(engine.rss_mb, 1) if running else 0,
                'private_chat': self.store.setting('admin_user_id'), 'presets': list(PRESETS),
                'cpu_cap_supported': sys.platform.startswith('linux')}

    def engine_env(self):
        env = dict(os.environ)
        # The child needs neither Telegram nor model account credentials.
        for key in tuple(env):
            if any(part in key.upper() for part in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY')):
                env.pop(key, None)
        env.update({key: '1' for key in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS')})
        env['CUDA_VISIBLE_DEVICES'] = ''
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONPATH'] = os.pathsep.join((str(self.root / 'runtime'), str(Path(__file__).resolve().parent.parent), env.get('PYTHONPATH', '')))
        return env

    def engine_command(self):
        return [sys.executable, '-m', 'repostbot.speech_engine', str(self.root)]

    def prepare(self):
        with self.lock:
            if self.preparing:
                return {'message': 'Подготовка Silero уже выполняется в фоне.'}
            if self.reserved:
                raise ValueError('Дождитесь завершения текущей озвучки перед подготовкой.')
            if self.closed.is_set():
                raise ValueError('Бот выключен.')
            self.preparing = True
            self.prepare_stop.clear()
        threading.Thread(target=self._prepare, daemon=True, name='silero-prepare').start()
        return {'message': 'Подготовка Silero запущена в фоне. Обычные ответы продолжают работать.'}

    def _install(self, args):
        # Wheels only: never build torch or native encoders on the weak NAS.
        cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
               '--only-binary=:all:', '--timeout', '20', '--retries', '1', '--upgrade',
               '--target', str(self.root / 'runtime'), *args]
        if sys.platform.startswith('linux') and shutil.which('nice'):
            cmd = [shutil.which('nice'), '-n', '15', *cmd]
        self.prepare_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.engine_env())
        deadline = time.monotonic() + 900
        while self.prepare_process.poll() is None:
            if self.closed.wait(.3) or self.prepare_stop.is_set() or time.monotonic() > deadline:
                self.prepare_process.kill()
                self.prepare_process.wait()
                raise SpeechError('Подготовка остановлена или превысила время ожидания.')
        if self.prepare_process.returncode:
            raise SpeechError('Не удалось установить CPU-зависимости. Проверьте интернет и свободное место в data.')
        self.prepare_process = None

    def _prepare(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / 'audio').mkdir(exist_ok=True)
            (self.root / 'ready.json').unlink(missing_ok=True)
            self.close_engine()
            self.event('', '', 'prepare', 'Установка PyTorch CPU и кодировщика MP3 в data.')
            try:
                runtime_info = json.loads((self.root / 'runtime-info.json').read_text())
            except (OSError, ValueError):
                runtime_info = {}
            if not (self.root / 'runtime' / 'torch' / '__init__.py').is_file() or runtime_info.get('python_tag') != sys.implementation.cache_tag:
                self._install(['torch>=2.2,<3', '--index-url', 'https://download.pytorch.org/whl/cpu'])
            self._install(['numpy>=1.26,<3', 'lameenc>=1.7,<2'])
            (self.root / 'runtime-info.json').write_text(json.dumps({'python_tag': sys.implementation.cache_tag}), encoding='utf-8')
            model = self.root / 'v5_3_ru.pt'
            if not model.is_file():
                from .core import _https_context, urlopen
                self.event('', '', 'prepare', 'Скачивание русского голоса Silero.')
                temporary = model.with_suffix('.part')
                try:
                    size = 0
                    with urlopen(MODEL_URL, timeout=30, context=_https_context(True)) as response, temporary.open('wb') as output:
                        while True:
                            if self.closed.is_set() or self.prepare_stop.is_set():
                                raise SpeechError('Подготовка остановлена.')
                            block = response.read(256 * 1024)
                            if not block:
                                break
                            size += len(block)
                            if size > 256 * 1024 * 1024:
                                raise SpeechError('Файл голоса превысил допустимый размер.')
                            output.write(block)
                    if size < 1024 * 1024:
                        raise SpeechError('Файл голоса передан не полностью.')
                    temporary.replace(model)
                finally:
                    temporary.unlink(missing_ok=True)
            self.event('', '', 'prepare', 'Проверка загрузки модели в отдельном CPU-процессе.')
            if self.prepare_stop.is_set() or self.closed.is_set():
                raise SpeechError('Подготовка остановлена.')
            self.get_engine()
            self.close_engine()
            if self.prepare_stop.is_set() or self.closed.is_set():
                raise SpeechError('Подготовка остановлена.')
            (self.root / 'ready.json').write_text(json.dumps({'model': 'v5_3_ru', 'at': int(time.time())}), encoding='utf-8')
            self.event('', '', 'ready', 'Silero готов. Озвучка запускается только командой в разрешённых чатах.')
        except Exception as exc:
            self.close_engine()
            self.event('', '', 'error', str(exc) if isinstance(exc, SpeechError) else 'Подготовка Silero не удалась: ' + type(exc).__name__)
        finally:
            with self.lock:
                self.preparing = False
            if not self.closed.is_set() and self.settings()['enabled'] and self.ready():
                self.activate()

    def get_engine(self, required_revision=None):
        with self.engine_lock:
            if self.closed.is_set() or self.preparing and self.prepare_stop.is_set() or required_revision is not None and (required_revision != self.revision or not self.settings()['enabled']):
                raise SpeechError('Загрузка голоса отменена после изменения настроек.')
            if self.engine is None or self.engine.closed.is_set() or self.engine.process is None or self.engine.process.poll() is not None:
                if self.engine:
                    self.engine.close()
                self.engine = SileroProcess(self, required_revision)
                try:
                    self.engine.start()
                except Exception:
                    self.engine.close()
                    self.engine = None
                    raise
            return self.engine

    def close_engine(self):
        # Do not take engine_lock here: abort must interrupt a stalled start.
        engine = self.engine
        if engine:
            engine.close()

    def request(self, token, msg):
        question = re.sub(r'^/voice(?:@[A-Za-z0-9_]+)?\s*', '', msg.get('text') or msg.get('caption') or '', flags=re.I).strip()
        if not question:
            anchor = self.store.replied_bot_context(msg['chat']['id'], (msg.get('reply_to_message') or {}).get('message_id'))
            question = anchor['question'] if anchor and anchor['question'] else '[Новое вложение без подписи]' if any(msg.get(key) for key in ('photo', 'sticker', 'animation', 'video')) else 'Озвучь предыдущее сообщение бота.'
        return {'token': token, 'token_hash': hashlib.sha256(token.encode()).hexdigest(),
                'chat': str(msg['chat']['id']), 'person': str((msg.get('from') or {}).get('id', '')),
                'message_id': int(msg['message_id']), 'thread_id': msg.get('message_thread_id', 0),
                'hidden': bool(msg.get('ephemeral_message_id') or msg.get('receiver_user') or msg.get('is_ephemeral')),
                'test_voice': (msg['chat'].get('type') == 'private'
                               and str((msg.get('from') or {}).get('id', '')) == self.store.setting('admin_user_id')
                               and bool(re.match(r'^/voice(?:@[A-Za-z0-9_]+)?(?:\s|$)', msg.get('text') or '', re.I))),
                'revision': self.revision, 'dialog_question': question[:1500]}

    def availability_error(self, chat):
        if not self.allowed(chat):
            return 'Озвучка доступна только в чатах, разрешённых владельцем.'
        if not self.ready() or self.preparing:
            return 'Голос ещё не готов. Владельцу нужно подготовить Silero в панели → Озвучка.'
        return ''

    def dialogue_text(self, answer):
        """Never send ledger notices or operational status text to TTS."""
        while isinstance(answer, GrudgeReply):
            answer = answer.body
        if isinstance(answer, (StandaloneReply, TransitionReply, UnavailableReply)):
            return ""
        stock = {line.strip() for kind in ('hint', 'strong', 'refuse', 'reply_grudge', 'reply_roleplay', 'help')
                 for line in self.worker.community.template_text(kind).splitlines() if line.strip()}
        stock.update(str(self.store.setting(key) or '').strip() for key in ('ai_limit_message', 'ai_error_message', 'admin_captcha_message', 'fallback'))
        operational = re.compile(r'книг\w*\s+обид|велик\w*\s+книг|репутац|рейтинг|промпт|\bAPI\b|\bOCR\b|Silero|Tesseract|/quest|/voice|'
                                 r'(?:модел|нейросет)\w*.{0,45}(?:недоступ|лимит|ошиб|ключ)|'
                                 r'(?:включите|настройте|проверьте).{0,55}(?:панел|API|озвучк)|'
                                 r'(?:не удалось|ошибка|запрос).{0,45}(?:API|озвуч|сервер|модел)|'
                                 r'(?:доступн\w*|исчерпан\w*)\s+лимит', re.I)
        parts = re.split(r'\n\s*\n', str(answer or ''))
        return '\n\n'.join(part for part in parts if part.strip() not in stock and not operational.search(part)).strip()

    @staticmethod
    def book_notice(answer):
        notices = []
        while isinstance(answer, GrudgeReply):
            if answer.notice:
                notices.append(answer.notice)
            answer = answer.body
        return '\n\n'.join(notices)

    def submit(self, request, text):
        text = self.dialogue_text(text)
        if not text:
            return 'Служебные сообщения и Книга обид остаются только текстом.'
        config = self.settings()
        error = self.availability_error(request['chat'])
        if error:
            return error
        dialog_text = self.worker.community.redact_terms(text)
        text = plain_speech(dialog_text, config)
        if not text:
            return 'В этой строке нет текста для озвучки.'
        key = (request['chat'], request['message_id'])
        now = time.time()
        with self.lock:
            if self.closed.is_set() or key in self.reserved:
                return 'Эта озвучка уже в очереди.'
            if self.jobs.qsize() >= config['queue_limit']:
                return 'Писарь занят озвучкой. Попробуй немного позже.'
            if request.get('automatic') and not self.automatic_allowed(request['chat'], request['person']):
                return 'Пауза автоматической озвучки или дневной предел.'
            user_key = 'speech_user:' + request['chat'] + ':' + request['person']
            chat_key = 'speech_chat:' + request['chat']
            if not request.get('preview') and (now - float(self.store.state(user_key, '0')) < config['user_pause_seconds'] or now - float(self.store.state(chat_key, '0')) < config['chat_pause_seconds']):
                return 'Голосу нужна короткая передышка. Попробуй немного позже.'
            self.reserved.add(key)
            self.reserved_people[key] = request['person']
            job = {**request, 'text': text, 'dialog_answer': dialog_text[:3500], 'at': now, 'revision': self.revision}
            self.jobs.put_nowait(job)
            self.store.set_state(user_key, str(now))
            self.store.set_state(chat_key, str(now))
            if request.get('automatic'):
                self.store.set_state('speech_auto_chat:' + self.store.canonical_chat_id(request['chat']), str(now))
                self.store.set_state('speech_auto_person:' + request['person'], str(now))
                daily_key = self.automatic_day_key(request['chat'])
                self.store.set_state(daily_key, str(int(self.store.state(daily_key, '0')) + 1))
        if request.get('automatic'):
            with self.worker.telegram_queue_lock:
                self.worker.telegram_voice_lanes.add(request['person'])
        self.event(request['chat'], request['person'], 'queued', 'Озвучка поставлена в отдельную очередь.')
        self.ensure_loop()
        return ''

    def automatic_day_key(self, chat):
        day = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
        return 'speech_auto_day:' + self.store.canonical_chat_id(str(chat)) + ':' + day

    def automatic_allowed(self, chat, person):
        config = self.settings()
        if (not config['automatic_enabled'] or not config['automatic_chance'] or not person or not str(person).isdigit()
                or str(person) == '0' or not self.allowed(chat) or not self.ready() or self.preparing):
            return False
        actual = self.store.canonical_chat_id(str(chat))
        if not any(self.store.canonical_chat_id(item) == actual for item in config['automatic_chats'].split()):
            return False
        now = time.time()
        return (now - float(self.store.state('speech_auto_chat:' + actual, '0')) >= config['automatic_pause_seconds']
                and now - float(self.store.state('speech_auto_person:' + str(person), '0')) >= config['automatic_user_pause_seconds']
                and int(self.store.state(self.automatic_day_key(chat), '0')) < config['automatic_daily_limit'])

    def offer_automatic(self, request, answer):
        """The existing dialogue analysis chose the reply; no extra model request."""
        if not answer or request.get('hidden') or not self.automatic_allowed(request['chat'], request['person']):
            return False
        spoken_answer = self.dialogue_text(answer)
        if not spoken_answer:
            return False
        config = self.settings()
        # Do not voice only the start of a long answer and lose its meaning.
        spoken = plain_speech(self.worker.community.redact_terms(spoken_answer), {**config, 'max_chars': 10000})
        if not spoken or len(spoken) > min(config['max_chars'], config['automatic_max_chars']):
            return False
        if secrets.randbelow(100) >= config['automatic_chance']:
            return False
        try:
            error = self.submit({**request, 'automatic': True, 'text_delivered': False}, spoken_answer)
            if error:
                self.event(request['chat'], request['person'], 'skipped', 'Автоматический голос: ' + error)
            elif self.book_notice(answer):
                try:
                    self.worker.telegram_delivery(request['token'], 'sendMessage', {
                        'chat_id': request['chat'], 'text': self.worker.community.redact_terms(self.book_notice(answer)),
                        'reply_parameters': {'message_id': request['message_id'], 'allow_sending_without_reply': True},
                        'disable_notification': True})
                except Exception as exc:
                    self.event(request['chat'], request['person'], 'error', 'Текстовое уведомление Книги не доставлено: ' + type(exc).__name__)
            return not error
        except Exception as exc:
            self.event(request['chat'], request['person'], 'error', 'Автоматический голос не поставлен в очередь: ' + type(exc).__name__)
            return False

    def capture_reply(self, chat, message_id, answer):
        request = getattr(self.local, 'request', None)
        if request and request['chat'] == str(chat) and request['message_id'] == message_id:
            self.local.request = None
            if not self.dialogue_text(answer):
                return
            request['text_delivered'] = True
            error = self.submit(request, answer)
            if error:
                self.event(chat, request['person'], 'skipped', error)
                self.notify(request, error)

    def ensure_loop(self):
        with self.lock:
            if self.loop_thread is None or not self.loop_thread.is_alive():
                self.loop_thread = threading.Thread(target=self.loop, daemon=True, name='silero-queue')
                self.loop_thread.start()

    def activate(self):
        if self.settings()['enabled'] and self.ready() and not self.preparing and not self.closed.is_set():
            with self.lock:
                if self.jobs.empty():
                    self.jobs.put_nowait({'kind': 'warmup', 'revision': self.revision})
            self.ensure_loop()

    def clear_queue(self):
        tokens = set()
        fallbacks = []
        with self.lock:
            while True:
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if job.get('kind') != 'warmup':
                    if job.get('automatic') and not self.closed.is_set() and not self.worker.stop.is_set():
                        fallbacks.append(job)
                    else:
                        self.reserved.discard((job['chat'], job['message_id']))
                        self.reserved_people.pop((job['chat'], job['message_id']), None)
                        tokens.add(job['token'])
                self.jobs.task_done()
        for token in tokens:
            self.wake_messages(token)
        if fallbacks:
            def deliver_fallbacks():
                for job in fallbacks:
                    try:
                        self.notify(job, '', failure=True)
                    finally:
                        with self.lock:
                            self.reserved.discard((job['chat'], job['message_id']))
                            self.reserved_people.pop((job['chat'], job['message_id']), None)
                        self.wake_messages(job['token'])
            threading.Thread(target=deliver_fallbacks, daemon=True, name='speech-cancelled-text').start()

    def blocked_people(self):
        with self.lock:
            return set(self.reserved_people.values()) - {''}

    def wake_messages(self, token):
        if not self.closed.is_set() and not self.worker.stop.is_set():
            self.worker.schedule_telegram_inbox(token)

    def remember_dialog(self, job, sent, voice_sent=True):
        chat = self.store.canonical_chat_id(str((sent.get('chat') or {}).get('id', job['chat'])))
        if not job['message_id'] or job.get('preview'):
            return
        answer = job['dialog_answer'][:1500]
        self.store.remember_bot_reply(chat, job['message_id'], sent, answer)
        self.worker.social.personal.record_reply(chat, job['person'], answer, 'reply:' + str(job['message_id']), sent.get('message_id', 0))
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE bot_reply_memory SET question=? WHERE chat=? AND message_id=?', (job['dialog_question'][:500], chat, sent['message_id']))
            self.store.db.execute('INSERT OR REPLACE INTO speech_dialogs(chat,person,question,answer,spoken_text,at,source_message_id,audio_message_id) VALUES (?,?,?,?,?,?,?,?)',
                                  (chat, job['person'], job['dialog_question'], answer, job['text'] if voice_sent else '', int(time.time()), job['message_id'], sent['message_id']))
            cutoff = self.store.memory_cutoff()
            if cutoff >= 0:
                self.store.db.execute('DELETE FROM speech_dialogs WHERE at<?', (cutoff,))

    def recent_dialog(self, chat, person):
        if not person or int(self.store.setting('memory_turns', '2')) <= 0:
            return None
        chat = self.store.canonical_chat_id(str(chat))
        with self.store.lock:
            row = self.store.db.execute('SELECT question,answer,spoken_text,at,source_message_id FROM speech_dialogs WHERE chat=? AND person=? AND at>=? ORDER BY at DESC,id DESC LIMIT 1',
                                        (chat, str(person), self.store.memory_cutoff())).fetchone()
        return dict(row) if row else None

    def test(self, data):
        chat = str(data.get('chat') or '').strip()
        text = str(data.get('text') or '').strip()
        self.event(chat[:40], '', 'test_request', 'Получен запрос веб-теста в ' + ('личный чат.' if chat.isdigit() else 'Telegram-чат.'))
        if not re.fullmatch(r'-?[1-9]\d{0,19}', chat):
            self.event(chat[:40], '', 'error', 'Тест: укажите числовой ID Telegram-чата.')
            raise ValueError('Укажите числовой ID. Для личного чата нужен ваш Telegram user ID без минуса.')
        if not 1 <= len(text) <= 1000:
            raise ValueError('Укажите тестовую фразу длиной до 1000 символов.')
        if not self.allowed(chat):
            message = ('Добавьте ID личного чата в разрешённые чаты озвучки, сохраните настройки и включите озвучку.' if chat.isdigit()
                       else 'Тест доступен только в разрешённых чатах при включённой озвучке.')
            self.event(chat, '', 'error', 'Тест: ' + message)
            raise ValueError(message)
        token = self.store.setting('telegram_token')
        if not token:
            raise ValueError('Сначала подключите Telegram.')
        request = self.request(token, {'chat': {'id': chat}, 'from': {'id': self.store.setting('admin_user_id', '0')}, 'message_id': 0})
        request['preview'] = True
        if any(key in data for key in VOICE_KEYS):
            request['voice_preview'] = voice_settings({key: data[key] for key in VOICE_KEYS if key in data}, self.settings())
        error = self.submit(request, text)
        if error:
            raise ValueError(error)
        return {'message': 'Тест поставлен в очередь. Голос придёт в выбранный чат; результат появится в журнале.'}

    @staticmethod
    def telegram_error(description, token=''):
        description = str(description).replace(token, '[скрыто]') if token else str(description)
        lower = description.casefold()
        if 'chat not found' in lower or "can't initiate conversation" in lower:
            return 'Telegram не видит личный чат. Откройте бота, отправьте /start и проверьте свой числовой Telegram ID.'
        if 'blocked by the user' in lower:
            return 'Бот заблокирован получателем. Разблокируйте его в Telegram и отправьте /start.'
        if 'voice_messages_forbidden' in lower or 'voice messages' in lower and 'forbidden' in lower:
            return 'В настройках приватности получателя запрещены голосовые сообщения.'
        return 'Telegram не принял голосовую запись: ' + description[:180]

    def check_test_target(self, job):
        self.event(job['chat'], job['person'], 'checking', 'Проверка доступности тестового Telegram-чата.')
        try:
            info = self.worker.telegram_delivery(job['token'], 'getChat', {'chat_id': job['chat']}, timeout=10)
        except Exception as exc:
            raise SpeechError(self.telegram_error(str(exc), job['token'])) from None
        if info.get('type') == 'private' and info.get('has_restricted_voice_and_video_messages'):
            raise SpeechError('В настройках приватности этого личного чата ограничены голосовые сообщения.')

    def valid_job(self, job):
        token = self.store.setting('telegram_token')
        return (not self.closed.is_set() and not self.worker.stop.is_set() and job['revision'] == self.revision
                and self.allowed(job['chat']) and token and hashlib.sha256(token.encode()).hexdigest() == job['token_hash'])

    def notify(self, job, text, failure=False):
        if self.closed.is_set() or self.worker.stop.is_set():
            return
        token = self.store.setting('telegram_token')
        ordinary_fallback = (job.get('automatic') and failure and not self.closed.is_set() and not self.worker.stop.is_set()
                             and token and hashlib.sha256(token.encode()).hexdigest() == job['token_hash']
                             and self.store.setting('reply_enabled', '1') == '1')
        if not ordinary_fallback and not self.valid_job(job):
            return
        try:
            fallback = failure and not job.get('text_delivered') and job.get('dialog_answer')
            if job.get('automatic') and fallback:
                text = ''
            if fallback:
                text = (text + '\n\n' if text else '') + job['dialog_answer']
            payload = {'chat_id': job['chat'], 'text': text[:3500]}
            if job['message_id']:
                payload['reply_parameters'] = {'message_id': job['message_id'], 'allow_sending_without_reply': True}
            if job['thread_id']:
                payload['message_thread_id'] = job['thread_id']
            sent = self.worker.telegram_delivery(job['token'], 'sendMessage', payload)
            with self.lock:
                if self.closed.is_set() or self.worker.stop.is_set():
                    return
                if fallback and isinstance(sent, dict) and sent.get('message_id'):
                    self.remember_dialog(job, sent, voice_sent=False)
        except Exception:
            with self.lock:
                if not self.closed.is_set() and not self.worker.stop.is_set():
                    self.event(job['chat'], job['person'], 'error', 'Не удалось доставить уведомление об озвучке.')

    def run_job(self, job):
        queue_ms = round((time.time() - job['at']) * 1000)
        if not self.valid_job(job) or time.time() - job['at'] > 600:
            if job.get('automatic'):
                self.notify(job, '', failure=True)
            self.event(job['chat'], job['person'], 'cancelled', 'Задание отменено: настройки изменились или очередь устарела.')
            return
        config = self.settings()
        if job.get('preview'):
            self.check_test_target(job)
        # A web preview may compare an unsaved style, but cannot change budgets or enable speech.
        if job.get('preview') and job.get('voice_preview'):
            config.update(voice_settings(job['voice_preview']))
        digest = hashlib.sha256(json.dumps([config['speaker'], config['rate'], config['pitch'], config['max_seconds'], job['text'], 'v5_3_ru'], ensure_ascii=False).encode()).hexdigest()
        audio_dir = self.root / 'audio'
        audio_dir.mkdir(parents=True, exist_ok=True)
        output = audio_dir / (digest + '.mp3')
        cache_hit = output.is_file()
        started = time.monotonic()
        if cache_hit:
            try:
                result = json.loads(output.with_suffix('.json').read_text())
            except (ValueError, OSError):
                result = {'duration': 0, 'clipped': False}
        else:
            self.event(job['chat'], job['person'], 'generating', 'Голос готовится в отдельном процессе.', queue_ms=queue_ms)
            engine = self.get_engine(job['revision'])
            result = engine.render(job['text'], output, config)
            output.with_suffix('.json').write_text(json.dumps({key: result.get(key) for key in ('duration', 'clipped')}), encoding='utf-8')
            self.last_engine_use = time.monotonic()
        if not self.valid_job(job):
            if job.get('automatic'):
                self.notify(job, '', failure=True)
            self.event(job['chat'], job['person'], 'cancelled', 'Голос не отправлен после изменения настроек.')
            return
        if not output.is_file() or not 0 < output.stat().st_size <= 2_000_000:
            raise SpeechError('Не удалось получить голосовую запись.')
        synthesis_ms = round((time.monotonic() - started) * 1000) if not cache_hit else 0
        if job.get('automatic') and result.get('clipped'):
            self.notify(job, '', failure=True)
            self.event(job['chat'], job['person'], 'skipped', 'Автоматический голос превысил длительность; ответ доставлен полностью текстом.')
            return
        caption = voice_test_caption(config) if job.get('preview') or job.get('test_voice') else ''
        delivery_job = {**job, 'voice_caption': caption} if caption else job
        sent = self.upload_voice(delivery_job, output.read_bytes(), result.get('duration', 0))
        self.store.mark_outbound((sent.get('chat') or {}).get('id', job['chat']), sent['message_id'])
        self.remember_dialog(job, sent)
        detail = ('Голос отправлен из кэша.' if cache_hit else 'Голос отправлен.') + (' Запись сокращена по пределу длительности.' if result.get('clipped') else '')
        if caption:
            detail += ' ' + caption.replace('\n', ' ')
        self.event(job['chat'], job['person'], 'sent', detail,
                   queue_ms=queue_ms, synthesis_ms=synthesis_ms, duration=result.get('duration', 0))
        # Bounded disk use; voice files never enter the code update archive.
        files = sorted(audio_dir.glob('*.mp3'), key=lambda file: file.stat().st_mtime, reverse=True)
        for file in files[30:]:
            file.unlink(missing_ok=True)
            file.with_suffix('.json').unlink(missing_ok=True)

    def upload_voice(self, job, raw, duration):
        from .core import _https_context, urlopen, Request, TelegramChatMigrated, telegram_migration
        from urllib.error import HTTPError
        boundary = 'assistant-voice-' + uuid.uuid4().hex
        actual = self.store.canonical_chat_id(job['chat'])
        for attempt in range(2):
            fields = {'chat_id': actual}
            if job.get('voice_caption'):
                fields['caption'] = job['voice_caption'][:1024]
            if job.get('automatic'):
                fields['disable_notification'] = 'true'
            if job['message_id']:
                fields['reply_parameters'] = json.dumps({'message_id': job['message_id'], 'allow_sending_without_reply': True})
            if job['thread_id']:
                fields['message_thread_id'] = str(job['thread_id'])
            if duration:
                fields['duration'] = str(max(1, round(duration)))
            parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode() for name, value in fields.items()]
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="voice"; filename="voice.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode() + raw + f'\r\n--{boundary}--\r\n'.encode())
            request = Request('https://api.telegram.org/bot' + job['token'] + '/sendVoice', data=b''.join(parts), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
            try:
                with urlopen(request, timeout=30, context=_https_context(True)) as response:
                    result = json.load(response)
            except HTTPError as exc:
                try:
                    result = json.loads(exc.read(8192))
                except ValueError:
                    raise SpeechError('Telegram не принял голосовую запись.') from None
            migrated = telegram_migration(result)
            if migrated and not attempt:
                self.store.migrate_telegram_chat(actual, migrated)
                actual = migrated
                continue
            if not result.get('ok'):
                raise SpeechError(self.telegram_error(result.get('description') or 'Неизвестная ошибка', job['token']))
            return result['result']
        raise SpeechError('Не удалось отправить голос в обновлённую группу.')

    def loop(self):
        try:
            while not self.closed.is_set() and not self.worker.stop.is_set() and self.settings()['enabled']:
                try:
                    job = self.jobs.get(timeout=.3)
                except queue.Empty:
                    continue
                if job.get('kind') == 'warmup':
                    try:
                        if job['revision'] == self.revision and self.settings()['enabled']:
                            self.get_engine(job['revision'])
                            self.event('', '', 'loaded', 'Голос загружен: один поток CPU, отдельный процесс с ограничением нагрузки.')
                    except Exception as exc:
                        self.close_engine()
                        self.event('', '', 'error', str(exc) if isinstance(exc, SpeechError) else 'Не удалось загрузить голос.')
                    finally:
                        self.jobs.task_done()
                    continue
                with self.lock:
                    self.active = job
                try:
                    self.run_job(job)
                except Exception as exc:
                    self.close_engine()
                    text = str(exc) if isinstance(exc, SpeechError) else 'Эту строку летописи застлала пелена: голос пока недоступен. Текстовые ответы продолжают работать.'
                    self.event(job['chat'], job['person'], 'error', text)
                    self.notify(job, self.settings()['error_message'], failure=True)
                finally:
                    with self.lock:
                        self.reserved.discard((job['chat'], job['message_id']))
                        self.reserved_people.pop((job['chat'], job['message_id']), None)
                        self.active = None
                    self.jobs.task_done()
                    self.wake_messages(job['token'])
        finally:
            self.close_engine()
            # A fast off/on toggle can happen while the old dispatcher exits.
            with self.lock:
                self.loop_thread = None
                restart = not self.closed.is_set() and not self.worker.stop.is_set() and self.settings()['enabled'] and not self.jobs.empty()
            if restart:
                self.ensure_loop()

    def close(self):
        with self.lock:
            self.closed.set()
        self.prepare_stop.set()
        if self.prepare_process and self.prepare_process.poll() is None:
            self.prepare_process.kill()
        self.close_engine()
        self.clear_queue()

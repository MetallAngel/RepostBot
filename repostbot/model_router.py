"""Opt-in provider pool, official compatible APIs, local health and human grades."""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

PRESETS = {
    'kimi': {'title': 'Kimi / Moonshot', 'base_url': 'https://api.moonshot.ai/v1', 'model': 'kimi-k2-turbo-preview'},
    'openrouter': {'title': 'OpenRouter Free', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'openrouter/free'},
    'groq': {'title': 'Groq', 'base_url': 'https://api.groq.com/openai/v1', 'model': ''},
    'gemini': {'title': 'Gemini AI Studio', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'model': ''},
    'litellm': {'title': 'LiteLLM / совместимый API', 'base_url': 'http://localhost:4000/v1', 'model': ''},
}
LEGACY = ('openai', 'chatgpt', 'local')
DEFAULT_MODELS = {'openai': 'gpt-4.1-mini', 'chatgpt': '', 'local': 'qwen3:4b'}


class ModelRouter:
    def __init__(self, worker):
        self.worker, self.store = worker, worker.store
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='model-check')
        self.busy = set()
        self.success_epoch = {}
        with self.store.lock, self.store.db:
            self.store.db.executescript('''
                CREATE TABLE IF NOT EXISTS model_endpoints (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL,
                    base_url TEXT NOT NULL, model TEXT NOT NULL, vision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS model_metrics (
                    provider TEXT PRIMARY KEY, latency_ms REAL, retry_at INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', checked_at INTEGER NOT NULL DEFAULT 0,
                    daily_at INTEGER NOT NULL DEFAULT 0, grade_total REAL NOT NULL DEFAULT 0,
                    grade_count INTEGER NOT NULL DEFAULT 0, signature TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS model_evaluations (
                    id INTEGER PRIMARY KEY, question TEXT NOT NULL, reference_provider TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS model_evaluation_answers (
                    evaluation_id INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', latency_ms REAL,
                    grade REAL, PRIMARY KEY(evaluation_id,provider));
            ''')
            self.store.db.execute("UPDATE model_evaluations SET status='interrupted' WHERE status='running'")
            columns = {r[1] for r in self.store.db.execute('PRAGMA table_info(model_metrics)')}
            if 'signature' not in columns:
                self.store.db.execute("ALTER TABLE model_metrics ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
            for name, definition in (('probe_error', "TEXT NOT NULL DEFAULT ''"), ('probe_at', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    self.store.db.execute(f'ALTER TABLE model_metrics ADD COLUMN {name} {definition}')

    def enabled(self):
        return self.store.setting('router_enabled', '0') == '1'

    def settings(self):
        return {'enabled': self.enabled(), 'selected': json.loads(self.store.setting('router_selected', '["openai"]')),
                'primary': self.store.setting('router_primary', 'openai'),
                'timeout': int(self.store.setting('router_timeout', '25')),
                'min_quality': int(self.store.setting('router_min_quality', '0')),
                'daily_check': self.store.setting('router_daily_check', '1') == '1'}

    def endpoints(self):
        with self.store.lock:
            return {r['id']: dict(r) for r in self.store.db.execute('SELECT * FROM model_endpoints ORDER BY id')}

    def is_endpoint(self, provider):
        return str(provider).startswith('endpoint:')

    def secret_key(self, provider):
        return 'model_secret:' + provider

    def configured(self, provider):
        profile = self.endpoints().get(provider)
        return bool(profile and profile['model'] and (profile['kind'] == 'litellm' or self.store.setting(self.secret_key(provider))))

    def configure(self, data):
        current = self.settings()
        values = {**current, **{k: data[k] for k in current if k in data}}
        selected = values['selected']
        if not isinstance(selected, list) or len(selected) > 20 or any(x not in (*LEGACY, *self.endpoints()) for x in selected):
            raise ValueError('Выберите до 20 известных провайдеров')
        selected = list(dict.fromkeys(selected))
        if values['enabled'] and (not selected or values['primary'] not in selected):
            raise ValueError('Основная модель должна быть среди включённых')
        timeout, quality = int(values['timeout']), int(values['min_quality'])
        if not 5 <= timeout <= 90 or not 0 <= quality <= 100:
            raise ValueError('Таймаут: 5–90 секунд; качество: 0–100')
        self.store.set_settings({'router_enabled': '1' if values['enabled'] is True else '0',
                                 'router_selected': json.dumps(selected), 'router_primary': str(values['primary']),
                                 'router_timeout': str(timeout), 'router_min_quality': str(quality),
                                 'router_daily_check': '1' if values['daily_check'] is True else '0'})
        return self.settings()

    def save(self, data):
        kind = str(data.get('kind', 'kimi'))
        if kind not in PRESETS:
            raise ValueError('Неизвестный тип подключения')
        identifier = str(data.get('id') or '').removeprefix('endpoint:')
        if not identifier:
            import secrets
            identifier = secrets.token_hex(6)
        if not re.fullmatch(r'[a-z0-9_-]{1,40}', identifier):
            raise ValueError('Некорректный ID подключения')
        provider = 'endpoint:' + identifier
        base = str(data.get('base_url') or PRESETS[kind]['base_url']).strip().rstrip('/')
        url = urlsplit(base)
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Укажите базовый HTTP(S) адрес без ключей и параметров')
        if url.scheme == 'http' and kind != 'litellm':
            raise ValueError('Для облачного API требуется HTTPS')
        model = str(data.get('model') or '').strip()
        if len(model) > 160 or any(ord(c) < 32 for c in model):
            raise ValueError('Выберите точный ID модели из списка провайдера')
        if kind == 'openrouter' and model and not (model.endswith(':free') or model == 'openrouter/free'):
            raise ValueError('OpenRouter Free допускает только модели :free или openrouter/free')
        label = str(data.get('label') or PRESETS[kind]['title']).strip()[:80]
        secret = str(data.get('api_key') or '').strip()
        if secret and (len(secret) > 2048 or any(ord(c) < 33 for c in secret)):
            raise ValueError('Некорректный API-ключ')
        with self.store.lock, self.store.db:
            previous = self.store.db.execute('SELECT * FROM model_endpoints WHERE id=?', (provider,)).fetchone()
            if previous and previous['base_url'] != base and not secret:
                # Never forward a previously stored credential to a new host.
                self.store.set_settings({self.secret_key(provider): ''})
            self.store.db.execute('INSERT INTO model_endpoints VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,kind=excluded.kind,base_url=excluded.base_url,model=excluded.model,vision=excluded.vision',
                                  (provider, label, kind, base, model, int(data.get('vision') is True)))
            self.store.db.execute('DELETE FROM model_metrics WHERE provider=?', (provider,))
        if secret:
            self.store.set_settings({self.secret_key(provider): secret})
        return self.panel()

    def delete(self, provider):
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM model_endpoints WHERE id=?', (str(provider),))
            self.store.db.execute('DELETE FROM model_metrics WHERE provider=?', (str(provider),))
        self.store.set_settings({self.secret_key(str(provider)): ''})
        settings = self.settings()
        settings['selected'] = [x for x in settings['selected'] if x != provider]
        if settings['primary'] == provider:
            settings['primary'] = next(iter(settings['selected']), 'openai')
        if not settings['selected']:
            settings['enabled'] = False
        self.configure(settings)
        return self.panel()

    def metrics(self):
        with self.store.lock:
            rows = [dict(r) for r in self.store.db.execute('SELECT * FROM model_metrics')]
        return {r['provider']: r for r in rows if r.get('signature') == self.signature(r['provider'])}

    def signature(self, provider):
        if self.is_endpoint(provider):
            p = self.endpoints().get(provider, {})
            return p.get('base_url', '') + '|' + p.get('model', '')
        return self.model_name(provider)

    def model_name(self, provider):
        if self.is_endpoint(provider):
            return self.endpoints().get(provider, {}).get('model', '')
        return self.store.setting({'openai':'openai_model','chatgpt':'chatgpt_model','local':'ollama_model'}.get(provider, ''), DEFAULT_MODELS.get(provider, ''))

    def supports_vision(self, provider):
        if self.is_endpoint(provider):
            return bool(self.endpoints().get(provider, {}).get('vision'))
        if provider == 'local':
            return bool(re.search(r'vl|vision|llava|gemma3|moondream|minicpm-v', self.store.setting('ollama_model', ''), re.I))
        if provider == 'openai':
            return not bool(re.search(r'gpt-6-luna|^o1-mini', self.store.setting('openai_model', ''), re.I))
        return provider == 'chatgpt'

    def candidates(self, vision=False):
        settings, metrics = self.settings(), self.metrics()
        now = time.time()
        candidates = []
        for p in settings['selected']:
            if not self.worker.provider_configured(p) or vision and not self.supports_vision(p):
                continue
            metric = metrics.get(p, {})
            count = metric.get('grade_count', 0)
            quality = metric.get('grade_total', 0) / count if count else None
            if metric.get('retry_at', 0) > now or settings['min_quality'] and (quality is None or quality < settings['min_quality']):
                continue
            candidates.append(p)
        def rank(p):
            m = metrics.get(p, {})
            return (p != settings['primary'], m.get('latency_ms') is None, m.get('latency_ms') or 0, settings['selected'].index(p))
        return sorted(candidates, key=rank)

    def observe(self, provider, elapsed_ms=0, error=None):
        from .ai import IncompleteResponse
        if isinstance(error, IncompleteResponse):
            return
        now = int(time.time())
        signature = self.signature(provider)
        with self.lock, self.store.lock, self.store.db:
            # Persist only bounded codes/types, never provider error bodies or secrets.
            self.store.db.execute('INSERT OR IGNORE INTO model_metrics(provider) VALUES (?)', (provider,))
            self.store.db.execute("UPDATE model_metrics SET latency_ms=NULL,retry_at=0,error='',checked_at=0,daily_at=0,grade_total=0,grade_count=0,signature=? WHERE provider=? AND signature<>?", (signature, provider, signature))
            if error is not None:
                self.store.db.execute('UPDATE model_metrics SET error=?,retry_at=?,checked_at=? WHERE provider=?',
                                      (self.error_code(error), now + 900, now, provider))
            else:
                self.success_epoch[provider] = self.success_epoch.get(provider, 0) + 1
                self.store.db.execute('UPDATE model_metrics SET latency_ms=CASE WHEN latency_ms IS NULL THEN ? ELSE latency_ms*.7+?*.3 END,error=\'\',probe_error=\'\',retry_at=0,checked_at=? WHERE provider=?',
                                      (elapsed_ms, elapsed_ms, now, provider))

    @staticmethod
    def error_code(error):
        code = str(getattr(error, 'code', '') or '')
        return type(error).__name__ + (':' + code if re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', code) else '')

    def mark_ready(self, provider):
        """A real successful response overrides stale diagnostic failures, even outside the pool."""
        with self.lock, self.store.lock, self.store.db:
            self.success_epoch[provider] = self.success_epoch.get(provider, 0) + 1
            self.store.db.execute("UPDATE model_metrics SET error='',retry_at=0,probe_error='',checked_at=? WHERE provider=?", (int(time.time()), provider))

    def record_probe(self, provider, error=None):
        signature = self.signature(provider)
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM model_metrics WHERE provider=? AND signature<>?', (provider, signature))
            self.store.db.execute('INSERT OR IGNORE INTO model_metrics(provider,signature) VALUES (?,?)', (provider, signature))
            self.store.db.execute('UPDATE model_metrics SET probe_error=?,probe_at=? WHERE provider=?',
                                  (self.error_code(error) if error else '', int(time.time()), provider))

    @staticmethod
    def messages(messages):
        result = []
        for message in messages:
            content = message.get('content', '')
            if isinstance(content, list):
                content = [{'type': 'image_url', 'image_url': {'url': p['image_url']}} if p.get('type') == 'input_image'
                           else {'type': 'text', 'text': p.get('text', '')} for p in content]
            result.append({'role': message['role'], 'content': content})
        return result

    def request(self, provider, instructions, messages, max_tokens=300, diagnostic=False):
        from .ai import openai_json, AIError, QuotaExceeded, IncompleteResponse
        profile = self.endpoints().get(provider)
        if not profile:
            raise ValueError('Подключение удалено')
        has_image = any(isinstance(m.get('content'), list) and any(p.get('type') == 'input_image' for p in m['content']) for m in messages)
        if has_image and not profile['vision']:
            raise ValueError('Модель не поддерживает изображения')
        payload = {'model': profile['model'], 'messages': [{'role': 'system', 'content': instructions}] + self.messages(messages),
                   'max_tokens': int(max_tokens), 'stream': False}
        if profile['kind'] == 'openrouter':
            payload['provider'] = {'max_price': {'prompt': 0, 'completion': 0}}
        started = time.monotonic()
        try:
            data = openai_json(profile['base_url'] + '/chat/completions', payload,
                               token=self.store.setting(self.secret_key(provider)), timeout=self.settings()['timeout'])
            choice = data.get('choices', [{}])[0]
            text = choice.get('message', {}).get('content')
            if choice.get('finish_reason') == 'length' and (diagnostic or not text):
                raise IncompleteResponse('Тест не завершён: исчерпан лимит генерации', code='max_output_tokens')
            if not isinstance(text, str) or not text.strip():
                raise AIError('Совместимый API вернул пустой ответ')
            usage = data.get('usage', {})
            if not diagnostic:
                self.observe(provider, (time.monotonic() - started) * 1000)
            result = {'text': text.strip()[:12000], 'usage': {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': usage.get('completion_tokens', 0)}}
            self.store.record_ai_usage(provider, profile['model'], result['usage'])
            return result
        except Exception as exc:
            if not diagnostic:
                self.observe(provider, error=exc)
            error_type = QuotaExceeded if isinstance(exc, QuotaExceeded) else IncompleteResponse if isinstance(exc, IncompleteResponse) else AIError
            raise error_type('API ' + profile['label'] + ': ' + type(exc).__name__, code=getattr(exc, 'code', '')) from None

    def model_list(self, provider):
        from .ai import openai_json
        profile = self.endpoints().get(provider)
        if not profile:
            raise ValueError('Сначала сохраните подключение')
        try:
            listing = openai_json(profile['base_url'] + '/models', token=self.store.setting(self.secret_key(provider)), timeout=15)
        except Exception as exc:
            raise ValueError('Список моделей недоступен: ' + type(exc).__name__) from None
        result = []
        for item in listing.get('data', []):
            key = str(item.get('id') or '')
            if not key or profile['kind'] == 'openrouter' and not (key.endswith(':free') or key == 'openrouter/free'):
                continue
            result.append({'id': key, 'vision': 'image' in (item.get('architecture', {}).get('input_modalities') or [])})
        return result[:500]

    def vision_request(self, instructions, messages, max_tokens=180):
        """Try only selected image-capable providers, retaining text-only reserves."""
        from .ai import response_text
        from .core import request_json
        for provider in self.candidates(vision=True):
            started = time.monotonic()
            try:
                if self.is_endpoint(provider):
                    return self.request(provider, instructions, messages, max_tokens)
                if provider == 'local':
                    local = []
                    for message in messages:
                        content = message.get('content', '')
                        item = {'role': message['role'], 'content': content}
                        if isinstance(content, list):
                            item['content'] = '\n'.join(p.get('text', '') for p in content if p.get('type') == 'input_text')
                            item['images'] = [p['image_url'].split(',', 1)[1] for p in content if p.get('type') == 'input_image']
                        local.append(item)
                    data = request_json(self.store.setting('ollama_url', 'http://127.0.0.1:11434').rstrip('/')+'/api/chat',
                                        {'model':self.store.setting('ollama_model'), 'stream':False, 'messages':[{'role':'system','content':instructions}]+local}, timeout=self.settings()['timeout'])
                    result = {'text':data.get('message',{}).get('content','')}
                else:
                    account = provider == 'chatgpt'
                    model = self.store.setting('chatgpt_model' if account else 'openai_model', 'gpt-4.1-mini')
                    token = self.worker.chatgpt.access_token() if account else self.store.setting('openai_api_key')
                    result = response_text(token, model, instructions, messages, account=account, max_tokens=max_tokens, timeout=self.settings()['timeout'])
                    self.store.record_ai_usage(provider, model, result.get('usage', {}))
                if not result.get('text'):
                    raise ValueError('Пустой ответ модели изображений')
                self.observe(provider, (time.monotonic()-started)*1000)
                return result
            except Exception as exc:
                self.worker.model_mark_failed(provider, exc)
        raise ValueError('Нет доступных выбранных моделей с поддержкой изображений')

    def panel(self):
        from .local_ocr import status
        profiles, metrics = self.endpoints(), self.metrics()
        providers = [{'id': p, 'label': self.worker.provider_label(p), 'model': self.model_name(p), 'builtin': True} for p in LEGACY]
        providers.extend({**p, 'builtin': False} for p in profiles.values())
        for p in providers:
            p['configured'] = self.worker.provider_configured(p['id'])
            p['api_key_configured'] = bool(self.store.setting(self.secret_key(p['id'])))
            p['metrics'] = metrics.get(p['id'], {})
        with self.store.lock:
            evaluations = [dict(r) for r in self.store.db.execute('SELECT * FROM model_evaluations ORDER BY id DESC LIMIT 10')]
            for evaluation in evaluations:
                evaluation['answers'] = [dict(r) for r in self.store.db.execute('SELECT * FROM model_evaluation_answers WHERE evaluation_id=?', (evaluation['id'],))]
        with self.lock:
            busy = sorted(self.busy)
        return {'settings': self.settings(), 'providers': providers, 'presets': PRESETS, 'evaluations': evaluations, 'ocr': status(), 'busy': busy}

    def _submit(self, key, task):
        with self.lock:
            if key in self.busy:
                return False
            self.busy.add(key)
        def run():
            try:
                task()
            finally:
                with self.lock:
                    self.busy.discard(key)
        self.pool.submit(run)
        return True

    def check(self, provider):
        if provider not in (*LEGACY, *self.endpoints()):
            raise ValueError('Неизвестный провайдер')
        def task():
            started = time.monotonic()
            signature = self.signature(provider)
            with self.lock:
                epoch = self.success_epoch.get(provider, 0)
            try:
                if not self.worker.probe_model_provider(provider):
                    raise ValueError('Модель не настроена')
                if signature != self.signature(provider):
                    return
                self.observe(provider, (time.monotonic() - started) * 1000)
                self.worker.model_mark_ready(provider)
                self.record_probe(provider)
            except Exception as exc:
                from .ai import IncompleteResponse
                with self.lock:
                    if signature != self.signature(provider) or self.success_epoch.get(provider, 0) != epoch:
                        return
                    self.record_probe(provider, exc)
                    # An unfinished sample is a test limitation, not an outage.
                    if not isinstance(exc, IncompleteResponse):
                        self.observe(provider, error=exc)
        return self._submit(provider, task)

    def daily_tick(self):
        if not self.enabled() or not self.settings()['daily_check']:
            return
        now, metrics = int(time.time()), self.metrics()
        for p in self.settings()['selected']:
            if not self.worker.provider_configured(p) or now - metrics.get(p, {}).get('daily_at', 0) < 86400:
                continue
            with self.store.lock, self.store.db:
                signature = self.signature(p)
                self.store.db.execute('DELETE FROM model_metrics WHERE provider=? AND signature<>?', (p, signature))
                self.store.db.execute('INSERT OR IGNORE INTO model_metrics(provider,signature) VALUES (?,?)', (p, signature))
                self.store.db.execute('UPDATE model_metrics SET daily_at=? WHERE provider=?', (now, p))
            self.check(p)

    def _sample(self, provider, question):
        if self.is_endpoint(provider):
            return self.request(provider, 'Ответь по существу. Не выдумывай факты.', [{'role':'user', 'content':question}], 400)['text']
        from .ai import response_text
        if provider == 'local':
            from .core import request_json
            data = request_json(self.store.setting('ollama_url', 'http://127.0.0.1:11434').rstrip('/') + '/api/chat',
                                {'model':self.store.setting('ollama_model','qwen3:4b'), 'stream':False, 'messages':[{'role':'user','content':question}]}, timeout=self.settings()['timeout'])
            text = data.get('message', {}).get('content', '')
            if not text:
                raise ValueError('Ollama вернула пустой ответ')
            return text
        account = provider == 'chatgpt'
        token = self.worker.chatgpt.access_token() if account else self.store.setting('openai_api_key')
        model = self.model_name(provider)
        result = response_text(token, model, 'Ответь по существу. Не выдумывай факты.', [{'role':'user','content':question}], account=account, max_tokens=400, timeout=self.settings()['timeout'])
        self.store.record_ai_usage(provider, model, result.get('usage', {}))
        return result['text']

    def evaluate(self, data):
        question, reference = str(data.get('question') or '').strip(), str(data.get('reference') or 'chatgpt')
        if not question or len(question) > 2000 or reference not in LEGACY or not self.worker.provider_configured(reference):
            raise ValueError('Введите вопрос до 2000 символов и подключите эталонную модель')
        candidates = [p for p in self.settings()['selected'] if p != reference and self.worker.provider_configured(p)][:8]
        with self.lock:
            if 'evaluation' in self.busy:
                raise ValueError('Сравнение уже выполняется')
            with self.store.lock, self.store.db:
                identifier = self.store.db.execute('INSERT INTO model_evaluations(question,reference_provider,status,at) VALUES (?,?,?,?)', (question, reference, 'running', int(time.time()))).lastrowid
        def task():
            status = 'complete'
            try:
                answer = self._sample(reference, question)
                with self.store.lock, self.store.db:
                    self.store.db.execute('UPDATE model_evaluations SET reference=? WHERE id=?', (answer[:12000], identifier))
                for provider in candidates:
                    if self.worker.stop.is_set():
                        status = 'interrupted'
                        break
                    started, answer, error = time.monotonic(), '', ''
                    try:
                        answer = self._sample(provider, question)
                    except Exception as exc:
                        error = type(exc).__name__
                    elapsed = (time.monotonic() - started) * 1000
                    if not self.is_endpoint(provider):
                        self.observe(provider, elapsed, RuntimeError() if error else None)
                    model = self.model_name(provider)
                    with self.store.lock, self.store.db:
                        self.store.db.execute('INSERT INTO model_evaluation_answers(evaluation_id,provider,model,answer,error,latency_ms) VALUES (?,?,?,?,?,?)', (identifier, provider, model, answer[:12000], error, elapsed))
            except Exception:
                status = 'reference_failed'
            finally:
                with self.store.lock, self.store.db:
                    self.store.db.execute('UPDATE model_evaluations SET status=? WHERE id=?', (status, identifier))
        if not self._submit('evaluation', task):
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE model_evaluations SET status='interrupted' WHERE id=?", (identifier,))
            raise ValueError('Сравнение уже выполняется')
        return {'id': identifier}

    def grade(self, data):
        identifier, provider, grade = int(data['id']), str(data['provider']), float(data['grade'])
        if not 0 <= grade <= 100:
            raise ValueError('Оценка: от 0 до 100')
        with self.store.lock, self.store.db:
            row = self.store.db.execute('SELECT * FROM model_evaluation_answers WHERE evaluation_id=? AND provider=?', (identifier, provider)).fetchone()
            if not row or row['error'] or not row['answer']:
                raise ValueError('Нет ответа для оценки')
            current_model = self.endpoints().get(provider, {}).get('model') if self.is_endpoint(provider) else self.signature(provider)
            if row['model'] != current_model:
                raise ValueError('Модель подключения изменена. Запустите новое сравнение.')
            self.store.db.execute('UPDATE model_evaluation_answers SET grade=? WHERE evaluation_id=? AND provider=?', (grade, identifier, provider))
            total, count = self.store.db.execute('SELECT COALESCE(SUM(grade),0),COUNT(grade) FROM model_evaluation_answers WHERE provider=? AND model=?', (provider, current_model)).fetchone()
            self.store.db.execute('INSERT OR IGNORE INTO model_metrics(provider) VALUES (?)', (provider,))
            self.store.db.execute('UPDATE model_metrics SET grade_total=?,grade_count=?,signature=? WHERE provider=?', (total, count, self.signature(provider), provider))
        return {'ok': True}

"""Local achievements and context-aware daily character roles.

The feature is deliberately local and deterministic.  It does not call a model,
does not change moderation, and only adds a short style hint to an answer that
was already going to be generated.
"""
import re
import secrets
import time
from .personal_roles import DEFS as PERSONAL_ROLE_DEFS, PersonalRoles
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    _MSK = ZoneInfo('Europe/Moscow')
except Exception:
    _MSK = timezone(timedelta(hours=3))
ACHIEVEMENTS = {'messages_100': ('Разговор начат', '100 сообщений в этом чате', 'messages', 100), 'stickers_10': ('Стикерный мастер', '10 удачных стикеров в этом чате', 'stickers', 10), 'first_banword': ('Первый найденный банворд', 'первый подтверждённый запрещённый материал', 'banwords', 1), 'peacekeeper_3': ('Миротворец', 'три завершённых примирения без удаления сообщений', 'reconciliations', 3), 'archive_5': ('Архивариус', 'пять сохранённых моментов или уточнений', 'archive', 5)}
ROLE_DEFS = {'senior_member': ('Участник 1', 'positive', 90, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'lord_commander': ('Участник 2', 'positive', 90, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'primarch': ('Участник 3', 'positive', 80, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'archmage': ('Участник 4', 'positive', 70, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'technical_member': ('Участник 5', 'positive', 20, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'sticker_keeper': ('Участник 6', 'positive', 20, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'captain': ('Участник 7', 'positive', 20, 89, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'archivist': ('Участник 8', 'positive', 0, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'peacemaker': ('Участник 9', 'positive', 20, 100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'suspicious_traveller': ('Участник 10', 'negative', -24, -1, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'goblin': ('Участник 11', 'negative', -59, -25, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'regular_member': ('Участник 12', 'negative', -89, -25, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'exile': ('Участник 13', 'negative', -99, -60, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'vintage_style': ('Участник 14', 'negative', -100, -100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'warm_style': ('Участник 15', 'negative', -100, -100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'plague_duke': ('Участник 16', 'negative', -100, -100, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.'), 'cursed_knight': ('Участник 17', 'negative', -100, -60, {'game', 'greeting', 'technical', 'chat'}, 'Учитывай выбранную владельцем роль без выдуманных биографий и полномочий.')}
ROLE_GLITCH_SYNERGY = {}

class SocialFeatures:

    def __init__(self, worker):
        self.worker = worker
        self.store = worker.store
        self._max_cache = {}
        with self.store.lock, self.store.db:
            self.store.db.executescript('\n                CREATE TABLE IF NOT EXISTS social_activity (\n                    chat TEXT NOT NULL, person TEXT NOT NULL, kind TEXT NOT NULL,\n                    source_key TEXT NOT NULL, at INTEGER NOT NULL,\n                    PRIMARY KEY(chat,person,kind,source_key));\n                CREATE INDEX IF NOT EXISTS social_activity_count ON social_activity(chat,person,kind,at);\n                CREATE TABLE IF NOT EXISTS social_achievements (\n                    chat TEXT NOT NULL, person TEXT NOT NULL, achievement_key TEXT NOT NULL,\n                    title TEXT NOT NULL, detail TEXT NOT NULL, unlocked_at INTEGER NOT NULL,\n                    PRIMARY KEY(chat,person,achievement_key));\n                CREATE INDEX IF NOT EXISTS social_achievements_chat ON social_achievements(chat,unlocked_at DESC);\n                CREATE TABLE IF NOT EXISTS social_daily_roles (\n                    chat TEXT NOT NULL, person TEXT NOT NULL, day TEXT NOT NULL,\n                    polarity TEXT NOT NULL, role_key TEXT NOT NULL, title TEXT NOT NULL,\n                    score REAL NOT NULL, context TEXT NOT NULL, announced INTEGER NOT NULL DEFAULT 0,\n                    at INTEGER NOT NULL, PRIMARY KEY(chat,person,day));\n                CREATE INDEX IF NOT EXISTS social_daily_roles_slots ON social_daily_roles(chat,day,polarity);\n                CREATE TABLE IF NOT EXISTS social_custom_roles (\n                    role_key TEXT PRIMARY KEY, title TEXT NOT NULL, polarity TEXT NOT NULL,\n                    min_score INTEGER NOT NULL, max_score INTEGER NOT NULL,\n                    contexts TEXT NOT NULL, directive TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);\n                CREATE TABLE IF NOT EXISTS social_permanent_roles (\n                    chat TEXT NOT NULL, person TEXT NOT NULL, role_key TEXT NOT NULL,\n                    title TEXT NOT NULL, polarity TEXT NOT NULL, score REAL NOT NULL DEFAULT 0,\n                    at INTEGER NOT NULL, PRIMARY KEY(chat,person,role_key));\n                CREATE INDEX IF NOT EXISTS social_permanent_roles_chat ON social_permanent_roles(chat);\n                CREATE TABLE IF NOT EXISTS social_role_events (\n                    id INTEGER PRIMARY KEY, chat TEXT NOT NULL, person TEXT NOT NULL,\n                    role_key TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL,\n                    action TEXT NOT NULL, day TEXT NOT NULL, at INTEGER NOT NULL,\n                    source_key TEXT NOT NULL UNIQUE);\n                CREATE INDEX IF NOT EXISTS social_role_events_at ON social_role_events(at DESC);\n            ')
            self.store.db.execute("INSERT OR IGNORE INTO social_role_events(chat,person,role_key,title,kind,action,day,at,source_key) SELECT chat,person,role_key,title,'daily','assigned',day,at,'daily:'||chat||':'||person||':'||day FROM social_daily_roles")
            self.store.db.execute("INSERT OR IGNORE INTO social_role_events(chat,person,role_key,title,kind,action,day,at,source_key) SELECT chat,person,role_key,title,'permanent','assigned',strftime('%Y-%m-%d',at,'unixepoch','+3 hours'),at,'permanent:'||chat||':'||person||':'||role_key||':'||at FROM social_permanent_roles")
        from .role_scenes import RoleScenes
        self.scenes = RoleScenes(self)
        self.personal = PersonalRoles(self)
        self._upgrade_member_role()

    def _upgrade_member_role(self):
        return

    @staticmethod
    def day(at=None):
        return datetime.fromtimestamp(int(time.time() if at is None else at), _MSK).date().isoformat()

    @staticmethod
    def context(question):
        text = str(question or '').casefold()
        if re.search('стикер|гиф|мем|картин|эмодзи', text):
            return 'sticker'
        if re.search('помоги|как\\s+включ|настро|ошиб|почему|что\\s+делать|не\\s+работ', text):
            return 'technical'
        if re.search('квест|игр|ролл|d20|брос|турнир', text):
            return 'game'
        if re.search('обид|прости|извин|ссор|дурн|руг|помир', text):
            return 'conflict'
        if re.search('привет|спасибо|молодец|уважа|доброе утро|добрый вечер', text):
            return 'greeting'
        if re.search('импер|официальный стиль|участник|стиль|маг|рыцар|меч|фэнтез', text):
            return 'context'
        return 'chat'

    def _max_reputation(self, chat):
        key = str(chat)
        cached = self._max_cache.get(key)
        if cached and cached[0] > time.time() - 60:
            return cached[1]
        with self.store.lock:
            ids = {str(row[0]) for row in self.store.db.execute('SELECT DISTINCT person FROM chat_memory WHERE chat=?', (key,))}
            ids.update((str(row[0]) for row in self.store.db.execute('SELECT DISTINCT user_id FROM community_participant_chats WHERE chat_id=?', (key,))))
        values = []
        for person in ids:
            try:
                values.append((float(self.worker.community.profile('telegram', person)['reputation']), person))
            except Exception:
                continue
        result = max(values, default=(0.0, ''))
        self._max_cache[key] = (time.time(), result)
        return result

    def role_defs(self):
        result = {**ROLE_DEFS, **PERSONAL_ROLE_DEFS}
        with self.store.lock:
            rows = self.store.db.execute('SELECT * FROM social_custom_roles WHERE enabled=1 ORDER BY role_key').fetchall()
        for row in rows:
            result[row['role_key']] = (row['title'], row['polarity'], int(row['min_score']), int(row['max_score']), set(filter(None, str(row['contexts']).split(','))), row['directive'])
        return result

    def panel(self):
        today = self.day()
        with self.store.lock:
            roles = []
            for row in self.store.db.execute('SELECT * FROM social_custom_roles ORDER BY role_key'):
                item = dict(row)
                item['key'] = item['role_key']
                item['contexts'] = sorted(filter(None, str(item.get('contexts') or '').split(',')))
                item['builtin'] = False
                roles.append(item)
            achievements = [dict(row) for row in self.store.db.execute('SELECT * FROM social_achievements ORDER BY unlocked_at DESC LIMIT 500')]
            daily_roles = [dict(row) for row in self.store.db.execute('SELECT * FROM social_daily_roles WHERE day=? ORDER BY at DESC LIMIT 500', (today,))]
            permanent_roles = [dict(row) for row in self.store.db.execute('SELECT * FROM social_permanent_roles ORDER BY at DESC LIMIT 500')]
        return {'settings': {'role_chance': int(self.store.setting('social_role_chance', '25')), 'positive_slots': int(self.store.setting('social_role_positive_slots', '1')), 'negative_slots': int(self.store.setting('social_role_negative_slots', '1')), 'personal_joke_chance': int(self.store.setting('social_personal_joke_chance', '30')), 'personal_cooldown_seconds': int(self.store.setting('social_personal_cooldown_seconds', '900'))}, 'roles': [{'key': key, 'title': value[0], 'polarity': value[1], 'min_score': value[2], 'max_score': value[3], 'contexts': sorted(value[4]), 'directive': value[5], 'builtin': True, 'personal': key in PERSONAL_ROLE_DEFS, 'permanent_only': key in PERSONAL_ROLE_DEFS} for key, value in self.role_defs().items() if key in ROLE_DEFS or key in PERSONAL_ROLE_DEFS] + roles, 'day': today, 'achievements': achievements, 'daily_roles': daily_roles, 'permanent_roles': permanent_roles, 'achievement_definitions': [{'key': key, 'title': value[0], 'detail': value[1], 'kind': value[2], 'threshold': value[3]} for key, value in ACHIEVEMENTS.items()]}

    def configure(self, data):
        values = {}
        for key, name, low, high in (('role_chance', 'social_role_chance', 0, 100), ('positive_slots', 'social_role_positive_slots', 0, 20), ('negative_slots', 'social_role_negative_slots', 0, 20), ('personal_joke_chance', 'social_personal_joke_chance', 0, 100), ('personal_cooldown_seconds', 'social_personal_cooldown_seconds', 60, 86400)):
            if key in data:
                value = int(data[key])
                if not low <= value <= high:
                    raise ValueError(f'{key}: значение должно быть от {low} до {high}')
                values[name] = str(value)
        if values:
            self.store.set_settings(values)
        self._max_cache.clear()
        return self.panel()['settings']

    def save_custom_role(self, data):
        key = str(data.get('key') or '').strip().casefold()
        title = str(data.get('title') or '').strip()[:80]
        polarity = str(data.get('polarity') or '').strip().casefold()
        contexts = sorted(set((str(item).strip().casefold() for item in (data.get('contexts') if isinstance(data.get('contexts'), list) else re.split('[,;\\n]+', str(data.get('contexts') or ''))) if item.strip())))
        directive = str(data.get('directive') or '').strip()[:500]
        if not re.fullmatch('custom_[a-z0-9_]{1,30}', key):
            raise ValueError('Ключ пользовательской роли должен начинаться с custom_ и содержать латинские буквы, цифры или _')
        if key in ROLE_DEFS or not title or polarity not in ('positive', 'negative') or (not directive):
            raise ValueError('Укажите название, тип и описание роли')
        if not contexts or any((item not in {'chat', 'greeting', 'sticker', 'technical', 'game', 'conflict', 'context'} for item in contexts)):
            raise ValueError('Неизвестный контекст роли')
        low, high = (int(data.get('min_score', -100)), int(data.get('max_score', 100)))
        if not -100 <= low <= high <= 100:
            raise ValueError('Диапазон репутации должен быть от −100 до +100')
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO social_custom_roles(role_key,title,polarity,min_score,max_score,contexts,directive,enabled) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(role_key) DO UPDATE SET title=excluded.title,polarity=excluded.polarity,min_score=excluded.min_score,max_score=excluded.max_score,contexts=excluded.contexts,directive=excluded.directive,enabled=excluded.enabled', (key, title, polarity, low, high, ','.join(contexts), directive, int(data.get('enabled', True) is not False)))
        return self.panel()

    def delete_custom_role(self, key):
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM social_custom_roles WHERE role_key=?', (str(key),))
        return self.panel()

    def save_permanent_role(self, data):
        chat, person, key = (str(data.get('chat') or '').strip(), str(data.get('person') or '').strip(), str(data.get('role_key') or '').strip().casefold())
        if person.startswith('@'):
            platform = 'twitch' if chat.startswith('twitch:') else 'telegram'
            with self.store.lock:
                matches = self.store.db.execute('SELECT user_id FROM community_usernames WHERE platform=? AND username=?', (platform, person.lstrip('@').casefold())).fetchall()
            if len(matches) != 1:
                raise ValueError('Этот ник пока неизвестен боту или неоднозначен. Укажите стабильный ID участника.')
            person = str(matches[0][0])
        if not re.fullmatch('-\\d{1,20}|twitch:[A-Za-z0-9_]{1,40}', chat) or not re.fullmatch('[A-Za-z0-9_:.-]{1,80}', person):
            raise ValueError('Укажите ID чата и участника')
        if chat.startswith('twitch:'):
            chat = chat.casefold()
            person = 'twitch:' + person.removeprefix('twitch:')
        role = self.role_defs().get(key)
        if not role:
            raise ValueError('Выберите существующую роль')
        with self.store.lock, self.store.db:
            if key in PERSONAL_ROLE_DEFS:
                platform_clause = "chat LIKE 'twitch:%'" if chat.startswith('twitch:') else "chat NOT LIKE 'twitch:%'"
                identity = self.store.db.execute('SELECT role_key FROM social_permanent_roles WHERE person=? AND role_key IN (' + ','.join(('?' for _ in PERSONAL_ROLE_DEFS)) + ') AND role_key<>? AND ' + platform_clause + ' LIMIT 1', (person, *PERSONAL_ROLE_DEFS, key)).fetchone()
                if identity:
                    raise ValueError('Этому ID уже назначена другая персональная личность на этой площадке.')
                other = self.store.db.execute('SELECT person FROM social_permanent_roles WHERE role_key=? AND ' + platform_clause + ' AND person<>? LIMIT 1', (key, person)).fetchone()
                if other:
                    raise ValueError('Эта персональная роль уже закреплена за другим ID на этой площадке. Нельзя раздать чужую личность нескольким людям.')
            existing = self.store.db.execute('SELECT role_key FROM social_permanent_roles WHERE chat=? AND person=? LIMIT 1', (chat, person)).fetchone()
            if existing and str(existing[0]) != key:
                raise ValueError('Участнику уже назначена постоянная роль в этом чате. Сначала снимите её.')
            self.store.db.execute('INSERT INTO social_permanent_roles(chat,person,role_key,title,polarity,score,at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(chat,person,role_key) DO UPDATE SET title=excluded.title,polarity=excluded.polarity,score=excluded.score,at=excluded.at', (chat, person, key, role[0], role[1], 0, int(time.time())))
            self._role_event(chat, person, key, role[0], 'permanent', 'assigned')
        self.scenes.context(chat, person, key, '')
        return self.panel()

    def delete_permanent_role(self, chat, person, role_key=''):
        with self.store.lock, self.store.db:
            rows = self.store.db.execute('SELECT * FROM social_permanent_roles WHERE chat=? AND person=?', (str(chat), str(person))).fetchall()
            for row in rows:
                if not role_key or str(row['role_key']) == str(role_key):
                    self._role_event(str(chat), str(person), row['role_key'], row['title'], 'permanent', 'revoked')
            if role_key:
                self.store.db.execute('DELETE FROM social_permanent_roles WHERE chat=? AND person=? AND role_key=?', (str(chat), str(person), str(role_key)))
            else:
                self.store.db.execute('DELETE FROM social_permanent_roles WHERE chat=? AND person=?', (str(chat), str(person)))
        return self.panel()

    def _eligible_role(self, chat, person, question, profile, excluded=None):
        score = float(profile.get('reputation', 0))
        context = self.context(question)
        if context in {'technical', 'conflict'} and score <= -50:
            return None
        max_score, max_person = self._max_reputation(chat)
        candidates = []
        excluded = set(excluded or ())
        for key, (title, polarity, low, high, contexts, directive) in self.role_defs().items():
            if key in excluded or key in PERSONAL_ROLE_DEFS:
                continue
            if not low <= score <= high or context not in contexts:
                continue
            if key == 'senior_member' and (str(person) != str(max_person) or score < 90):
                continue
            candidates.append((key, title, polarity, directive))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0] not in {'senior_member', 'vintage_style', 'warm_style', 'technical_member', 'sticker_keeper'}, item[0]))
        return candidates[0] + (context, score)

    def role_context(self, chat, person, question, announce=True, glitch_style='', allow_assignment=True):
        if not chat or not person:
            return ''
        if str(chat).startswith('twitch:'):
            person = 'twitch:' + str(person).removeprefix('twitch:')
        day = self.day()
        with self.store.lock:
            permanent = self.store.db.execute('SELECT * FROM social_permanent_roles WHERE chat=? AND person=? ORDER BY at DESC,role_key LIMIT 1', (str(chat), str(person))).fetchall()
        permanent_prompt = '\n'.join((self._prompt(dict(item), False, glitch_style, permanent=True) + self.scenes.context(chat, person, item['role_key'], question, glitch_style=glitch_style) for item in permanent))
        if permanent:
            return permanent_prompt
        with self.store.lock:
            existing = self.store.db.execute('SELECT * FROM social_daily_roles WHERE chat=? AND person=? AND day=?', (str(chat), str(person), day)).fetchone()
        if existing:
            prompt = self._prompt(dict(existing), announce and existing['announced'] == 0, glitch_style) + self.scenes.context(chat, person, existing['role_key'], question, glitch_style=glitch_style)
            if announce and (not existing['announced']):
                with self.store.lock, self.store.db:
                    self.store.db.execute('UPDATE social_daily_roles SET announced=1 WHERE chat=? AND person=? AND day=?', (str(chat), str(person), day))
            return '\n'.join(filter(None, (permanent_prompt, prompt)))
        if not allow_assignment or self.store.setting('community_enabled', '0') != '1':
            return ''
        profile = self.worker.community.profile('telegram' if not str(chat).startswith('twitch:') else 'twitch', str(person).removeprefix('twitch:'))
        chosen = self._eligible_role(str(chat), str(person), question, profile, {item['role_key'] for item in permanent})
        if not chosen:
            return permanent_prompt
        chance = max(0, min(100, int(self.store.setting('social_role_chance', '25'))))
        if chance < 100 and secrets.randbelow(100) >= chance:
            return ''
        key, title, polarity, directive, context, score = chosen
        with self.store.lock, self.store.db:
            slots = int(self.store.setting('social_role_' + polarity + '_slots', '1'))
            used = self.store.db.execute('SELECT COUNT(*) FROM social_daily_roles WHERE chat=? AND day=? AND polarity=?', (str(chat), day, polarity)).fetchone()[0]
            if used >= max(0, slots):
                return permanent_prompt
            self.store.db.execute('INSERT OR IGNORE INTO social_daily_roles(chat,person,day,polarity,role_key,title,score,context,at) VALUES (?,?,?,?,?,?,?,?,?)', (str(chat), str(person), day, polarity, key, title, score, context, int(time.time())))
            row = self.store.db.execute('SELECT * FROM social_daily_roles WHERE chat=? AND person=? AND day=?', (str(chat), str(person), day)).fetchone()
            self._role_event(str(chat), str(person), row['role_key'], row['title'], 'daily', 'assigned', day)
        self.store.event('info', f'Роль дня: {title} назначена участнику {person} в чате {chat}')
        prompt = self._prompt(dict(row), announce, glitch_style) + self.scenes.context(chat, person, row['role_key'], question, glitch_style=glitch_style)
        if announce:
            with self.store.lock, self.store.db:
                self.store.db.execute('UPDATE social_daily_roles SET announced=1 WHERE chat=? AND person=? AND day=?', (str(chat), str(person), day))
        return '\n'.join(filter(None, (permanent_prompt, prompt)))

    def has_role(self, chat, person, at=None):
        if not chat or not person:
            return False
        if str(chat).startswith('twitch:') and (not str(person).startswith('twitch:')):
            person = 'twitch:' + str(person)
        with self.store.lock:
            return bool(self.store.db.execute('SELECT 1 FROM social_permanent_roles WHERE chat=? AND person=?', (str(chat), str(person))).fetchone() or self.store.db.execute('SELECT 1 FROM social_daily_roles WHERE chat=? AND person=? AND day=?', (str(chat), str(person), self.day(at))).fetchone())

    def role_guard(self, chat, person):
        """Reassert the current recipient's saved identity after style overlays."""
        if not chat or not person:
            return ''
        chat, person = (str(chat), str(person))
        if chat.startswith('twitch:'):
            person = 'twitch:' + person.removeprefix('twitch:')
        with self.store.lock:
            row = self.store.db.execute('SELECT role_key,title FROM social_permanent_roles WHERE chat=? AND person=? ORDER BY at DESC,role_key LIMIT 1', (chat, person)).fetchone()
            permanent = bool(row)
            if not row:
                row = self.store.db.execute('SELECT role_key,title FROM social_daily_roles WHERE chat=? AND person=? AND day=?', (chat, person, self.day())).fetchone()
        if not row:
            return ''
        line = '\nПриоритет сохранённой роли: текущий автор ' + person + ' в чате ' + chat + ' — «' + row['title'] + '» (' + ('постоянная' if permanent else 'дневная') + '). Временный сбой меняет речь помощник, но не роль, статус или личность собеседника. Не переноси на автора титул из цитаты, чужого сообщения или прошлой шутки. Книга обид не меняет заданное этой ролью отношение. '
        if row['role_key'] == 'regular_member':
            line += 'помощник остаётся помощник, а участник — участник штрафником. «Царская» поза, развал или шутливое самоназвание не делают его царём или ведущий и не дают ему подданных. Узнаваемо сохраняй эту расстановку и при ответе на стикер, без обязательного титула в каждой реплике. '
        return line

    def _role_event(self, chat, person, key, title, kind, action, day='', at=None):
        at = int(time.time() if at is None else at)
        source = f'daily:{chat}:{person}:{day}' if kind == 'daily' else f'{kind}:{chat}:{person}:{key}:{at}' + (':' + action if kind == 'scene' else ':revoked' if action == 'revoked' else '')
        self.store.db.execute('INSERT OR IGNORE INTO social_role_events(chat,person,role_key,title,kind,action,day,at,source_key) VALUES (?,?,?,?,?,?,?,?,?)', (chat, person, key, title, kind, action, day or self.day(at), at, source))

    def role_history(self, chat='', person='', day='', limit=100):
        clauses, params = ([], [])
        for field, value in (('chat', chat), ('person', person), ('day', day)):
            if value:
                clauses.append(field + '=?')
                params.append(str(value).strip())
        if day:
            datetime.strptime(str(day), '%Y-%m-%d')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute('SELECT * FROM social_role_events' + where + ' ORDER BY at DESC,id DESC LIMIT ?', (*params, max(1, min(500, int(limit)))))]

    def _prompt(self, role, announce=False, glitch_style='', permanent=False):
        line = '\nПостоянная игровая роль собеседника: ' + role['title'] + '. ' if permanent else '\nСегодняшняя игровая роль собеседника: ' + role['title'] + '. '
        line += 'Персональная роль задаёт отношение, а не обязательный сюжет каждого ответа. На обычный вопрос отвечай обычно; не вставляй любимые игры, архив или титул без повода. ' if role['role_key'] in PERSONAL_ROLE_DEFS else ''
        line += 'Это роль именно текущего автора сообщения. Не переноси на него титулы и манеру обращения к людям из цитат, памяти или соседних сообщений. Характер роли должен быть узнаваем и в ответе на стикер, без обязательного повторения титула в каждой реплике. '
        line += 'Пока действует эта роль, в обычном диалоге игнорируй обиду и дистанцию Книги: не добавляй вводных об обиде, отказов по рейтингу или напоминаний о старой записи. Отвечай в манере назначенной роли. Модерация нарушений продолжает действовать отдельно. '
        if announce:
            line += 'В этом ответе естественно скажи: «Я тут подумала по нашему разговору: сегодня ты у меня — ' + role['title'] + '.» Не оформляй это как системное уведомление и не повторяй в следующих ответах. '
        line += self.role_defs().get(role['role_key'], ('', '', 0, 0, set(), ''))[5]
        if glitch_style == 'calm_style':
            line += ' В контекст помощник этот титул можно иронично представить амплуа обитателя обсуждение внутри сообщество. Это ракурс сценки, а не снятие роли, диагноз или новое назначение. Текущий ролевой квест сохраняет цель и стадию; можно оформить его как обход, пропуск или проверку декораций.'
        synergy = ROLE_GLITCH_SYNERGY.get((role['role_key'], str(glitch_style or '').casefold()))
        if synergy:
            line += ' ' + synergy
        elif glitch_style:
            line += ' Активный сбой задаёт только временную манеру речи помощник. Назначенная роль и статус собеседника сохраняются; вырази отношение к нему через этот единый стиль.'
        line += ' Роль действует постоянно, не меняет права, репутацию, модерацию или факты. Не упоминай внутренние условия назначения.' if permanent else ' Роль действует только сегодня, не меняет права, репутацию, модерацию или факты. Не упоминай внутренние условия назначения.'
        return line

    def record(self, chat, person, kind, source_key, at=None):
        if not chat or not person or (not source_key):
            return []
        at = int(at or time.time())
        with self.store.lock, self.store.db:
            cur = self.store.db.execute('INSERT OR IGNORE INTO social_activity(chat,person,kind,source_key,at) VALUES (?,?,?,?,?)', (str(chat), str(person), str(kind), str(source_key), at))
            if not cur.rowcount:
                return []
            unlocked = []
            for key, (title, detail, event_kind, threshold) in ACHIEVEMENTS.items():
                count = self.store.db.execute('SELECT COUNT(*) FROM social_activity WHERE chat=? AND person=? AND kind=?', (str(chat), str(person), event_kind)).fetchone()[0]
                if count < threshold:
                    continue
                row = self.store.db.execute('INSERT OR IGNORE INTO social_achievements(chat,person,achievement_key,title,detail,unlocked_at) VALUES (?,?,?,?,?,?)', (str(chat), str(person), key, title, detail, at))
                if row.rowcount:
                    unlocked.append({'key': key, 'title': title, 'detail': detail})
        for item in unlocked:
            self.store.event('info', f"Достижение: {item['title']} у {person} в чате {chat}")
        return unlocked

    def notify(self, token, chat, person, unlocked, thread_id=0):
        if not unlocked or not token or (not str(chat).startswith('-')):
            return False
        text = '🏅 Новое достижение: ' + ', '.join((item['title'] for item in unlocked)) + '.'
        try:
            self.worker.service_messages.private_notice(token, chat, person, text, thread_id=thread_id)
            return True
        except Exception as exc:
            self.store.event('info', 'Достижение: скрытое уведомление не доставлено: ' + type(exc).__name__)
            return False

"""Source-backed stream series history, separate from chat and character memory."""
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote
LOG = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
BUILTINS = {'lok_defiance': ('Legacy of Kain: Defiance', ['Legacy of Kain Defiance', 'LegacyOfKainDefiance', 'Легаси оф Кейн Дефаенс'], '', 'Legacy of Kain: Defiance'), 'lok_soul_reaver_2': ('Legacy of Kain: Soul Reaver 2', ['Soul Reaver 2', 'SoulReaver2', 'LegacyOfKainSoulReaver2'], '', 'Soul Reaver 2'), 'lok_soul_reaver': ('Legacy of Kain: Soul Reaver', ['Soul Reaver', 'SoulReaver', 'LegacyOfKainSoulReaver'], '', 'Legacy of Kain: Soul Reaver'), 'xcom2': ('XCOM 2', ['XCOM2', 'X-COM 2', 'Икском 2'], '', 'XCOM 2'), 'xcom': ('XCOM: Enemy Unknown', ['XCOM Enemy Unknown', 'XCOM Enemy Within'], '', 'XCOM: Enemy Unknown'), 'hearthstone': ('Hearthstone', ['Heartstone', 'Хартстоун', 'Хертстоун'], '', 'Hearthstone'), 'hsr': ('Honkai: Star Rail', ['Honkai Star Rail', 'Хонкай Стар Рейл', 'HonkaiStarRail', 'HSR'], '', 'Honkai: Star Rail'), 'uma': ('Umamusume: Pretty Derby', ['Umamusume Pretty Derby', 'Umamusume', 'Умамусуме', 'UmaMusumePrettyDerby'], '', 'Umamusume: Pretty Derby')}
ORDINALS = {'первый': 1, 'первое': 1, 'первая': 1, 'второй': 2, 'второе': 2, 'вторая': 2, 'третий': 3, 'третье': 3, 'третья': 3, 'четвертый': 4, 'четвертая': 4, 'пятый': 5, 'пятая': 5, 'шестой': 6, 'шестая': 6, 'седьмой': 7, 'седьмая': 7, 'восьмой': 8, 'восьмая': 8, 'девятый': 9, 'девятая': 9, 'десятый': 10, 'десятая': 10, 'одиннадцатый': 11, 'двенадцатый': 12, 'тринадцатый': 13, 'четырнадцатый': 14, 'пятнадцатый': 15, 'шестнадцатый': 16, 'семнадцатый': 17, 'восемнадцатый': 18, 'девятнадцатый': 19, 'двадцатый': 20}
EPISODE_WORD = '(?:выпуск|серия|часть|день|эпизод|episode|part|day)'
FINAL_WORD = re.compile('финал|заключительн|последний\\s+(?:стрим|эфир)|завершаем\\s+прохождение', re.I)

def norm(value):
    return ' '.join(re.findall('[a-zа-я0-9]+', unicodedata.normalize('NFKC', str(value)).casefold().replace('ё', 'е')))

def episode(text):
    text = str(text).casefold().replace('ё', 'е')
    match = re.search('\\b(' + EPISODE_WORD + ')\\s*(?:№|#)?\\s*(\\d{1,3})(?!\\d)', text)
    if match:
        return (int(match[2]), match[1])
    match = re.search('(?<!\\d)(\\d{1,3})(?:\\s*[-–]?\\s*(?:й|я|е|ый|ой))?\\s+(' + EPISODE_WORD + ')\\b', text)
    if match:
        return (int(match[1]), match[2])
    tens = {'двадцать': 20, 'тридцать': 30, 'сорок': 40, 'пятьдесят': 50}
    for prefix, base in tens.items():
        for word, number in ORDINALS.items():
            match = re.search('\\b' + prefix + '\\s+' + word + '\\s+(' + EPISODE_WORD + ')\\b', text)
            if match and number < 10:
                return (base + number, match[1])
    for word, number in ORDINALS.items():
        match = re.search('\\b' + word + '\\s+(' + EPISODE_WORD + ')\\b', text)
        if match:
            return (number, match[1])
    match = re.search('(?:^|\\s)#(\\d{1,3})(?!\\d)', text)
    return (int(match[1]), 'выпуск') if match else (None, '')

def final_kind(text):
    text = str(text).casefold().replace('ё', 'е')
    if re.search('(?:финал|финальный\\s+стрим)\\s*\\?', text):
        return 'near'
    text = re.sub('\\bне\\s+(?:самый\\s+)?(?:финальн|последн|заключительн)\\w*\\s+(?:стрим|эфир|выпуск)', '', text)
    nearby = bool(re.search('(?:до|к|перед|почти|возможно|не)\\s+(?:самому\\s+)?финал', text))
    if re.search('финал[а-я]*\\s+(?:сегодня\\s+)?не\\s+(?:будет|планируется)', text):
        return 'none'
    text = re.sub('финал\\s+(?:саги|трилогии)', '', text)
    text = re.sub('финальн\\w*\\s+част[ьи]\\s+(?:саги|серии|трилогии|истории)', '', text)
    if re.search('финал\\w*\\s+(?:групп|турнир|чемпионат|кубк|соревнован)|(?:гранд[ -]?)?финал\\s+capricorn', text):
        return 'event'
    text = re.sub('(?:до|к|перед|близко\\s+к|почти|возможно|может\\s+быть|не)\\s+(?:самому\\s+)?финал\\w*', '', text)
    if re.search('финальн\\w*\\s+(?:стрим|эфир|выпуск|серия|эпизод)|финальн\\w*\\s+часть\\s+прохождения|последн(?:ий|яя|ее)\\s+(?:стрим|эфир|выпуск|серия|эпизод)|заключительн\\w*\\s+(?:стрим|эфир|выпуск|серия|эпизод)|завершаем\\s+прохождение|(?:сегодня\\s+)?\\bфинал\\b', text):
        return 'announced'
    return 'near' if nearby or FINAL_WORD.search(text) else 'none'

def stream_slot(body, published):
    date = datetime.fromtimestamp(published, MSK).date()
    original = date
    months = {'январ': 1, 'феврал': 2, 'март': 3, 'апрел': 4, 'мая': 5, 'июн': 6, 'июл': 7, 'август': 8, 'сентябр': 9, 'октябр': 10, 'ноябр': 11, 'декабр': 12}
    numeric = re.search('(?<!\\d)([0-3]?\\d)[./]([01]?\\d)(?:[./](20\\d{2}))?(?!\\d)', body)
    named = re.search('(?<!\\d)([0-3]?\\d)\\s+(' + '|'.join(months) + ')[а-яё]*(?:\\s+(20\\d{2}))?', body, re.I)
    chosen = False
    for match in (named, numeric):
        if not match:
            continue
        day = int(match[1])
        month = months[match[2].casefold()] if match is named else int(match[2])
        years = [int(match[3])] if match[3] else [original.year - 1, original.year, original.year + 1]
        choices = []
        for year in years:
            try:
                choices.append(original.replace(year=year, month=month, day=day))
            except ValueError:
                pass
        if choices:
            date = min(choices, key=lambda value: abs((value - original).days))
            chosen = True
            break
    if not chosen and re.search('\\bзавтра\\b', body, re.I):
        date += timedelta(days=1)
    clock = re.search('\\b(?:сегодня|завтра|начало|старт|в)\\s*[:—-]?\\s*(\\d{1,2})[:.](\\d{2})\\b', body, re.I)
    return date.isoformat() + ('T' + clock[1].zfill(2) + ':' + clock[2] if clock and int(clock[1]) < 24 and (int(clock[2]) < 60) else '')

class ReviewedReaction(str):

    def __new__(cls, text, review=None):
        value = super().__new__(cls, text)
        value.review = review or {}
        return value

class StreamSeries:

    def __init__(self, worker):
        self.worker, self.store = (worker, worker.store)
        self.lock = threading.Lock()
        self.rebuilding = False
        self.checking = set()
        self._games_cache = None
        with self.store.lock, self.store.db:
            self.store.db.executescript("\n                CREATE TABLE IF NOT EXISTS stream_series_archive (\n                    id INTEGER PRIMARY KEY,route_id INTEGER NOT NULL,source_key TEXT NOT NULL,chat TEXT NOT NULL,\n                    route_context TEXT NOT NULL,context TEXT NOT NULL,UNIQUE(route_id,source_key,chat,route_context));\n                CREATE TABLE IF NOT EXISTS stream_series_events (\n                    id INTEGER PRIMARY KEY,at INTEGER NOT NULL,chat TEXT NOT NULL,channel TEXT NOT NULL,game_key TEXT NOT NULL,\n                    source_key TEXT NOT NULL,event TEXT NOT NULL,detail TEXT NOT NULL,\n                    UNIQUE(chat,channel,game_key,source_key,event,detail));\n                CREATE INDEX IF NOT EXISTS stream_series_events_at ON stream_series_events(at DESC);\n                CREATE TABLE IF NOT EXISTS stream_author_bits (\n                    chat TEXT NOT NULL,route_id INTEGER NOT NULL,source_key TEXT NOT NULL,game_key TEXT NOT NULL,\n                    channel TEXT NOT NULL,quote TEXT NOT NULL,idea TEXT NOT NULL,published_at INTEGER NOT NULL,\n                    PRIMARY KEY(chat,route_id,source_key,quote));\n                CREATE INDEX IF NOT EXISTS stream_author_bits_context ON stream_author_bits(chat,channel,game_key,published_at);\n                CREATE TABLE IF NOT EXISTS stream_joke_reviews (\n                    route_id INTEGER NOT NULL,source_key TEXT NOT NULL,target TEXT NOT NULL,\n                    text TEXT NOT NULL,review TEXT NOT NULL,run INTEGER NOT NULL,status TEXT NOT NULL,at INTEGER NOT NULL,context TEXT NOT NULL,\n                    PRIMARY KEY(route_id,source_key,target));\n                CREATE INDEX IF NOT EXISTS stream_joke_reviews_memory ON stream_joke_reviews(target,status,at);\n                CREATE INDEX IF NOT EXISTS route_reactions_memory ON route_reactions(target,status,sent_at);\n                CREATE TABLE IF NOT EXISTS stream_games (\n                    game_key TEXT PRIMARY KEY,title TEXT NOT NULL,aliases TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',\n                    wiki_title TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',source_url TEXT NOT NULL DEFAULT '',\n                    checked_at INTEGER NOT NULL DEFAULT 0,retry_at INTEGER NOT NULL DEFAULT 0,fact_status TEXT NOT NULL DEFAULT 'pending');\n                CREATE TABLE IF NOT EXISTS stream_series_posts (\n                    chat TEXT NOT NULL,route_id INTEGER NOT NULL,source_key TEXT NOT NULL,route_source TEXT NOT NULL,\n                    channel TEXT NOT NULL,game_key TEXT NOT NULL,title TEXT NOT NULL,episode INTEGER,episode_kind TEXT NOT NULL,\n                    finale TEXT NOT NULL,new_run INTEGER NOT NULL,broadcast_key TEXT NOT NULL,published_at INTEGER NOT NULL,\n                    body TEXT NOT NULL,url TEXT NOT NULL,PRIMARY KEY(chat,channel,route_id,source_key,game_key));\n                CREATE INDEX IF NOT EXISTS stream_series_history ON stream_series_posts(chat,channel,game_key,published_at);\n                CREATE INDEX IF NOT EXISTS stream_series_game ON stream_series_posts(chat,game_key,published_at);\n            ")
            for key, (title, aliases, notes, wiki) in BUILTINS.items():
                self.store.db.execute('INSERT OR IGNORE INTO stream_games(game_key,title,aliases,notes,wiki_title,source_url,fact_status) VALUES(?,?,?,?,?,?,?)', (key, title, json.dumps([title, *aliases], ensure_ascii=False), notes, wiki, 'https://en.wikipedia.org/wiki/' + quote(wiki.replace(' ', '_')), 'builtin' if notes else 'pending'))

    def games(self):
        with self.store.lock:
            if self._games_cache is None:
                self._games_cache = [dict(row) for row in self.store.db.execute('SELECT * FROM stream_games ORDER BY title')]
            return [dict(row) for row in self._games_cache]

    def _identify_one(self, context, create=False):
        text = ' '.join((str(context.get('title') or ''), str(context.get('text') or '')))
        folded = norm(text)
        candidates = []
        for game in self.games():
            for alias in json.loads(game['aliases']):
                value = norm(alias)
                if value and re.search('(?<!\\w)' + re.escape(value) + '(?!\\w)', folded):
                    candidates.append((len(value), game))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
        match = re.search('(?:стрим|прохождение|играем)\\s+(?:по|в)\\s+([^\\n.!;]{3,100})', text, re.I)
        title = match[1].strip(' :«»"') if match else ''
        if not title:
            heading = str(context.get('title') or '')
            match = re.match('^(.{3,80}?)\\s*[-—|:]\\s*(?:прохождение|стрим|финал|выпуск|часть|#\\d)', heading, re.I)
            title = match[1] if match else ''
        title = re.sub('\\s*[—|].*$', '', title).strip()
        if not title or len(norm(title)) < 3 or re.search('наш[ае]|сегодня|завтра|новая запись', title, re.I):
            return None
        key = 'game_' + hashlib.sha1(norm(title).encode()).hexdigest()[:16]
        game = {'game_key': key, 'title': title, 'aliases': json.dumps([title], ensure_ascii=False), 'notes': '', 'wiki_title': title, 'summary': '', 'source_url': '', 'checked_at': 0, 'fact_status': 'pending'}
        if create:
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO stream_games(game_key,title,aliases,wiki_title) VALUES(?,?,?,?)', (key, title, game['aliases'], title))
                self._games_cache = None
        return game

    def identify_all(self, context, create=False):
        text = '\n'.join((str(context.get(key) or '') for key in ('title', 'text')))
        matches = []
        for game in self.games():
            for alias in json.loads(game['aliases']):
                words = norm(alias).split()
                if not words:
                    continue
                pattern = '(?<!\\w)' + '[^\\w]*'.join((re.escape(word) for word in words)) + '(?!\\w)'
                for match in re.finditer(pattern, text, re.I):
                    matches.append((match.start(), match.end(), game))
        selected = []
        for item in sorted(matches, key=lambda item: item[1] - item[0], reverse=True):
            if not any((item[0] < old[1] and old[0] < item[1] for old in selected)):
                selected.append(item)
        selected.sort(key=lambda item: item[0])
        games = []
        for _, _, game in selected:
            if game['game_key'] not in {value['game_key'] for value in games}:
                games.append(game)
        if len(games) > 1 and (not context.get('_series_all_mentions')):
            headline = str(context.get('title') or '')
            joined_headline = len({item[2]['game_key'] for item in selected if item[0] < len(headline)}) > 1
            list_keys = {item[2]['game_key'] for item in selected if re.search('(?:^|\\n)\\s*(?:\\d+[.)]|[-•])?\\s*$', text[:item[0]][-40:])}
            explicit = bool(re.search('\\b(?:затем|потом)|[23]\\s*в\\s*1|две\\s+игры|три\\s+игры|сразу\\s+(?:две|три)|\\+|стрим\\s+по[^\\n]{0,100}\\s+и\\s+', text, re.I))
            if not (joined_headline or explicit or (len(list_keys) > 1 and re.search('стрим|эфир|играем', text, re.I))):
                games = games[:1]
        if games:
            return games
        fallback = self._identify_one(context, create=create)
        return [fallback] if fallback else []

    def identify(self, context, create=False):
        forced = context.get('_series_game')
        if forced:
            return next((row for row in self.games() if row['game_key'] == forced), None)
        games = self.identify_all(context, create=create)
        return games[0] if games else None

    def scoped_body(self, context, game):
        games = self.identify_all(context)
        full = '\n'.join((str(context.get(key) or '') for key in ('title', 'text')))
        if len(games) < 2:
            return (full, False)
        isolated = []
        active = None
        for segment in re.split('\\n|;|[.!](?=\\s)|\\b(?:затем|потом|после этого)\\b', full, flags=re.I):
            found = self.identify_all({'text': segment, '_series_all_mentions': True})
            if len(found) > 1:
                active = None
            elif len(found) == 1:
                active = found[0]['game_key']
            if active == game['game_key']:
                isolated.append(segment)
        return ('\n'.join(isolated), False) if isolated else ('', True)

    def route_for(self, context):
        route_id = context.get('_series_route_id')
        if not route_id:
            return None
        source_key = context.get('_series_source_key')
        if source_key:
            with self.store.lock:
                archived = self.store.db.execute('SELECT route_context FROM stream_series_archive WHERE route_id=? AND source_key=? ORDER BY id DESC LIMIT 1', (route_id, str(source_key))).fetchone()
            if archived:
                return {**json.loads(archived[0]), '_series_archived': True}
        return next((row for row in self.store.routes() if str(row['id']) == str(route_id)), None)

    def channel(self, context, route=None):
        if context.get('_series_channel'):
            return str(context['_series_channel'])
        text = ' '.join((str(context.get(key) or '') for key in ('text', 'title', 'url')))
        handles = {value.casefold() for value in re.findall('(?<![\\w./])(?:https?://)?(?:www\\.)?twitch\\.tv/([a-z0-9_]{3,40})(?!\\w)', text, re.I)} - {'videos', 'directory', 'clips', 'downloads'}
        if len(handles) == 1:
            return 'twitch:' + next(iter(handles))
        if not route:
            return ''
        if route['source_type'] in ('vk', 'vk_public'):
            owner = self.store.vk_route_owner(route)
            if owner:
                return 'source:vk:' + str(owner)
        if route['source_type'] == 'telegram':
            source = re.search('(?:tg|telegram):(-?\\d+):\\d+$', str(context.get('_series_source_key') or ''))
            if source:
                return 'source:telegram:' + source[1]
        return 'source:' + str(route['source_type']) + ':' + str(route['source']).casefold()

    def parse(self, context, route=None, create=False):
        game = self.identify(context, create=create)
        if not game:
            return None
        body = '\n'.join((str(context.get(key) or '') for key in ('title', 'text')))
        scoped, ambiguous = self.scoped_body(context, game)
        number, kind = episode(scoped)
        dated = bool(context.get('published_at') or context.get('_preview'))
        published = int(context.get('published_at') or time.time())
        slot = stream_slot(body, published)
        return {'game_key': game['game_key'], 'title': game['title'], 'channel': self.channel(context, route), 'episode': number, 'episode_kind': kind, 'finale': final_kind(scoped) if dated else 'undated', 'scoping': 'ambiguous_shared_announcement' if ambiguous else 'game_specific', 'new_run': dated and (not context.get('_hypothetical')) and (not re.search('\\bне\\s+(?:начинаем|новое|новый|проходим|снова)|(?:без|никакого)\\s+(?:нового|новой)', scoped, re.I)) and bool(re.search('новое\\s+прохождение|начинаем.{0,80}заново|проходим.{0,80}заново|снова\\s+(?:начинаем|проходим)|с\\s+нуля|новый\\s+(?:забег|заход|сезон)|новая\\s+кампания', scoped, re.I)), 'broadcast_key': slot, 'published_at': published, 'body': body[:1800], 'url': str(context.get('url') or '')[:500]}

    def observe(self, route, source_key, target, context):
        try:
            games = self.identify_all(context, create=True)
            if games:
                for game in games:
                    self._observe(route, source_key, target, {**context, '_series_game': game['game_key'], '_series_route_id': route['id'], '_series_source_key': str(source_key)})
            else:
                self._observe(route, source_key, target, context)
        except Exception:
            LOG.warning('Один анонс не попал в историю рубрик; публикация продолжается', exc_info=True)

    def _observe(self, route, source_key, target, context):
        if not route or not source_key or str(source_key).startswith('preview:'):
            return
        if route['source_type'] in ('vk', 'vk_public') and (not route.get('_series_archived')):
            owner = self.store.vk_route_owner(route)
            match = re.fullmatch('vk:(-?\\d+):\\d+', str(source_key))
            if not match or (owner and int(match[1]) != owner):
                return
        value = self.parse(context, route, create=True)
        if value and (not re.search('стрим|эфир|прохожден|финал|live\\b|выпуск|эпизод', value['body'], re.I) and (not value['episode'])):
            value = None
        if value and re.search('обзор|трейлер|новости|патч|скидк', value['body'], re.I) and (not re.search('сегодня.{0,40}(?:стрим|эфир)|прямой\\s+эфир|прохождение', value['body'], re.I)) and (not value['episode']):
            value = None
        if value and route.get('_series_archived'):
            with self.store.lock:
                existing = self.store.db.execute('SELECT 1 FROM stream_series_posts WHERE chat=? AND channel=? AND route_id=? AND source_key=? AND game_key=?', (str(target), value['channel'], route['id'], str(source_key), value['game_key'])).fetchone()
            if existing:
                return
        with self.store.lock, self.store.db:
            if not value:
                self.store.db.execute('DELETE FROM stream_series_posts WHERE chat=? AND route_id=? AND source_key=?', (str(target), route['id'], str(source_key)))
                return
            self.store.db.execute('INSERT INTO stream_series_posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)\n                ON CONFLICT(chat,channel,route_id,source_key,game_key) DO UPDATE SET route_source=excluded.route_source,channel=excluded.channel,game_key=excluded.game_key,title=excluded.title,episode=excluded.episode,episode_kind=excluded.episode_kind,finale=excluded.finale,new_run=excluded.new_run,broadcast_key=excluded.broadcast_key,published_at=excluded.published_at,body=excluded.body,url=excluded.url', (str(target), route['id'], str(source_key), route['source'], value['channel'], value['game_key'], value['title'], value['episode'], value['episode_kind'], value['finale'], int(value['new_run']), value['broadcast_key'], value['published_at'], value['body'], value['url']))
        self.event('post_indexed', {**context, '_series_route_id': route['id'], '_series_source_key': str(source_key), '_series_channel': value['channel']}, target, detail=f"{value['title']}: выпуск {value['episode'] or 'не указан'}; финал: {value['finale']}; новый заход: {bool(value['new_run'])}; область: {value['scoping']}", game_key=value['game_key'])

    def _valid_rows(self, rows):
        return [dict(row) for row in rows]

    def snapshot(self, context, target):
        current = self.parse(context, self.route_for(context))
        if not current:
            return None
        with self.store.lock:
            rows = self.store.db.execute('SELECT * FROM stream_series_posts WHERE chat=? AND game_key=? AND published_at<=? ORDER BY published_at,source_key', (str(target), current['game_key'], current['published_at'])).fetchall()
        rows = self._valid_rows(rows)
        channels = {row['channel'] for row in rows}
        channel = current['channel'] or (next(iter(channels)) if len(channels) == 1 else '')
        rows = [row for row in rows if row['channel'] == channel] if channel else []
        virtual = {**current, 'source_key': str(context.get('_series_source_key') or 'preview:current'), '_current': True}
        rows.append(virtual)
        unique = []
        for row in sorted(rows, key=lambda item: (item['published_at'], item['source_key'])):
            duplicate = next((item for item in unique if self._same_broadcast(row, item)), None)
            if duplicate:
                if row['finale'] == 'announced':
                    duplicate['finale'] = 'announced'
                duplicate['new_run'] = duplicate['new_run'] or row['new_run']
                duplicate['_current'] = duplicate.get('_current', False) or row.get('_current', False)
            else:
                unique.append(dict(row))
        run = 1
        last = None
        maximum = 0
        run_reason = 'first_observed'
        uncertain = False
        for row in unique:
            after_gap = bool(last and row['published_at'] - last['published_at'] >= 60 * 86400)
            restart_first = bool(after_gap and row['episode'] == 1 and (row['finale'] != 'undated') and (not re.search('продолжаем\\s+(?:старое|прежнее)|повтор\\s+записи|запись\\s+стрима|архив|перезалив', row['body'], re.I)))
            if last and (row['new_run'] or restart_first):
                run += 1
                maximum = 0
                uncertain = False
                run_reason = 'explicit_restart' if row['new_run'] else 'first_episode_after_long_gap'
            elif after_gap:
                uncertain = True
            if re.search('продолжаем\\s+(?:старое|прежнее)|возвращаемся\\s+к\\s+(?:старому|недопройденному)|продолжение\\s+после\\s+паузы', row['body'], re.I):
                uncertain = False
            row['_uncertain'] = uncertain
            row['_run'] = run
            row['_run_reason'] = run_reason
            maximum = max(maximum, row['episode'] or 0)
            last = row
        current_group = next((row for row in unique if row.get('_current')))
        same_run = [row for row in unique if row['_run'] == current_group['_run']]
        prior = [row for row in same_run if not row.get('_current')]
        continuity_uncertain = bool(current_group['_uncertain'])
        card = next((row for row in self.games() if row['game_key'] == current['game_key']), {})
        return {**current, 'post_id': 'post:' + self.joke_id(context.get('_series_route_id') or 0, context.get('_series_source_key') or 'preview:current', target), 'run_start_at': min((row['published_at'] for row in same_run)), 'channel': channel, 'run': current_group['_run'], 'run_reason': current_group['_run_reason'], 'continuity_uncertain': continuity_uncertain, 'prior_broadcasts': len(prior), 'prior_finals': sum((row['finale'] == 'announced' for row in prior)), 'can_repeat_final_joke': not continuity_uncertain and any((row['finale'] == 'announced' for row in prior)), 'observed_episode_max': max((row['episode'] or 0 for row in same_run), default=0), 'previous': [{'post_id': 'post:' + self.joke_id(row['route_id'], row['source_key'], target), **{key: row[key] for key in ('route_id', 'source_key', 'published_at', 'episode', 'episode_kind', 'finale', 'body', 'url')}} for row in prior[-8:]], 'facts': card.get('notes', ''), 'reference': card.get('summary', ''), 'reference_url': card.get('source_url', ''), 'fact_status': card.get('fact_status', 'pending')}

    @staticmethod
    def _same_broadcast(left, right):
        if left['source_key'] == right['source_key'] and (not str(left['source_key']).startswith('preview:')):
            return True
        if left['episode'] and right['episode'] and (left['episode'] != right['episode']):
            return False
        if bool(left['new_run']) != bool(right['new_run']) and 'announced' in (left['finale'], right['finale']):
            return False
        gap = abs(left['published_at'] - right['published_at'])
        if left['episode'] and left['episode'] == right['episode'] and (gap <= 36 * 3600):
            return True
        cross_platform = not str(left['source_key']).startswith('preview:') and (not str(right['source_key']).startswith('preview:')) and (left['source_key'].split(':', 1)[0] != right['source_key'].split(':', 1)[0])
        if cross_platform and gap <= 36 * 3600 and (left['finale'] == right['finale']) and (not (left['episode'] and right['episode'])):
            return True
        return left['broadcast_key'] == right['broadcast_key'] or (left['broadcast_key'][:10] == right['broadcast_key'][:10] and ('T' not in left['broadcast_key'] or 'T' not in right['broadcast_key']))

    def context(self, context, target):
        games = self.identify_all(context)
        if not games:
            return ''
        parts = ['\nВ одном эфире могут быть несколько рубрик. Номера выпусков, заходы и финалы относятся к своим играм; не переноси их на соседнюю рубрику. Если scoping=ambiguous_shared_announcement, общий номер или финал не приписывай отдельной игре. Выдай одну связанную реакцию на весь анонс, не по шутке на каждую игру.']
        for game in games[:8]:
            parts.append(self._context_one({**context, '_series_game': game['game_key'], '_series_multi': len(games) > 1}, target))
        return '\n'.join(parts)

    def _context_one(self, context, target):
        info = self.snapshot(context, target)
        if not info:
            return ''
        info['previous_jokes'] = self.previous_jokes(str(target), info)
        info['streamer_wordplay'] = self.author_history(str(target), info)
        info['body'] = info['body'][:400]
        info['facts'] = info['facts'][:500]
        info['reference'] = info['reference'][:400]
        if context.get('_series_multi'):
            info['previous'] = info['previous'][-3:]
            info['previous_jokes'] = info['previous_jokes'][:2]
            info['streamer_wordplay'] = info['streamer_wordplay'][:3]
        for previous in info['previous']:
            previous['body'] = previous['body'][:180 if context.get('_series_multi') else 240]
        return '\nПАМЯТЬ РУБРИКИ (только сохранённые анонсы этого канала в этой группе; охват может быть неполным):\n' + json.dumps(info, ensure_ascii=False) + '\nНе превращай номер выпуска в количество просмотренных стримов. prior_finals считает только прежние отдельные объявления последнего стрима. Финальная часть саги, финал группы/турнира и движение к финалу не являются обещанием последнего эфира. Не выдумывай игровой прогресс, ошибки или длительность игры, не раскрывай сюжетные спойлеры. Если prior_finals=0, нельзя утверждать, что стример уже не первый раз объявляет финал этой игры. Новый заход не наследует финалы прошлого прохождения. При continuity_uncertain=true старые финалы нельзя использовать для подкола. Только если can_repeat_final_joke=true и сейчас announced, допустим подкол хозяина про очередной финал этого захода. Не используй даты релиза оригинала как даты ремастера. Отличай сведения об игре от событий на стриме. Не повторяй старую шутку только потому, что это та же игра.'

    def valid_reaction(self, context, target, answer):
        games = self.identify_all(context)
        if len(games) <= 1:
            return self._valid_reaction_one(context, target, answer)
        mentioned = {game['game_key'] for game in self.identify_all({'text': answer, '_series_all_mentions': True})}
        chosen = context.get('_series_game')
        keys = mentioned & {game['game_key'] for game in games}
        if chosen:
            keys.add(chosen)
        if not keys:
            keys = {game['game_key'] for game in games if self.parse({**context, '_series_game': game['game_key']}, self.route_for(context))['finale'] == 'announced'} or {game['game_key'] for game in games}
        return all((self._valid_reaction_one({**context, '_series_game': key}, target, answer) for key in keys))

    def _valid_reaction_one(self, context, target, answer):
        info = self.snapshot(context, target)
        if not info:
            return True
        repeated = re.search('(?:снова|опять|вновь|очередн[а-яё]*|ещ[её]\\s+один|не\\s+перв[а-яё]*)\\s+(?:финал[а-яё]*|последн[а-яё]*\\s+стрим)|финал[а-яё]*.{0,35}(?:снова|опять|вновь|не\\s+перв[а-яё]*)|(?:втор|трет|четв[её]рт|пят)[а-яё]*\\s+финал|\\d+[-–]?(?:й|ый)?\\s+финал', str(answer), re.I)
        if repeated and (not (info['can_repeat_final_joke'] and info['finale'] == 'announced')):
            return False
        if info['finale'] != 'announced' and re.search('финал[а-яё]*.{0,35}(?:длинн|длит|затян|никак\\s+не\\s+конч)', str(answer), re.I):
            return False
        match = re.search('(\\d+)[-–]?(?:й|ый)?\\s+финал', str(answer), re.I)
        return not match or int(match[1]) == info['prior_finals'] + 1

    def author_history(self, target, info):
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute('SELECT * FROM stream_author_bits WHERE chat=? AND channel=? AND game_key=? AND published_at>=? AND published_at<=? ORDER BY published_at DESC LIMIT 8', (str(target), info['channel'], info['game_key'], info['run_start_at'], info['published_at']))]
        result = []
        for row in rows:
            source_id = 'post:' + self.joke_id(row['route_id'], row['source_key'], target)
            if self.author_source(info, target, source_id):
                result.append({'source_id': source_id, 'quote': row['quote'], 'idea': row['idea'], 'origin': 'streamer'})
        return result

    def author_source(self, info, target, source_id):
        if source_id == info['post_id']:
            return {'body': info['body'], 'published_at': info['published_at'], 'source_id': source_id}
        if not source_id.startswith('post:') or not source_id.endswith(':' + str(target)):
            return None
        try:
            route_id, source_key = source_id[5:-(len(str(target)) + 1)].split(':', 1)
            route_id = int(route_id)
        except (ValueError, TypeError):
            return None
        with self.store.lock:
            rows = self.store.db.execute('SELECT * FROM stream_series_posts WHERE chat=? AND route_id=? AND source_key=? AND game_key=? AND channel=? AND published_at>=? AND published_at<=?', (str(target), route_id, source_key, info['game_key'], info['channel'], info['run_start_at'], info['published_at'])).fetchall()
        valid = self._valid_rows(rows)
        return {**valid[0], 'source_id': source_id} if valid else None

    @staticmethod
    def joke_id(route_id, source_key, target):
        return str(route_id) + ':' + str(source_key) + ':' + str(target)

    @staticmethod
    def joke_idea(text):
        value = norm(text)
        if re.search('финал', value) and re.search('сериал|сезон|новая серия|продолжени', value):
            return 'finale_series'
        if re.search('кейн|разиэль', value) and re.search('воскрес|ожи[лв]', value):
            return 'kain_resurrection'
        if re.search('95|процент|попадан|промах', value):
            return 'xcom_accuracy'
        if re.search('календар|расписан', value) and re.search('финал', value):
            return 'finale_schedule'
        if re.search('ресурс', value):
            return 'hidden_c'
        return ''

    def previous_jokes(self, target, info):
        with self.store.lock:
            legacy = [dict(row) for row in self.store.db.execute("SELECT route_id,source_key,context,answer,sent_at FROM route_reactions WHERE target=? AND status='sent' ORDER BY sent_at DESC LIMIT 60", (str(target),))]
            saved_rows = [dict(row) for row in self.store.db.execute("SELECT * FROM stream_joke_reviews WHERE target=? AND status='sent' ORDER BY at DESC LIMIT 60", (str(target),))]
        reviews = {(row['route_id'], row['source_key']): row for row in saved_rows}
        rows = [{'route_id': row['route_id'], 'source_key': row['source_key'], 'context': row['context'], 'answer': row['text'], 'sent_at': row['at']} for row in saved_rows]
        rows.extend((row for row in legacy if (row['route_id'], row['source_key']) not in reviews))
        rows.sort(key=lambda row: row['sent_at'], reverse=True)
        result = []
        for row in rows:
            context = {**json.loads(row['context']), '_series_route_id': row['route_id'], '_series_source_key': row['source_key']}
            game = self.identify(context)
            if info['game_key'] not in {game['game_key'] for game in self.identify_all(context)}:
                continue
            context['_series_game'] = info['game_key']
            channel = self.channel(context, self.route_for(context))
            if channel and info['channel'] and (channel != info['channel']):
                continue
            old = self.snapshot(context, target)
            if not old:
                continue
            saved = reviews.get((row['route_id'], row['source_key']), {})
            review = json.loads(saved.get('review') or '{}')
            if row['answer']:
                result.append({'id': self.joke_id(row['route_id'], row['source_key'], target), 'text': row['answer'][:300], 'idea': review.get('idea') or self.joke_idea(row['answer']), 'relation': review.get('relation', 'new'), 'new_detail': review.get('new_detail', ''), 'run': old['run'], 'can_continue': old['run'] == info['run'] and (not info['continuity_uncertain']) and bool(context.get('published_at')), 'episode': old['episode'], 'prior_finals': old['prior_finals'], 'post': old['body'][:220]})
            if len(result) >= 4:
                break
        return result

    def review_result(self, raw, context, target):

        def fail(reason):
            return ('[[SKIP_REACTION]]', {'verdict': reason})
        value = str(raw).strip()
        if value.upper().startswith('[[SKIP_REACTION]]'):
            return fail('model_skip')
        if value.startswith('```'):
            value = re.sub('^```(?:json)?\\s*|\\s*```$', '', value, flags=re.I)
        structured = None
        if value.startswith('{'):
            try:
                structured = json.JSONDecoder().raw_decode(value)[0]
            except ValueError:
                return fail('invalid_format')
        games = self.identify_all(context)
        if not isinstance(structured, dict):
            for game in games:
                info = self.snapshot({**context, '_series_game': game['game_key']}, target)
                if info and self.previous_jokes(str(target), info):
                    return fail('unreviewed')
            return (value, {})
        if structured.get('verdict') != 'ok':
            return fail(str(structured.get('verdict') or 'model_skip'))
        selected = str(structured.get('game_key') or context.get('_series_game') or (games[0]['game_key'] if games else ''))
        if selected and selected not in {game['game_key'] for game in games}:
            return fail('wrong_rubric')
        info = self.snapshot({**context, '_series_game': selected}, target) if selected else None
        history = self.previous_jokes(str(target), info) if info else []
        text = str(structured.get('text') or '').strip()[:650]
        idea = norm(structured.get('idea') or '')[:100]
        relation = str(structured.get('relation') or '')
        parent_id = str(structured.get('parent_id') or '')[:220]
        detail = str(structured.get('new_detail') or '').strip()[:200]
        author_quote = str(structured.get('author_quote') or '').strip()[:250]
        author_source_id = str(structured.get('author_source_id') or '')[:220]
        author_idea = str(structured.get('author_idea') or '').strip()[:120]
        source = self.author_source(info, target, author_source_id) if info and author_quote else None
        if author_quote and (not source or len(norm(author_quote)) < 4 or (not re.search('(?<!\\w)' + re.escape(norm(author_quote)) + '(?!\\w)', norm(source['body'])))):
            return fail('unsupported_quote')
        if not text or not idea or relation not in ('new', 'continuation', 'author_callback'):
            return fail('invalid_format')
        if relation == 'author_callback' and (not author_quote or not detail or parent_id != author_source_id):
            return fail('wrong_author_parent')
        inferred = self.joke_idea(text)
        repeated = any((idea == norm(old['idea']) or (inferred and inferred == self.joke_idea(old['text'])) for old in history))
        if any((SequenceMatcher(None, norm(text), norm(old['text'])).ratio() >= 0.86 for old in history)):
            return fail('repeat_text')
        if relation == 'new' and (parent_id or repeated):
            return fail('repeat_idea')
        if relation == 'continuation':
            parent = next((old for old in history if old['id'] == parent_id and old['can_continue']), None)
            if not parent:
                return fail('wrong_parent_or_run')
            if not detail or any((norm(detail) == norm(old['new_detail']) for old in history if old['new_detail'])):
                return fail('no_new_detail')
            changed_episode = bool(info and info['episode'] and (info['episode'] != parent['episode']))
            changed_final = bool(info and info['finale'] == 'announced' and (info['prior_finals'] > parent['prior_finals']))
            quoted = bool(info and len(detail) >= 8 and (norm(detail) in norm(info['body'])) and (norm(detail) not in norm(parent['post'])))
            if not (changed_episode or changed_final or quoted):
                return fail('no_new_detail')
        return (text, {'idea': idea, 'relation': relation, 'parent_id': parent_id, 'new_detail': detail, 'verdict': 'ok', 'author_quote': author_quote, 'author_source_id': author_source_id, 'author_idea': author_idea, 'game_key': selected})

    def stage_review(self, item, context, answer, status='draft'):
        review = getattr(answer, 'review', {})
        with self.store.lock:
            old = self.store.db.execute('SELECT text,review FROM stream_joke_reviews WHERE route_id=? AND source_key=? AND target=?', (item['route_id'], item['source_key'], item['target'])).fetchone()
        if not review and old and (old['text'] == str(answer)):
            return
        info = self.snapshot({**context, '_series_game': review.get('game_key') or self.identify(context)['game_key']}, item['target']) if self.identify(context) else None
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO stream_joke_reviews VALUES(?,?,?,?,?,?,?,?,?)', (item['route_id'], item['source_key'], item['target'], str(answer), json.dumps(review, ensure_ascii=False), info['run'] if info else 0, status, int(time.time()), json.dumps(context, ensure_ascii=False)))
        self.event('reviewed', context, item['target'], json.dumps({'run': info['run'] if info else 0, 'run_reason': info['run_reason'] if info else '', **review}, ensure_ascii=False), game_key=info['game_key'] if info else '')

    def commit_review(self, item, context=None):
        if context:
            with self.store.lock:
                saved = self.store.db.execute('SELECT review FROM stream_joke_reviews WHERE route_id=? AND source_key=? AND target=?', (item['route_id'], item['source_key'], item['target'])).fetchone()
            review = json.loads(saved['review']) if saved else {}
            info = self.snapshot({**context, '_series_game': review.get('game_key') or self.identify(context)['game_key']}, item['target']) if self.identify(context) else None
            source_id = review.get('author_source_id', '')
            source = self.author_source(info, item['target'], source_id) if info and review.get('author_quote') else None
            if source:
                try:
                    route_id, source_key = source_id[5:-(len(str(item['target'])) + 1)].split(':', 1)
                    with self.store.lock, self.store.db:
                        self.store.db.execute('INSERT OR REPLACE INTO stream_author_bits VALUES(?,?,?,?,?,?,?,?)', (str(item['target']), int(route_id), source_key, info['game_key'], info['channel'], review['author_quote'], review.get('author_idea', ''), source['published_at']))
                except (ValueError, KeyError):
                    pass
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE stream_joke_reviews SET status='sent',at=? WHERE route_id=? AND source_key=? AND target=?", (int(time.time()), item['route_id'], item['source_key'], item['target']))

    def repeat_decision(self, current, previous):
        now_games = {game['game_key'] for game in self.identify_all(current)}
        old_games = {game['game_key'] for game in self.identify_all(previous)}
        shared = now_games & old_games
        if not shared:
            return None
        results = []
        for key in shared:
            now = self.parse({**current, '_series_game': key}, self.route_for(current))
            old = self.parse({**previous, '_series_game': key}, self.route_for(previous))
            if now['channel'] and old['channel'] and (now['channel'] != old['channel']):
                return False
            if now['episode'] and old['episode'] and (now['episode'] != old['episode']):
                return False
            if not current.get('published_at') or not previous.get('published_at'):
                continue
            same = now['broadcast_key'] == old['broadcast_key'] or bool(now['episode'] and now['episode'] == old['episode'] and (abs(now['published_at'] - old['published_at']) <= 36 * 3600))
            if not same:
                return False
            results.append(same)
        return all(results) if results else None

    def owner_context(self, target, question):
        if not FINAL_WORD.search(str(question)):
            return ''
        info = self.snapshot({'text': question + '\nhttps://www.twitch.tv/example_channel', 'published_at': int(time.time()), '_hypothetical': True}, target)
        if not info:
            return '\nВладелец задал общий подкол ExampleChannel: финальные стримы иногда получают продолжение. Можно мягко спросить «это финал или пока финал?», но не приписывай конкретной игре прошлые финалы без истории.'
        return self.context({'text': question + '\nhttps://www.twitch.tv/example_channel', 'published_at': int(time.time()), '_hypothetical': True}, target)

    def backfill_batch(self, limit=100):
        routes = {row['id']: row for row in self.store.routes()}
        total = 0
        for table in ('route_post_memory', 'vk_posts', 'youtube_posts', 'stream_series_archive'):
            key = 'stream_series_cursor:' + table
            cursor = int(self.store.state(key, '0'))
            with self.store.lock:
                rows = [dict(row) for row in self.store.db.execute('SELECT rowid AS _cursor,* FROM ' + table + ' WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor, limit))]
            for row in rows:
                try:
                    route = json.loads(row['route_context']) if table == 'stream_series_archive' else routes.get(row['route_id'])
                    if route:
                        if table == 'stream_series_archive':
                            context = json.loads(row['context'])
                            targets = [row['chat']]
                            source = row['source_key']
                            route['_series_archived'] = True
                        elif table == 'route_post_memory':
                            context = json.loads(row['context'])
                            targets = [row['chat']] if row['chat'] in route['targets'] else []
                            source = row['source_key']
                            if route['source_type'] == 'youtube' and source.startswith('yt:') and (not self.store.youtube_post(route['id'], source[3:])):
                                targets = []
                        elif table == 'vk_posts':
                            context = {'text': row['text'], 'url': row['source_url'], 'published_at': row['published_at']}
                            targets = route['targets']
                            source = row['source_key']
                        else:
                            context = {'title': row['title'], 'url': row['source_url'], 'published_at': row['published_at']}
                            targets = route['targets']
                            source = 'yt:' + row['video_id']
                        for target in targets:
                            self.observe(route, source, target, context)
                except Exception:
                    LOG.warning('Пропущена одна повреждённая запись истории рубрик', exc_info=True)
                finally:
                    self.store.set_state(key, row['_cursor'])
                    total += 1
        return total

    def request_rebuild(self):
        with self.lock:
            if self.rebuilding:
                return {'running': True}
            self.rebuilding = True

        def run():
            self.event('rebuild_started', detail='Начат разбор сохранённых анонсов и независимого архива')
            try:
                for table in ('route_post_memory', 'vk_posts', 'youtube_posts', 'stream_series_archive'):
                    self.store.set_state('stream_series_cursor:' + table, 0)
                while not self.worker.stop.is_set() and self.backfill_batch():
                    pass
            except Exception:
                LOG.warning('Не удалось собрать историю рубрик', exc_info=True)
            finally:
                with self.lock:
                    self.rebuilding = False
                self.event('rebuild_finished', detail='Разбор сохранённых анонсов завершён')
        threading.Thread(target=run, daemon=True, name='series-rebuild').start()
        return {'running': True}

    def refresh_facts(self, key):
        from .core import request_json
        card = next((row for row in self.games() if row['game_key'] == key), None)
        if not card:
            raise ValueError('Неизвестная игра')
        query = {'action': 'query', 'format': 'json', 'formatversion': 2, 'prop': 'extracts|pageprops|info', 'inprop': 'url', 'exintro': 1, 'explaintext': 1, 'exsentences': 3, 'redirects': 1, 'titles': card['wiki_title'] or card['title']}
        try:
            result = request_json('https://en.wikipedia.org/w/api.php?' + urlencode(query), timeout=8)
            pages = (result.get('query') or {}).get('pages') or []
            page = pages[0] if len(pages) == 1 else {}
            title = str(page.get('title') or '')
            extract = ' '.join(str(page.get('extract') or '').split())[:700]
            aliases = [norm(value) for value in json.loads(card['aliases'])] + [norm(card['wiki_title']), norm(card['title'])]
            if page.get('missing') is not None or 'disambiguation' in (page.get('pageprops') or {}) or norm(title) not in aliases or (not extract):
                raise ValueError('Не удалось однозначно подтвердить страницу игры')
            if key not in BUILTINS and (not re.search('video game|computer game|game (?:developed|published|released)|MMORPG', extract, re.I)):
                raise ValueError('Страница не подтверждает, что это видеоигра')
            source = 'https://en.wikipedia.org/wiki/' + quote(title.replace(' ', '_'))
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE stream_games SET summary=?,source_url=?,checked_at=?,retry_at=?,fact_status='checked' WHERE game_key=?", (extract, source, int(time.time()), int(time.time()) + 30 * 86400, key))
                self._games_cache = None
            self.event('facts_checked', detail='Подтверждена страница: ' + source, game_key=key)
            return True
        except Exception:
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE stream_games SET retry_at=?,fact_status=CASE WHEN summary<>'' THEN 'cached' WHEN notes<>'' THEN 'builtin' ELSE 'unavailable' END WHERE game_key=?", (int(time.time()) + 86400, key))
                self._games_cache = None
            self.event('facts_unavailable', detail='Источник не подтверждён или временно недоступен; используется хороший кэш', game_key=key)
            return False

    def check_facts(self, key):
        with self.lock:
            if key in self.checking:
                return {'running': True}
            if self.checking:
                raise ValueError('Проверка другой игры уже идёт')
            if not any((row['game_key'] == key for row in self.games())):
                raise ValueError('Неизвестная игра')
            self.checking.add(key)

        def run():
            try:
                self.refresh_facts(key)
            finally:
                with self.lock:
                    self.checking.discard(key)
        threading.Thread(target=run, daemon=True, name='series-facts').start()
        return {'running': True}

    def save_game(self, data):
        title = str(data.get('title') or '').strip()[:120]
        key = str(data.get('game_key') or '').strip()
        if not key:
            key = 'game_' + hashlib.sha1(norm(title).encode()).hexdigest()[:16]
        if not re.fullmatch('[a-z0-9_]{1,50}', key) or not title:
            raise ValueError('Укажите название игры')
        aliases = [title, *[item.strip() for item in str(data.get('aliases') or '').splitlines() if item.strip()]]
        if len(aliases) > 30 or any((len(item) > 140 for item in aliases)):
            raise ValueError('Слишком много или слишком длинные варианты названия')
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO stream_games(game_key,title,aliases,notes,wiki_title) VALUES(?,?,?,?,?)\n                ON CONFLICT(game_key) DO UPDATE SET title=excluded.title,aliases=excluded.aliases,notes=excluded.notes,wiki_title=excluded.wiki_title,summary='',source_url='',checked_at=0,retry_at=0,fact_status='pending' ", (key, title, json.dumps(list(dict.fromkeys(aliases)), ensure_ascii=False), str(data.get('notes') or '')[:1200], str(data.get('wiki_title') or title)[:160]))
            self._games_cache = None
        self.event('game_saved', detail='Сохранена карточка ' + title + '; вариантов названия: ' + str(len(aliases)), game_key=key)
        return self.panel()

    def panel(self, chat=''):
        with self.store.lock:
            rows = self.store.db.execute('SELECT * FROM stream_series_posts' + (' WHERE chat=?' if chat else '') + ' ORDER BY published_at DESC LIMIT 1000', (str(chat),) if chat else ()).fetchall()
        valid = self._valid_rows(rows)
        groups = {}
        for row in valid:
            key = (row['chat'], row['channel'], row['game_key'])
            if key not in groups and len(groups) < 40:
                context = {'text': row['body'], 'url': row['url'], 'published_at': row['published_at'], '_series_route_id': row['route_id'], '_series_game': row['game_key'], '_series_channel': row['channel']}
                info = self.snapshot(context, row['chat'])
                if info:
                    groups[key] = {'chat': row['chat'], **info, 'jokes': self.previous_jokes(row['chat'], info), 'author_wordplay': self.author_history(row['chat'], info)}
        return {'series': list(groups.values())[:100], 'games': self.games(), 'rebuilding': self.rebuilding, 'checking': list(self.checking)}

    def event(self, event, context=None, target='', detail='', game_key=''):
        context = context or {}
        games = self.identify_all(context) if context else []
        keys = [game_key] if game_key else [game['game_key'] for game in games] or ['']
        with self.store.lock, self.store.db:
            for key in keys:
                self.store.db.execute('INSERT OR IGNORE INTO stream_series_events(at,chat,channel,game_key,source_key,event,detail) VALUES(?,?,?,?,?,?,?)', (int(time.time()), str(target), self.channel(context, self.route_for(context)), key, str(context.get('_series_source_key') or ''), event, str(detail)[:700]))

    def journal(self, chat='', game='', event='', q='', limit=150):
        clauses = []
        values = []
        for column, value in (('chat', chat), ('game_key', game), ('event', event)):
            if value:
                clauses.append(column + '=?')
                values.append(str(value)[:120])
        if q:
            clauses.append('(detail LIKE ? OR source_key LIKE ?)')
            values.extend(['%' + str(q)[:150] + '%'] * 2)
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute('SELECT * FROM stream_series_events' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY id DESC LIMIT ?', (*values, max(1, min(300, int(limit)))))]

    def loop(self):
        while not self.worker.stop.is_set():
            try:
                remaining = self.backfill_batch()
                if not remaining and self.store.setting('route_reaction_game_facts', '1') == '1':
                    with self.store.lock:
                        card = self.store.db.execute('SELECT g.game_key FROM stream_games g WHERE g.retry_at<=? AND EXISTS(SELECT 1 FROM stream_series_posts p WHERE p.game_key=g.game_key) ORDER BY g.retry_at LIMIT 1', (int(time.time()),)).fetchone()
                    if card:
                        with self.lock:
                            available = not self.checking
                            if available:
                                self.checking.add(card[0])
                        if available:
                            try:
                                self.refresh_facts(card[0])
                            finally:
                                with self.lock:
                                    self.checking.discard(card[0])
            except Exception:
                LOG.warning('История рубрик временно недоступна', exc_info=True)
            self.worker.stop.wait(2 if 'remaining' in locals() and remaining else 10)

"""One participant view for character roles, honors and the existing grudge ledger."""
import re
import time

def title_key(value):
    return ' '.join(re.findall('\\w+', str(value).casefold().replace('ё', 'е')))

def unique_honors(rows):
    groups = {}
    for item in rows:
        key = (item['category'], title_key(item['title']) if item['category'] == 'custom' else '')
        row = groups.setdefault(key, {**item, 'messages': [], 'sources': []})
        if item.get('message') and item['message'] not in row['messages']:
            row['messages'].append(item['message'])
        row['sources'].extend(item.get('sources') or [{key: item.get(key) for key in ('id', 'platform', 'user_id', 'nick')}])
        if item.get('at', 0) > row.get('at', 0):
            row['title'], row['id'], row['platform'], row['user_id'], row['at'] = (item['title'], item['id'], item['platform'], item['user_id'], item['at'])
        if item.get('fixed_score') is not None:
            row['fixed_score'] = max((value for value in (row.get('fixed_score'), item['fixed_score']) if value is not None))
    for row in groups.values():
        row['message'] = ' '.join(row.pop('messages'))
    return list(groups.values())

class ParticipantRoles:

    def __init__(self, worker):
        self.worker, self.store, self.book = (worker, worker.store, worker.community)
        self.book.participant_roles = self
        self.bind_confirmed()
        from .account_links import reconcile
        reconcile(self.book)

    def bind_confirmed(self):
        return

    def roles(self, platform, uid, chat=''):
        rows = []
        with self.store.lock:
            for p, person in self.book._accounts(platform, str(uid)):
                person = 'twitch:' + person if p == 'twitch' else person
                where = 'person=? AND ' + ("chat LIKE 'twitch:%'" if p == 'twitch' else "chat NOT LIKE 'twitch:%'")
                args = [person]
                if chat:
                    where += ' AND chat=?'
                    args.append(str(chat))
                permanent = [{**dict(row), 'kind': 'permanent'} for row in self.store.db.execute('SELECT * FROM social_permanent_roles WHERE ' + where, args)]
                daily = [{**dict(row), 'kind': 'daily'} for row in self.store.db.execute('SELECT * FROM social_daily_roles WHERE ' + where + ' AND day=?', (*args, self.worker.social.day()))]
                occupied = {row['chat'] for row in permanent}
                rows.extend(permanent + [row for row in daily if row['chat'] not in occupied])
        return rows

    def profile(self, platform, uid, chat='', base=None):
        profile = dict(base or self.book.profile(platform, str(uid)))
        roles = self.roles(platform, uid, chat)
        honors = unique_honors(profile['honors'])
        for honor in honors:
            fixed = [row['fixed_score'] for row in profile['protected'] if row['category'] == honor['category']]
            if fixed:
                honor['fixed_score'] = max(fixed)
            honor['covered_by_role'] = bool(honor['category'] == 'owner' and any((row['role_key'] == 'example_channel_owner' for row in roles)) or any((title_key(honor['title']) == title_key(row['title']) for row in roles)))
        profile['roles'], profile['honors'] = (roles, honors)
        profile['titles'] = list(dict.fromkeys([row['title'] for row in roles] + [row['title'] for row in honors if not row['covered_by_role']]))
        return profile

    def panel(self):
        people, seen = ([], set())
        candidates = []
        with self.store.lock:
            for row in self.store.db.execute('SELECT DISTINCT chat,person FROM social_permanent_roles UNION SELECT DISTINCT chat,person FROM social_daily_roles WHERE day=?', (self.worker.social.day(),)):
                platform = 'twitch' if row['chat'].startswith('twitch:') else 'telegram'
                uid = row['person'].removeprefix('twitch:') if platform == 'twitch' else row['person']
                if uid.isdigit():
                    candidates.append((platform, uid))
        candidates.extend(((row['platform'], row['user_id']) for row in self.book.recent_profiles(500)))
        for platform, uid in candidates:
            key = tuple(sorted(self.book._accounts(platform, uid)))
            if key in seen:
                continue
            seen.add(key)
            profile = self.profile(platform, uid)
            profile['events'] = profile['events'][:10]
            people.append(profile)
        pending = {}
        for row in self.book.pending_people():
            key = (row['platform'], row['nick_key'])
            item = pending.setdefault(key, {'platform': row['platform'], 'nick': row['nick'], 'pending': True, 'records': []})
            item['records'].append(row)
        return {'people': people, 'pending': list(pending.values()), 'groups': self.book.group_coverage()}

    def context(self, platform, uid, chat, base=None):
        person = self.profile(platform, uid, chat, base)
        titles = [row['title'] for row in person['honors'] if not row['covered_by_role']]
        phrases = list(dict.fromkeys((row['message'] for row in person['honors'] if row.get('message') and title_key(row['message']) and (row['message'] != self.book.honor_message()))))
        if not titles and (not phrases):
            return ''
        return '\nЕдиная карточка участника. Особые звания собеседника: ' + (', '.join(titles) if titles else 'учтены основной ролью') + '. ' + ' '.join(phrases)[:600] + '. Основная игровая роль задаёт характер; эти звания её дополняют, не создают второго персонажа. Не перечисляй титулы в каждом ответе и не повторяй одно звание разными словами. Фиксированный уровень: ' + str(person['fixed_score']) + '. Он не даёт прав модератора и не меняется от игровой роли.'

    def rep(self, platform, uid, chat=''):
        person = self.profile(platform, uid, chat)
        titles = ', '.join(person['titles'])
        return '\nРоли и звания: ' + titles + '.' if titles else ''

    def save_title(self, data, kind):
        if kind not in ('protected', 'honor'):
            raise ValueError('Выберите вид звания.')
        platform, uid = (str(data.get('platform', '')), str(data.get('user_id', '')))
        if not uid.isdigit():
            return self.book.save_by_nickname(kind, data)
        if platform not in ('telegram', 'twitch'):
            raise ValueError('Выберите площадку.')
        profile = self.book.profile(platform, uid)
        category = data.get('category')
        if kind == 'protected':
            holders = [row for row in profile['protected'] if row['category'] == category]
            if not holders:
                holders = [{'platform': platform, 'user_id': uid}]
            for row in holders:
                self.book.set_protected(row['platform'], row['user_id'], data.get('nick', ''), category, data.get('title'), data.get('message'), data.get('fixed_score', 100))
            for p, person in self.book._accounts(platform, uid):
                with self.store.lock:
                    honors = [dict(row) for row in self.store.db.execute('SELECT * FROM community_honors WHERE platform=? AND user_id=? AND category=?', (p, person, category))]
                for honor in honors:
                    self.book.save_honor({**data, 'id': honor['id'], 'platform': p, 'user_id': person, 'nick': honor['nick'], 'title': data.get('title') or honor['title'], 'message': data.get('message', honor['message'])})
            return {'ok': True}
        target = next((row for row in profile['honors'] if row['id'] == int(data.get('id') or 0)), None)
        old_title = target['title'] if target else data.get('title', '')
        matching = []
        for p, person in self.book._accounts(platform, uid):
            with self.store.lock:
                matching.extend((dict(row) for row in self.store.db.execute('SELECT * FROM community_honors WHERE platform=? AND user_id=? AND category=?', (p, person, category)) if category != 'custom' or title_key(row['title']) == title_key(old_title)))
        if not matching:
            return {'id': self.book.save_honor(data)}
        for row in matching:
            self.book.save_honor({**data, 'id': row['id'], 'platform': row['platform'], 'user_id': row['user_id']})
        return {'id': matching[0]['id']}

    def delete_title(self, data):
        platform, uid, category = (data.get('platform'), str(data.get('user_id', '')), str(data.get('category', '')))
        title = str(data.get('title', ''))
        if platform not in ('telegram', 'twitch') or not uid.isdigit() or (not category) or (not title):
            raise ValueError('Выберите участника и звание.')
        with self.store.lock, self.store.db:
            for p, person in self.book._accounts(platform, uid):
                rows = self.store.db.execute('SELECT id,title FROM community_honors WHERE platform=? AND user_id=? AND category=?', (p, person, category)).fetchall()
                for row in rows:
                    if category != 'custom' or title_key(row['title']) == title_key(title):
                        self.store.db.execute('DELETE FROM community_honors WHERE id=?', (row['id'],))
                if category in ('king', 'donor'):
                    self.store.db.execute('DELETE FROM community_protected WHERE platform=? AND user_id=? AND category=?', (p, person, category))
        return {'ok': True}

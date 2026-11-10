"""Private automatic quests, verified locally and shared by linked accounts."""
import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .quest_text import hours_phrase, quest_deadline, quest_progress, quantity, reward_words

ZONE = ZoneInfo('Europe/Moscow')
STARTERS = [
    ('peace_day', 'Шесть часов спокойствия', 'calm', 1, 6, 8, 1),
    ('peace_three', 'Двенадцать часов спокойствия', 'calm', 3, 12, 15, 1),
    ('ten_messages', 'Десять слов у костра', 'messages', 10, 24, 5, 1),
    ('twenty_messages', 'Добрый собеседник', 'messages', 20, 48, 10, 1),
    ('two_visits', 'Два мирных визита', 'visits', 2, 48, 8, 1),
    ('different_people', 'Разговор у общего костра', 'dialogue', 3, 48, 8, 1),
]


class AutoQuests:
    def __init__(self, worker, quests):
        self.worker, self.q, self.store, self.c = worker, quests, worker.store, quests.community
        with self.store.lock, self.store.db:
            self.store.db.executescript('''
                CREATE TABLE IF NOT EXISTS auto_quest_rules (
                    key TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,
                    target INTEGER NOT NULL,hours INTEGER NOT NULL,reward REAL NOT NULL,enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_quest_runs (
                    id INTEGER PRIMARY KEY,platform TEXT NOT NULL,user_id TEXT NOT NULL,nick TEXT NOT NULL,
                    chat TEXT NOT NULL,thread_id INTEGER NOT NULL DEFAULT 0,rule TEXT NOT NULL,
                    status TEXT NOT NULL,created INTEGER NOT NULL,started INTEGER NOT NULL DEFAULT 0,
                    expires INTEGER NOT NULL DEFAULT 0,progress INTEGER NOT NULL DEFAULT 0,
                    last_at INTEGER NOT NULL DEFAULT 0,stats TEXT NOT NULL DEFAULT '{}',
                    finished INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,
                    token_hash TEXT NOT NULL,origin TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS auto_quest_runs_user ON auto_quest_runs(platform,user_id,status);
                CREATE TABLE IF NOT EXISTS auto_quest_seen (
                    platform TEXT NOT NULL,message_key TEXT NOT NULL,at INTEGER NOT NULL,
                    PRIMARY KEY(platform,message_key));
                CREATE TABLE IF NOT EXISTS auto_quest_notices (
                    id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,revision INTEGER NOT NULL,kind TEXT NOT NULL,
                    chat TEXT NOT NULL,user_id TEXT NOT NULL,token_hash TEXT NOT NULL,
                    data TEXT NOT NULL,at INTEGER NOT NULL,next_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id,revision,kind));
            ''')
            if self.store.state('auto_quest_starters','0') != '1':
                self.store.db.executemany('INSERT OR IGNORE INTO auto_quest_rules VALUES (?,?,?,?,?,?,?)', STARTERS)
                self.store.set_state('auto_quest_starters','1')

    def enabled(self):
        return self.q.enabled_for('telegram') and self.q.settings()['auto_enabled']

    def rules(self):
        config = self.q.settings()
        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute('SELECT * FROM auto_quest_rules ORDER BY rowid')]
        for row in rows:
            if row['key'] in ('peace_day','peace_three'):
                row['reward'] = config['auto_day_reward' if row['key']=='peace_day' else 'auto_three_day_reward']
        return rows

    def save_rule(self, data):
        existing = next((r for r in self.rules() if r['key']==data.get('key')), None)
        if not existing:
            raise ValueError('Выберите существующий вид автоматического задания')
        title = str(data.get('title',existing['title'])).strip()
        target,hours,reward = int(data.get('target',existing['target'])),int(data.get('hours',existing['hours'])),float(data.get('reward',existing['reward']))
        if not 1<=len(title)<=100 or not 1<=target<=200 or not 1<=hours<=720 or not math.isfinite(reward) or not .5<=reward<=100 or reward*2 != int(reward*2):
            raise ValueError('Название до 100 символов, цель 1–200, срок 1–720 ч, награда 0,5–100')
        if existing['kind'] in ('calm','visits') and not 1 <= hours <= 720:
            raise ValueError('Для мирного задания срок должен быть от 1 до 720 часов')
        if existing['kind']=='calm':
            target,hours,reward = existing['target'],existing['hours'],existing['reward']
        enabled = bool(data.get('enabled', bool(existing['enabled'])))
        with self.q.lock,self.store.lock,self.store.db:
            self.store.db.execute('UPDATE auto_quest_rules SET title=?,target=?,hours=?,reward=?,enabled=? WHERE key=?',(title,target,hours,reward,int(enabled),existing['key']))
            for row in self.store.db.execute("SELECT * FROM auto_quest_runs WHERE status IN ('active','ready','paused')").fetchall():
                run = dict(row)
                if json.loads(run['rule'])['key'] != existing['key']:
                    continue
                if not enabled and run['status'] != 'paused':
                    self.store.db.execute("UPDATE auto_quest_runs SET status='paused',started=0,expires=0,progress=0,last_at=0,stats='{}',revision=revision+1 WHERE id=?", (run['id'],))
                    self.cancel_notices(run['id'])
                elif enabled and run['status'] == 'paused':
                    self.store.db.execute("UPDATE auto_quest_runs SET status='waiting',revision=revision+1 WHERE id=?", (run['id'],))
                    changed = dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?', (run['id'],)).fetchone())
                    self.enqueue(changed, 'assigned')
        return self.rules()

    def rows(self, platform, person):
        with self.store.lock:
            return [dict(r) for p,uid in self.q.accounts(platform,person) for r in self.store.db.execute('SELECT * FROM auto_quest_runs WHERE platform=? AND user_id=? ORDER BY id DESC',(p,uid))]

    def active(self, platform, person):
        with self.q.lock,self.store.lock,self.store.db:
            rows = sorted((r for r in self.rows(platform,person) if r['status'] in ('waiting','active','ready','paused')),key=lambda r:r['id'])
            if rows and self.c.profile(platform,person)['protected']:
                for run in rows:self.finish(run, 'cancelled')
                return None
            for r in rows[1:]:
                self.store.db.execute("UPDATE auto_quest_runs SET status='cancelled',finished=? WHERE id=?",(int(time.time()),r['id']))
                self.cancel_notices(r['id'])
            return rows[0] if rows else None

    @staticmethod
    def conditions(rule):
        if rule.get('board'):
            return rule['description']
        if rule['kind'] in ('calm','visits'):
            messages = quantity(rule['daily_messages'], ('разное содержательное сообщение', 'разных содержательных сообщения', 'разных содержательных сообщений'), 'neuter')
            return ('Оставайся в мирной беседе ' + hours_phrase(rule['hours']) + ' после получения свитка и оставь хотя бы одно содержательное сообщение за этот период. '
                    'Считается сама активность в окне, а не количество сообщений за каждый день. Серьёзная обида начнёт путь заново.')
        if rule['kind']=='dialogue':
            replies = quantity(rule['target'], ('добрый ответ', 'добрых ответа', 'добрых ответов'))
            return 'За ' + hours_phrase(rule['hours']) + ' оставь ' + replies + ' разным путникам, отвечая по делу на их слова. Повторы и беседа с самим собой не оставят следа в свитке.'
        messages = quantity(rule['target'], ('разное содержательное сообщение', 'разных содержательных сообщения', 'разных содержательных сообщений'), 'neuter')
        pause = quantity(rule['pause_seconds'] // 60, ('минута', 'минуты', 'минут'), 'female') if rule['pause_seconds'] % 60 == 0 else quantity(rule['pause_seconds'], ('секунда', 'секунды', 'секунд'), 'female')
        return 'За ' + hours_phrase(rule['hours']) + ' оставь ' + messages + '. Между репликами дай перу передышку: хотя бы ' + pause + '. Копии и перестановки слов не станут новыми строками летописи.'

    def describe(self, run):
        rule = json.loads(run['rule'])
        if run['status']=='paused':
            progress='Писарь свернул этот свиток по решению хранителя. Путь остановлен; после возобновления начнётся новая скрытая выдача.'
        elif run['status']=='ready':
            progress='Путь исполнен. Писарь сохранил добрые свидетельства; почётная отметка ждёт своего часа. Повторять дела не нужно.'
        elif run['status']=='waiting':
            progress='Писарь готовит твой личный свиток. Песочные часы будут перевёрнуты, когда он окажется у тебя.'
        else:
            progress = quest_progress(run)
            if rule['kind'] in ('calm','visits'):
                stats=json.loads(run['stats'])
                progress += (' Активность за это окно уже отмечена.' if stats.get('activity')
                             else ' В этом окне пока не было содержательной активности.')
            progress += ' Путь открыт до ' + quest_deadline(run['expires']) + '.'
        return f"📜 Твой свиток: «{rule['title']}».\n{self.conditions(rule)}\n{progress}\nДобрый путь укрепит доверие и приблизит примирение."

    def enqueue(self, run, kind, token='', **data):
        fingerprint=self.worker.service_messages.fingerprint(token) if token else run['token_hash']
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO auto_quest_notices(run_id,revision,kind,chat,user_id,token_hash,data,at,next_at) VALUES (?,?,?,?,?,?,?,?,?)',(run['id'],run['revision'],kind,str(run['chat']),run['user_id'],fingerprint,json.dumps(data,ensure_ascii=False),int(time.time()),int(time.time())))

    def cancel_notices(self, run_id):
        self.store.db.execute("UPDATE auto_quest_notices SET status='cancelled' WHERE run_id=? AND status IN ('pending','sending')", (run_id,))

    def board_run(self, row):
        run=dict(row);rule=json.loads(run['rule'])
        rule.update(board=True,hours=int(self.q.settings()['expires_days'])*24)
        run.update(id=-run['id'],thread_id=0,rule=json.dumps(rule,ensure_ascii=False),token_hash=self.worker.service_messages.fingerprint(self.store.setting('telegram_token')))
        return run

    def enqueue_board(self, row, kind, token='', **data):
        token=token or self.store.setting('telegram_token')
        if token:self.enqueue(self.board_run(row),kind,token,**data)

    def assign(self, person, nick, chat, key, token='', thread_id=0, origin='penalty'):
        if not self.enabled() or not re.fullmatch(r'-\d+',str(chat)):
            return None
        token=token or self.store.setting('telegram_token')
        if not token:
            return None
        config=self.q.settings();now=int(time.time())
        with self.q.lock,self.store.lock,self.store.db:
            if self.c.profile('telegram',person)['protected'] or self.q.active('telegram',person) or self.active('telegram',person):
                return None
            history=self.rows('telegram',person)
            if any(r['status']=='cancelled' and now-r['finished']<6*3600 for r in history):
                return None
            pause=config['auto_random_cooldown_hours'] if origin=='random' else config['auto_penalty_cooldown_hours']
            if origin!='chain' and origin!='help' and any(now-max(r['created'],r['finished'])<pause*3600 for r in history if (r['origin']=='random')==(origin=='random')):
                return None
            rule=next((dict(r) for r in self.rules() if r['key']==key and r['enabled']),None)
            if not rule:
                return None
            rule.update(daily_messages=int(config['auto_day_messages']),pause_seconds=int(config['auto_message_pause_seconds']),ignore_penalty=config['auto_ignore_penalty'])
            run_id=self.store.db.execute('INSERT INTO auto_quest_runs(platform,user_id,nick,chat,thread_id,rule,status,created,token_hash,origin) VALUES (?,?,?,?,?,?,\'waiting\',?,?,?)',('telegram',str(person),nick[:100],str(chat),thread_id,json.dumps(rule,ensure_ascii=False),now,self.worker.service_messages.fingerprint(token),origin)).lastrowid
            run=dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run_id,)).fetchone())
            self.enqueue(run,'assigned',token)
            return run

    def rule_enabled(self, rule):
        return any(r['key']==rule['key'] and r['enabled'] for r in self.rules())

    def request_reconciliation(self, person, nick, chat, token='', thread_id=0):
        if not self.enabled() or self.c.profile('telegram',person)['reputation']>=0:
            return None
        with self.q.lock,self.store.lock,self.store.db:
            run=self.active('telegram',person)
            if run and json.loads(run['rule'])['key']!='peace_three':
                rule=json.loads(run['rule']);strong=next((r for r in self.rules() if r['key']=='peace_three' and r['enabled']),None)
                if strong:
                    was_calm=rule['kind']=='calm'
                    rule.update(strong)
                    if run['status'] in ('ready','paused'):
                        self.store.db.execute("UPDATE auto_quest_runs SET status='waiting',started=0,expires=0,progress=0,last_at=0,stats='{}' WHERE id=?", (run['id'],))
                    elif not was_calm:
                        self.store.db.execute("UPDATE auto_quest_runs SET started=CASE WHEN started>0 THEN ? ELSE 0 END,progress=0,last_at=0,stats='{}' WHERE id=?",(int(time.time()),run['id']))
                    self.store.db.execute('UPDATE auto_quest_runs SET rule=?,expires=CASE WHEN started>0 THEN started+? ELSE 0 END,revision=revision+1 WHERE id=?',(json.dumps(rule,ensure_ascii=False),rule['hours']*3600,run['id']))
                    self.cancel_notices(run['id'])
                    run=dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run['id'],)).fetchone());self.enqueue(run,'assigned',token)
            return run or self.assign(person,nick,chat,'peace_three',token,thread_id,'help')

    def penalty(self, platform, person, chat, key, hint=0):
        full=str(chat)+':'+str(key) if platform=='telegram' and not str(key).startswith(str(chat)+':') else str(key)
        prefix='word:'+platform+':'+full+':'
        with self.store.lock:
            total,count=self.store.db.execute('SELECT COALESCE(SUM(grudge_delta),0),COUNT(*) FROM grudge_ledger WHERE platform=? AND user_id=? AND (source_key IN (?,?) OR substr(source_key,1,?)=?)',(platform,str(person),'semantic:'+platform+':'+full,'roleplay:'+platform+':'+full,len(prefix),prefix)).fetchone()
        return max(total,hint) if count else 0

    def on_message(self, platform, chat, person, nick, key, text, at=None, penalty_hint=0, human_person='', token='', thread_id=0):
        if platform not in ('telegram','twitch') or not self.enabled() or not person:
            return
        now=int(time.time());at=int(at or now)
        if at>now+60:
            return
        with self.q.lock,self.store.lock,self.store.db:
            if not self.store.db.execute('INSERT OR IGNORE INTO auto_quest_seen VALUES (?,?,?)',(platform,str(chat)+':'+str(key),now)).rowcount:
                return
            profile=self.c.profile(platform,person)
            if profile['protected']:
                return
            points=self.penalty(platform,person,chat,key,penalty_hint)
            run=self.active(platform,person)
            if platform == 'twitch' and (not run or not points):
                return  # Twitch violations affect the shared book; quest progress stays in Telegram.
            notice_token = token if platform == 'telegram' else self.store.setting('telegram_token')
            inside_path = bool(run and (run['status']=='waiting' or run['status']=='active' and run['started']<=at<run['expires']))
            if inside_path and json.loads(run['rule'])['key']=='peace_day' and points>json.loads(run['rule'])['ignore_penalty'] and (points>=self.q.settings()['auto_strong_penalty'] or profile['reputation']<=float(self.store.setting('community_grudge_strong_at','-21'))):
                run=self.request_reconciliation(run['user_id'],run['nick'],run['chat'],notice_token,run['thread_id'])
            if run and run['status']=='active':
                rule=json.loads(run['rule'])
                if not self.rule_enabled(rule):return
                if points>rule['ignore_penalty'] and run['started']<=at<run['expires']:
                    if rule['kind']=='calm':
                        self.store.db.execute("UPDATE auto_quest_runs SET started=?,expires=?,progress=0,last_at=0,stats='{}',revision=revision+1 WHERE id=?",(now,now+rule['hours']*3600,run['id']))
                        run=dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run['id'],)).fetchone());self.enqueue(run,'reset',notice_token)
                    else:
                        self.finish(run,'failed');self.enqueue(run,'failed',notice_token);run=None
                elif platform == 'telegram' and not points and run['started']<=at<run['expires']:
                    normalized=re.sub(r'[\W_\d]+',' ',text.casefold()).strip();words=set(normalized.split())
                    digest=hashlib.sha256(' '.join(sorted(words)).encode()).hexdigest()
                    stats=json.loads(run['stats']);evidence=stats.get('evidence',[])
                    repeated=any(item['hash']==digest or len(words&set(item['words']))/max(1,len(words|set(item['words'])))>=.8 for item in evidence)
                    if len(normalized)>=15 and len(words)>=3 and at-run['last_at']>=rule['pause_seconds'] and not repeated and not text.lstrip().startswith(('/','!')):
                        evidence.append({'hash':digest,'words':sorted(words)[:80]});stats['evidence']=evidence[-600:]
                        progress=run['progress']
                        stats.setdefault('quest_activity_at', run['last_at'] if rule['kind'] != 'dialogue' or progress else 0)
                        if rule['kind'] in ('calm','visits'):
                            # One verified, non-repeated message proves
                            # activity for the whole short peaceful window.
                            stats.setdefault('activity', []).append(digest)
                        elif rule['kind']=='dialogue':
                            people=stats.setdefault('people',[])
                            if human_person and (platform,str(human_person)) not in self.q.accounts(platform,person) and str(human_person) not in people:
                                people.append(str(human_person));progress=len(people)
                        else:progress+=1
                        if rule['kind'] in ('calm','visits') or progress > run['progress']:
                            stats['quest_activity_at'] = at
                        self.store.db.execute('UPDATE auto_quest_runs SET progress=?,last_at=?,stats=? WHERE id=?',(progress,at,json.dumps(stats),run['id']))
                        if rule['kind'] in ('messages','dialogue') and progress>=rule['target']:
                            self.complete(dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run['id'],)).fetchone()))
            run=self.active(platform,person)
            if platform != 'telegram':
                return
            if not run and points and profile['reputation']<0 and secrets.randbelow(100)<self.q.settings()['auto_penalty_chance']:
                severe=points>=self.q.settings()['auto_strong_penalty'] or profile['reputation']<=float(self.store.setting('community_grudge_strong_at','-21'))
                self.assign(person,nick,chat,'peace_three' if severe else 'peace_day',token,thread_id)
            elif not run and not points and profile['reputation']>=0 and not text.lstrip().startswith(('/','!')) and len(text.strip())>=15 and len(set(text.split()))>=3 and secrets.randbelow(100)<self.q.settings()['auto_chat_chance']:
                choices=[r for r in self.rules() if r['enabled'] and r['kind']!='calm']
                if choices:self.assign(person,nick,chat,secrets.choice(choices)['key'],token,thread_id,'random')

    def finish(self, run, status):
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE auto_quest_runs SET status=?,finished=? WHERE id=?',(status,int(time.time()),run['id']))
            self.cancel_notices(run['id'])

    def complete(self, run):
        rule=json.loads(run['rule'])
        with self.q.lock,self.store.lock,self.store.db:
            current=self.store.db.execute('SELECT status,revision FROM auto_quest_runs WHERE id=?',(run['id'],)).fetchone()
            if not current or current['status'] not in ('active','ready') or current['revision'] != run['revision']:
                return
            if not self.enabled() or not self.rule_enabled(rule):return
            if self.c.profile('telegram',run['user_id'])['protected']:
                self.finish(run,'cancelled');return
            result=self.q.apply('telegram',run['chat'],run['user_id'],run['nick'],'auto:'+str(run['id']),'reward',rule['title'],'Проверено по времени и сообщениям',rule['reward'],'auto:run:'+str(run['id']),automatic=True)
            if not result and self.c.profile('telegram',run['user_id'])['reputation']<self.q.settings()['reward_cap']:
                self.store.db.execute("UPDATE auto_quest_runs SET status='ready' WHERE id=?",(run['id'],));return
            self.finish(run,'done');self.enqueue(run,'completed',reward=result['delta'] if result else 0)
            score=self.c.profile('telegram',run['user_id'])['reputation']
            if score<0:
                self.assign(run['user_id'],run['nick'],run['chat'],'peace_three' if score<=float(self.store.setting('community_grudge_strong_at','-21')) else 'peace_day',self.store.setting('telegram_token'),run['thread_id'],'chain')

    def activate(self, run_id, now=None, revision=None):
        now=int(now or time.time())
        with self.q.lock,self.store.lock,self.store.db:
            row=self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run_id,)).fetchone()
            if row and row['status'] in ('waiting','active','ready','paused') and self.c.profile('telegram',row['user_id'])['protected']:
                self.finish(dict(row), 'cancelled');return
            if row and row['status']=='waiting' and (revision is None or row['revision'] == revision) and self.enabled() and self.rule_enabled(json.loads(row['rule'])):
                rule=json.loads(row['rule'])
                self.store.db.execute("UPDATE auto_quest_runs SET status='active',started=?,expires=? WHERE id=?",(now,now+rule['hours']*3600,run_id))
                self.store.db.execute("UPDATE auto_quest_notices SET status='sent',error='' WHERE run_id=? AND revision=? AND kind='assigned' AND status IN ('pending','sending')",(run_id,row['revision']))

    def confirm_command_delivery(self, person):
        run=self.active('telegram',person)
        if run:self.activate(run['id'])

    def tick(self, now=None, watchdog=False):
        if not self.enabled():return
        now=int(now or time.time())
        if watchdog:
            poll=int(self.store.state('telegram_poll_ok_at','0'))
            if now-poll>120:return
            previous=int(self.store.state('auto_quest_monitor_at','0'))
            with self.store.lock,self.store.db:
                if previous and poll-previous>300:
                    for row in self.store.db.execute("SELECT * FROM auto_quest_runs WHERE status='active'").fetchall():
                        rule=json.loads(row['rule'])
                        if rule['kind'] in ('calm','visits'):
                            self.store.db.execute("UPDATE auto_quest_runs SET started=?,expires=?,progress=0,last_at=0,stats='{}',revision=revision+1 WHERE id=?",(now,now+rule['hours']*3600,row['id']))
                            changed=dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(row['id'],)).fetchone());self.enqueue(changed,'monitor')
                self.store.set_state('auto_quest_monitor_at',poll)
                if self.store.db.execute('SELECT 1 FROM telegram_inbox LIMIT 1').fetchone():return
        with self.store.lock:
            runs=[dict(r) for r in self.store.db.execute("SELECT * FROM auto_quest_runs WHERE status IN ('active','ready')")]
        for run in runs:
            rule=json.loads(run['rule'])
            if not self.rule_enabled(rule):continue
            with self.q.lock,self.store.lock,self.store.db:
                current=self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run['id'],)).fetchone()
                if not current or current['revision']!=run['revision'] or current['status'] not in ('active','ready'):continue
                run=dict(current)
                if self.c.profile('telegram',run['user_id'])['protected']:
                    self.finish(run,'cancelled');continue
                if run['status']=='ready':self.complete(run);continue
                if rule['kind'] in ('calm','visits'):
                    stats=json.loads(run['stats'])
                    if now >= run['expires']:
                        if stats.get('activity'):
                            self.complete(run)
                        else:
                            self.store.db.execute("UPDATE auto_quest_runs SET started=?,expires=?,progress=0,last_at=0,stats='{}',revision=revision+1 WHERE id=?",(now,now+rule['hours']*3600,run['id']))
                            changed=dict(self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(run['id'],)).fetchone());self.enqueue(changed,'activity')
                    elif stats.get('activity'):
                        self.store.db.execute('UPDATE auto_quest_runs SET progress=1 WHERE id=?',(run['id'],))
                elif now>=run['expires']:
                    self.finish(run,'expired');self.enqueue(run,'expired')
        self.enqueue_reminders(now, watchdog=watchdog)

    def reminder_due(self, run, now, delivery=False, check_inbox=True):
        config = self.q.settings()
        if (not config['auto_reminders_enabled'] or not self.q.enabled_for('telegram')
                or run['platform'] != 'telegram' or run['status'] != 'active'
                or not run['started'] or run['expires'] <= now or run['expires'] - run['started'] <= 86400
                or now - run['started'] < config['auto_reminder_grace_hours'] * 3600):
            return False
        rule = json.loads(run['rule'])
        if run['id'] < 0:
            if not config['board_enabled'] or not any(r['id'] == rule['id'] and r['enabled'] for r in self.q.rules()):
                return False
            last_at = run['last_at']
        else:
            if not self.enabled() or not self.rule_enabled(rule):
                return False
            last_at = json.loads(run['stats']).get('quest_activity_at', run['last_at'])
        today = datetime.fromtimestamp(now, ZONE)
        midnight = int(today.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        hour, minute = map(int, config['auto_reminder_time'].split(':'))
        scheduled = int(today.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())
        if now < scheduled or not delivery and now >= scheduled + 15 * 60 or last_at >= midnight:
            return False
        return not check_inbox or not self.pending_messages(run)

    def pending_messages(self, run):
        people = [str(uid) for platform, uid in self.q.accounts('telegram', run['user_id']) if platform == 'telegram']
        marks = ','.join('?' for _ in people)
        return bool(self.store.db.execute('SELECT 1 FROM telegram_inbox WHERE person IN (' + marks + ') LIMIT 1', people).fetchone())

    def enqueue_reminders(self, now=None, watchdog=False):
        now = int(now or time.time())
        if not self.q.settings()['auto_reminders_enabled'] or not self.q.enabled_for('telegram'):
            return
        if watchdog and now - int(self.store.state('telegram_poll_ok_at', '0')) > 120:
            return
        day = datetime.fromtimestamp(now, ZONE).date().isoformat()
        kind = 'reminder:' + day
        with self.q.lock, self.store.lock, self.store.db:
            runs = [dict(row) for row in self.store.db.execute("SELECT * FROM auto_quest_runs WHERE platform='telegram' AND status='active'")]
            runs += [self.board_run(row) for row in self.store.db.execute("SELECT * FROM quest_runs WHERE platform='telegram' AND status='active'")]
            for run in runs:
                if self.c.profile('telegram', run['user_id'])['protected'] or not self.reminder_due(run, now):
                    continue
                people = [str(uid) for platform, uid in self.q.accounts('telegram', run['user_id']) if platform == 'telegram']
                marks = ','.join('?' for _ in people)
                if self.store.db.execute("SELECT 1 FROM auto_quest_notices WHERE kind=? AND user_id IN (" + marks + ") AND status IN ('pending','sending','sent') LIMIT 1", (kind, *people)).fetchone():
                    continue
                self.enqueue(run, kind)

    def text_for_notice(self, notice, run):
        config=self.q.settings();rule=json.loads(run['rule']);kind=notice['kind'];data=json.loads(notice['data'])
        template=config['auto_reminder_message'] if kind.startswith('reminder:') else config['auto_assigned_message'] if kind=='assigned' else config['auto_completed_message'] if kind=='completed' else config['auto_reset_message'] if kind=='reset' else {
            'monitor':'📜 Пелена скрыла часть пути «{quest}» от писаря. Он переворачивает песочные часы заново, чтобы каждое доброе дело обрело свидетеля.',
            'activity':'📜 Для пути «{quest}» не хватило добрых следов в чате. Писарь начинает новый отсчёт; /quest покажет условия.',
            'expired':'📜 Срок свитка «{quest}» истёк. Награда не начислена; новое поручение может появиться позже.',
            'failed':'📜 Новая серьёзная пометка прервала путь «{quest}». Свиток закрыт без награды; писарь ещё предложит новый путь.',
        }.get(kind,'📜 Писарь обновил твой личный свиток «{quest}». /quest — прогресс.')
        values={'quest':rule['title'],'conditions':self.conditions(rule),'reward':reward_words(data.get('reward',rule['reward'])),'user':run['nick'].replace('@',''),'reason':'игровое событие'}
        if kind.startswith('reminder:'):
            values['deadline'] = quest_deadline(run['expires'])
            values['progress'] = quest_progress(run)
        for key,value in values.items():template=template.replace('{'+key+'}',value)
        return self.c.redact_terms(template)[:3000]

    @staticmethod
    def notice_current(notice, run):
        allowed = {'assigned': ('waiting', 'active'), 'reset': ('active',),
                   'monitor': ('active',), 'activity': ('active',),
                   'completed': ('done',), 'expired': ('expired',), 'failed': ('failed',)}
        return bool(run and run['revision'] == notice['revision'] and
                    run['status'] in (('active',) if notice['kind'].startswith('reminder:') else allowed.get(notice['kind'], ())))

    def flush(self, token):
        if not token or not self.q.enabled_for('telegram') or self.store.setting('reply_enabled','1')!='1':return
        now=int(time.time());fingerprint=self.worker.service_messages.fingerprint(token)
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE auto_quest_notices SET status='pending' WHERE status='sending' AND next_at<=?",(now,))
            notices=[dict(r) for r in self.store.db.execute("SELECT * FROM auto_quest_notices WHERE status='pending' AND next_at<=? ORDER BY id LIMIT 10",(now,))]
        for notice in notices:
            with self.q.lock,self.store.lock,self.store.db:
                if notice['run_id']<0:
                    board=self.store.db.execute('SELECT * FROM quest_runs WHERE id=?',(-notice['run_id'],)).fetchone()
                    run=self.board_run(board) if board else None
                else:run=self.store.db.execute('SELECT * FROM auto_quest_runs WHERE id=?',(notice['run_id'],)).fetchone()
                if run and run['status'] in ('waiting','active','paused','ready') and self.c.profile('telegram',run['user_id'])['protected']:
                    table='quest_runs' if notice['run_id']<0 else 'auto_quest_runs'
                    self.store.db.execute("UPDATE "+table+" SET status='cancelled',finished=? WHERE id=?",(now,abs(notice['run_id'])))
                    self.cancel_notices(notice['run_id'])
                    continue
                invalid=notice['token_hash']!=fingerprint or not self.notice_current(notice,run)
                if not invalid and notice['kind'].startswith('reminder:'):
                    invalid = (notice['kind'] != 'reminder:' + datetime.fromtimestamp(now, ZONE).date().isoformat()
                               or not self.reminder_due(dict(run), now, delivery=True, check_inbox=False))
                    poll = int(self.store.state('telegram_poll_ok_at', '0'))
                    if not invalid and (self.pending_messages(run) or poll and now - poll > 120):
                        self.store.db.execute("UPDATE auto_quest_notices SET next_at=? WHERE id=? AND status='pending'", (now + 30, notice['id']))
                        continue
                if invalid:
                    self.store.db.execute("UPDATE auto_quest_notices SET status='cancelled' WHERE id=?",(notice['id'],));continue
                if not self.store.db.execute("UPDATE auto_quest_notices SET status='sending',next_at=? WHERE id=? AND status='pending'",(now+60,notice['id'])).rowcount:continue
                run=dict(run)
            if notice['kind']=='assigned':
                rule=json.loads(run['rule'])
                enabled=(self.q.settings()['board_enabled'] and any(r['id']==rule['id'] and r['enabled'] for r in self.q.rules())) if run['id']<0 else self.enabled() and self.rule_enabled(rule)
                if not enabled:
                    with self.store.lock,self.store.db:self.store.db.execute("UPDATE auto_quest_notices SET status='pending',next_at=?,error='Задание или его выдача выключены в настройках' WHERE id=? AND status='sending'",(now+60,notice['id']))
                    continue
            try:
                self.worker.service_messages.private_notice(token,notice['chat'],notice['user_id'],self.text_for_notice(notice,run),run['thread_id'])
            except Exception as exc:
                error=str(exc).replace(token,'[скрыто]')[:200]
                with self.store.lock,self.store.db:
                    changed=self.store.db.execute("UPDATE auto_quest_notices SET status='pending',attempts=attempts+1,next_at=?,error=? WHERE id=? AND status='sending'",(now+min(1800,30*2**min(notice['attempts'],6)),error,notice['id'])).rowcount
                if changed and not notice['attempts']:self.store.event('info','Скрытый свиток не доставлен: '+error)
            else:
                with self.q.lock,self.store.lock,self.store.db:
                    if notice['kind'].startswith('reminder:'):
                        # A private delivery may finish after a reset/cancellation.
                        # It was still received: keep the daily limit across revisions.
                        self.store.db.execute("UPDATE auto_quest_notices SET status='sent',error='' WHERE id=?", (notice['id'],))
                        continue
                    table='quest_runs' if run['id']<0 else 'auto_quest_runs'
                    current=self.store.db.execute('SELECT * FROM '+table+' WHERE id=?',(abs(run['id']),)).fetchone()
                    if not self.notice_current(notice,current):
                        self.store.db.execute("UPDATE auto_quest_notices SET status='cancelled' WHERE id=? AND status='sending'",(notice['id'],))
                        continue
                    if not self.store.db.execute("UPDATE auto_quest_notices SET status='sent',error='' WHERE id=? AND status='sending'",(notice['id'],)).rowcount:
                        continue
                    if notice['kind']=='assigned':
                        if run['id']<0:self.q.activate_manual(abs(run['id']),notice['revision'])
                        else:self.activate(run['id'],revision=notice['revision'])

    def panel(self):
        with self.store.lock:
            runs=[dict(r) for r in self.store.db.execute("SELECT * FROM auto_quest_runs WHERE status IN ('waiting','active','ready','paused') ORDER BY id DESC LIMIT 100")]
            notices=[dict(r) for r in self.store.db.execute('SELECT * FROM auto_quest_notices ORDER BY id DESC LIMIT 50')]
        return {'auto_rules':self.rules(),'auto_runs':runs,'private_notices':notices}

    def loop(self):
        while not self.worker.stop.is_set():
            try:
                self.tick(watchdog=True)
                self.enqueue_reminders(watchdog=True)
                self.flush(self.store.setting('telegram_token'))
            except Exception as exc:self.store.event('info','Автоквесты: '+type(exc).__name__)
            self.worker.stop.wait(10)

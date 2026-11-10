"""Clear Telegram history only for the scout, after durable context capture."""
import asyncio
import json
import time

from .scout_transport import rpc_error


class ScoutHistory:
    def __init__(self,scout):
        self.scout,self.store=scout,scout.store
        self.lock=None
        self.scan_at=0
        self.scan_owner=''
        self.scan_error=''
        with self.store.lock,self.store.db:
            self.store.db.executescript('''
                CREATE TABLE IF NOT EXISTS scout_history_jobs (
                    owner TEXT NOT NULL,chat TEXT NOT NULL,target TEXT NOT NULL,max_id INTEGER NOT NULL,
                    cleared_id INTEGER NOT NULL DEFAULT 0,at INTEGER NOT NULL,next_at INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(owner,chat));
                CREATE TABLE IF NOT EXISTS scout_history_refs (
                    owner TEXT NOT NULL,chat TEXT NOT NULL,message_id INTEGER NOT NULL,body TEXT NOT NULL,
                    author TEXT NOT NULL,root INTEGER NOT NULL,post_id INTEGER NOT NULL,post TEXT NOT NULL,
                    channel_id TEXT NOT NULL,PRIMARY KEY(owner,chat,message_id));
            ''')
            if 'queued_at' not in {row[1] for row in self.store.db.execute('PRAGMA table_info(scout_history_jobs)')}:
                self.store.db.execute('ALTER TABLE scout_history_jobs ADD COLUMN queued_at INTEGER NOT NULL DEFAULT 0')
                self.store.db.execute('UPDATE scout_history_jobs SET queued_at=at WHERE max_id>cleared_id')

    def remember(self,target,message,snapshot=False):
        if not self.scout.settings()['clear_group_history'] or not self.scout.actor or target.get('owner')!=self.scout.actor.get('id'):return
        owner,chat=self.scout.actor['id'],target['chat']
        # Persist only contexts the ordinary scout pipeline has actually observed.
        if not self.scout.settings()['remember']:
            self.schedule(target,message['id'])
            return
        with self.store.lock,self.store.db:
            rows=[(message['id'],message['text'],message['person'])]
            if message.get('parent_id') and message.get('parent'):
                rows.append((message['parent_id'],message['parent'],message.get('parent_author','')))
            if message.get('root') and message.get('post_id') and message.get('post'):
                rows.append((message['root'],message['post'],'channel:'+str(message.get('origin_channel_id') or target.get('channel_id',''))))
            for number,body,author in rows:
                self.store.db.execute(('INSERT OR IGNORE' if snapshot else 'INSERT OR REPLACE')+' INTO scout_history_refs VALUES (?,?,?,?,?,?,?,?,?)',
                    (owner,chat,number,body[:4000],author,message.get('root',0),message.get('post_id',0),message.get('post','')[:2000],message.get('origin_channel_id') or target.get('channel_id','')))
        if not snapshot:self.schedule(target,message['id'])

    def schedule(self,target,max_id):
        if not self.scout.actor or target.get('owner')!=self.scout.actor.get('id') or not self.scout.settings()['clear_group_history'] or int(max_id or 0)<=0:return
        with self.store.lock,self.store.db:
            now=int(time.time())
            self.store.db.execute('''INSERT INTO scout_history_jobs(owner,chat,target,max_id,at,queued_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(owner,chat) DO UPDATE SET target=excluded.target,
                at=CASE WHEN excluded.max_id>max_id THEN excluded.at ELSE at END,
                queued_at=CASE WHEN max_id<=cleared_id AND excluded.max_id>cleared_id THEN excluded.queued_at ELSE queued_at END,
                max_id=MAX(max_id,excluded.max_id)''',
                (self.scout.actor['id'],target['chat'],json.dumps(target),int(max_id),now,now))

    def live(self,target,account,owner,epoch):
        # History cleanup is independent of initiative, test mode and reply pauses.
        return bool(account and self.scout.account is account and self.scout.actor.get('id')==owner
                    and target.get('owner')==owner and self.scout.phase=='connected'
                    and self.scout.epoch==epoch and self.scout.settings()['clear_group_history'])

    async def bootstrap(self,force=False):
        account,owner,epoch=self.scout.account,self.scout.actor.get('id'),self.scout.epoch
        now=int(time.time())
        if not account or not owner or self.scout.phase!='connected' or not self.scout.settings()['clear_group_history']:return
        if self.scout.busy or self.scout.queue and not self.scout.queue.empty():return
        retry=json.loads(self.store.state('scout_history_scan:'+owner,'{}'))
        if retry.get('retry_at',0)>now:
            self.scan_owner=owner;self.scan_at=retry['retry_at'];self.scan_error=retry.get('error','')
            return
        if self.scan_owner==owner and now<self.scan_at and (not force or self.scan_error):return
        self.scan_owner=owner
        self.scan_at=now+600
        try:
            groups=await account.group_histories()
            for group in groups:
                target={**group,'owner':owner,'_history_only':True}
                if not self.live(target,account,owner,epoch):return
                if self.scout.busy or self.scout.queue and not self.scout.queue.empty():
                    self.scan_at=now+15;return
                with self.store.lock:
                    old=self.store.db.execute('SELECT max_id FROM scout_history_jobs WHERE owner=? AND chat=?',(owner,target['chat'])).fetchone()
                if old and old[0]>=group['max_id']:continue
                if self.scout.settings()['remember']:
                    contexts=await account.group_history_context(target,group['max_id'])
                    if not self.live(target,account,owner,epoch):return
                    for message in contexts:self.remember(target,message,snapshot=True)
                self.schedule(target,group['max_id'])
            self.scan_error=''
            self.store.set_state('scout_history_scan:'+owner,'{}')
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.scan_at=now+(max(1,int(exc.seconds)) if hasattr(exc,'seconds') else 120)
            self.scan_error=rpc_error(exc)
            self.store.set_state('scout_history_scan:'+owner,json.dumps({'retry_at':self.scan_at,'error':self.scan_error}))
            self.scout.event('group_history_scan_retry','Проверка оставшейся истории групп отложена: '+self.scan_error)

    def panel(self):
        owner=self.scout.actor.get('id','')
        with self.store.lock:
            rows=[dict(row) for row in self.store.db.execute('SELECT chat,max_id,cleared_id,at,next_at,attempts FROM scout_history_jobs WHERE owner=?',(owner,))]
        return {'pending':sum(row['max_id']>row['cleared_id'] for row in rows),'error':self.scan_error if self.scan_owner==owner else '',
                'chats':rows}

    async def clear_now(self):
        if not self.scout.account or self.scout.phase!='connected':raise ValueError('Подключите аккаунт наблюдателя.')
        if not self.scout.settings()['clear_group_history']:raise ValueError('Сначала включите очистку групповой истории.')
        # Called on the same event loop; do not overlap automatic cleanup.
        if self.lock is None:self.lock=asyncio.Lock()
        async with self.lock:await self.bootstrap(force=True)
        await self.tick(force=True)
        return self.panel()

    def lookup(self,chat,number):
        if not self.scout.settings()['remember']:return None
        owner=self.scout.actor.get('id','')
        with self.store.lock:
            row=self.store.db.execute('SELECT * FROM scout_history_refs WHERE owner=? AND chat=? AND message_id=?',(owner,str(chat),int(number))).fetchone()
            if row:return dict(row)
            row=self.store.db.execute('SELECT body,actor,root,source_id FROM assistant_lines WHERE owner=? AND chat=? AND message_id=?',(owner,str(chat),int(number))).fetchone()
            if not row:return None
            source=self.store.db.execute('SELECT post_id,post,channel_id FROM scout_history_refs WHERE owner=? AND chat=? AND message_id=?',(owner,str(chat),row['source_id'])).fetchone()
        return {'body':row['body'],'author':owner if row['actor']=='scout' else str(self.scout.worker.bot_id),
                'root':row['root'],'post_id':source['post_id'] if source else 0,'post':source['post'] if source else '',
                'channel_id':source['channel_id'] if source else ''}

    async def tick(self,force=False):
        if self.lock is None:self.lock=asyncio.Lock()
        async with self.lock:
            account,owner,epoch=self.scout.account,self.scout.actor.get('id'),self.scout.epoch
            def live(target):
                return self.live(target,account,owner,epoch)
            if not account or not owner or self.scout.busy or self.scout.queue and not self.scout.queue.empty():return
            now=int(time.time())
            with self.store.lock:
                jobs=[dict(row) for row in self.store.db.execute('SELECT * FROM scout_history_jobs WHERE owner=? AND max_id>cleared_id AND (at<=? OR queued_at<=?) AND (next_at<=? OR (? AND attempts=0)) ORDER BY queued_at LIMIT 10',(owner,now if force else now-60,now if force else now-300,now,int(force)))]
            for job in jobs:
                target=json.loads(job['target'])
                if not live(target):continue
                if self.scout.busy or self.scout.queue and not self.scout.queue.empty():return
                try:
                    complete=await account.clear_group_history(target,job['max_id'],guard=lambda:live(target) and not self.scout.busy and (not self.scout.queue or self.scout.queue.empty()))
                    if not live(target):return
                    with self.store.lock,self.store.db:
                        self.store.db.execute('UPDATE scout_history_jobs SET cleared_id=CASE WHEN ? THEN MAX(cleared_id,?) ELSE cleared_id END,queued_at=CASE WHEN ? AND max_id<=? THEN 0 ELSE queued_at END,next_at=?,attempts=0 WHERE owner=? AND chat=?',
                            (bool(complete),job['max_id'],bool(complete),job['max_id'],now+300 if complete else now+5,owner,job['chat']))
                    if complete:self.scout.event('group_history_cleared','История очищена только у аккаунта наблюдателя. Публичные сообщения и база бота сохранены.',job['chat'])
                except asyncio.CancelledError:raise
                except Exception as exc:
                    attempt=job['attempts']+1
                    delay=max(1,int(exc.seconds)) if hasattr(exc,'seconds') else min(3600,15*2**min(attempt,8))
                    with self.store.lock,self.store.db:
                        self.store.db.execute('UPDATE scout_history_jobs SET next_at=?,attempts=? WHERE owner=? AND chat=?',(now+delay,attempt,owner,job['chat']))
                    self.scout.event('group_history_retry','Очистка истории у наблюдателя отложена: '+rpc_error(exc),job['chat'])

    async def loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                if self.lock is None:self.lock=asyncio.Lock()
                async with self.lock:await self.bootstrap()
                await self.tick()
            except asyncio.CancelledError:raise
            except Exception as exc:self.scout.event('group_history_error',rpc_error(exc))

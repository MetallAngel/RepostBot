"""Reply-only participation in groups the scout has actually joined."""
import json
import re
import time


class ScoutJoined:
    def __init__(self,scout):
        self.scout,self.store=scout,scout.store
        self.proofs={}
        with self.store.lock,self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS scout_joined_groups (
                owner TEXT NOT NULL,chat TEXT NOT NULL,title TEXT NOT NULL,peer TEXT NOT NULL,
                username TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,at INTEGER NOT NULL,
                PRIMARY KEY(owner,chat))''')

    def candidates_allowed(self,chat):
        owner=self.scout.actor.get('id')
        if not owner or not self.scout.account or self.scout.phase!='connected' or not self.scout.settings()['joined_chat_replies'] or not re.fullmatch(r'-\d+',str(chat)):return False
        test=self.scout.destination('test_chat')
        if test.get('owner')==owner and test.get('chat')==str(chat):return False
        home=self.scout.destination()
        if home.get('owner')==owner and home.get('chat')==str(chat):return False
        with self.store.lock:
            # Explicit targets retain their pause, post and channel restrictions.
            if self.store.db.execute('SELECT 1 FROM scout_targets WHERE owner=? AND chat=?',(owner,str(chat))).fetchone():return False
            row=self.store.db.execute('SELECT enabled FROM scout_joined_groups WHERE owner=? AND chat=?',(owner,str(chat))).fetchone()
        return row is None or bool(row['enabled'])

    def target(self,chat):
        if not self.candidates_allowed(chat):return None
        with self.store.lock:
            row=self.store.db.execute('SELECT * FROM scout_joined_groups WHERE owner=? AND chat=?',(self.scout.actor['id'],str(chat))).fetchone()
        if not row:return None
        return {'owner':row['owner'],'chat':row['chat'],'title':row['title'],'peer':json.loads(row['peer']),
                'username':row['username'],'comments_only':False,'channel_id':'','post_id':0,'_joined_chat':True}

    def eligible(self,chat):
        if not self.candidates_allowed(chat):return False
        presence=self.proofs.get(str(chat))
        if presence and presence[0]==self.scout.actor.get('id') and presence[1]>time.time()-60:return bool(presence[2])
        return bool(self.target(chat))

    def prove(self,chat,present):
        self.proofs[str(chat)]=(self.scout.actor.get('id'),time.time(),bool(present))

    def addressed(self,message,include_reply=True):
        if message.get('forwarded') or message.get('bot') or message.get('text','').lstrip().startswith('/'):return False
        text=message.get('text','')
        names=['помощник','помощник','помощник','помощник','наблюдатель',self.scout.actor.get('username',''),self.scout.actor.get('name',''),
               self.scout.worker.bot_username,*self.scout.disguise.aliases()]
        names+=re.split(r'[,;\n]',self.store.setting('reply_names'))
        return bool(message.get('mentioned') or self.scout.disguise.cover_named(text)
                    or include_reply and str(message.get('parent_author','')) in (self.scout.actor.get('id'),str(self.scout.worker.bot_id))
                    or any(name and re.search(r'(?<!\w)@?'+re.escape(name.strip())+r'(?!\w)',text,re.I) for name in names if name and name.strip()))

    def call(self,message):
        return self.eligible(message['chat']) and self.addressed(message)

    async def discover(self,event):
        chat=str(event.chat_id)
        if not self.candidates_allowed(chat):return None
        account,owner,epoch=self.scout.account,self.scout.actor.get('id'),self.scout.epoch
        target=await account.joined_group(event)
        if not target or account is not self.scout.account or owner!=self.scout.actor.get('id') or epoch!=self.scout.epoch:return None
        if not self.candidates_allowed(chat) or target['chat']!=chat:return None
        with self.store.lock,self.store.db:
            existed=self.store.db.execute('SELECT 1 FROM scout_joined_groups WHERE owner=? AND chat=?',(owner,chat)).fetchone()
            self.store.db.execute('''INSERT INTO scout_joined_groups(owner,chat,title,peer,username,at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(owner,chat) DO UPDATE SET title=excluded.title,peer=excluded.peer,username=excluded.username,at=excluded.at''',
                (owner,chat,target['title'],json.dumps(target['peer']),target['username'],int(time.time())))
        self.scout.dialogue.scout_presence[chat]=(time.time(),True)
        self.prove(chat,True)
        if not existed:self.scout.event('joined_group','Группа обнаружена: отвечает на обращения без тестового режима.',chat)
        return self.target(chat)

    def panel(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute('SELECT owner,chat,title,enabled,at FROM scout_joined_groups ORDER BY at DESC')]

    def active(self):
        return [target for row in self.panel() if row['owner']==self.scout.actor.get('id') and row['enabled']
                for target in [self.target(row['chat'])] if target and self.eligible(row['chat'])]

    def change(self,chat,enabled):
        owner=self.scout.actor.get('id')
        if not owner:raise ValueError('Подключите наблюдателя.')
        if not isinstance(enabled,bool):raise ValueError('Укажите, включены ли ответы в этой группе.')
        with self.store.lock,self.store.db:
            if not self.store.db.execute('SELECT 1 FROM scout_joined_groups WHERE owner=? AND chat=?',(owner,str(chat))).fetchone():
                raise ValueError('Группа не найдена для подключённого аккаунта.')
            self.store.db.execute('UPDATE scout_joined_groups SET enabled=? WHERE owner=? AND chat=?',(int(bool(enabled)),owner,str(chat)))
        self.scout.epoch+=1
        return self.scout.panel()

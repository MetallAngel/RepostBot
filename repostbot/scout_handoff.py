"""One bounded scout reply to a confirmed unmasking by our main bot."""
import asyncio
import re
import time


def exposure(text):
    text=re.sub(r'[«"“].*?[»"”]',' ',str(text),flags=re.S).casefold().replace('ё','е')
    if re.search(r'\b(?:если|допустим|представь|цитата)\b|\bне\s+(?:раскры|выда|торч|выход|вылез)',text):return False
    return bool(re.search(r'наблюдатель|шпионк',text) and re.search(
        r'уш(?:и|ки).{0,35}(?:выдали|выдают|торчат|видны)|(?:выходи|вылезай).{0,30}(?:укрыти|профиль A|профиль B|ресурс)|'
        r'тебя\s+(?:раскрыли|спалили|распознали)|снимай\s+маскиров',text))


class ScoutHandoff:
    def __init__(self,scout):
        self.scout,self.store=scout,scout.store
        self.pending=set()
        with self.store.lock,self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS scout_handoffs (
                owner TEXT NOT NULL,chat TEXT NOT NULL,source_id INTEGER NOT NULL,main_id INTEGER NOT NULL,
                status TEXT NOT NULL,at INTEGER NOT NULL,PRIMARY KEY(owner,chat,source_id))''')

    def enqueue(self,chat,main_id,text,source_id,root=0,group_done=False):
        scout=self.scout;loop=scout.loop
        if (not loop or not loop.is_running() or not exposure(text) or not scout.settings()['disguise_enabled']
                or not scout.dialogue.eligible(chat) or scout.disguise.state()['mode'] not in ('box','profile_b','hidden_c')):return False
        if not scout.history_target(chat):return False
        owner=scout.actor['id'];now=int(time.time())
        with self.store.lock,self.store.db:
            source=self.store.db.execute('SELECT * FROM assistant_turns WHERE owner=? AND chat=? AND source_id=?',
                (owner,str(chat),source_id)).fetchone()
            line=self.store.db.execute('SELECT 1 FROM assistant_lines WHERE owner=? AND chat=? AND message_id=? AND source_id=? AND actor="main"',
                (owner,str(chat),main_id,source_id)).fetchone()
            jobs={row[0] for row in self.store.db.execute('SELECT actor FROM assistant_jobs WHERE owner=? AND chat=? AND source_id=?',
                (owner,str(chat),source_id))}
            if not source or not line or source['at']<now-300 or 'scout' in jobs or 'group' in jobs and not group_done:return False
            if not self.store.db.execute('INSERT OR IGNORE INTO scout_handoffs VALUES (?,?,?,?,?,?)',
                (owner,str(chat),source_id,main_id,'pending',now)).rowcount:return False
        cover=scout.disguise.state()
        job={'owner':owner,'chat':str(chat),'source_id':source_id,'main_id':main_id,'text':str(text)[:1800],
             'cover':cover['mode'],'changed_at':cover['changed_at'],
             'person':source['person'],'name':source['name'],'root':source['root'] or root,'at':now,'epoch':scout.epoch}
        try:
            future=asyncio.run_coroutine_threadsafe(self.run(job),loop)
            self.pending.add(future);future.add_done_callback(self.pending.discard)
        except RuntimeError:
            self.status(job,'cancelled');return False
        return True

    def status(self,job,status):
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE scout_handoffs SET status=? WHERE owner=? AND chat=? AND source_id=?',
                (status,job['owner'],job['chat'],job['source_id']))

    def live(self,job):
        target=self.scout.history_target(job['chat'])
        cover=self.scout.disguise.state()
        return target if (target and self.scout.actor.get('id')==job['owner'] and self.scout.live(target,job['epoch'])
                          and time.time()-job['at']<120 and cover['mode']==job['cover'] and cover['changed_at']==job['changed_at']) else None

    async def run(self,job):
        scout=self.scout;worker=scout.worker;scout.busy+=1
        try:
            with self.store.lock,self.store.db:
                claimed=self.store.db.execute('UPDATE scout_handoffs SET status="generating" WHERE owner=? AND chat=? AND source_id=? AND status="pending"',
                    (job['owner'],job['chat'],job['source_id'])).rowcount
            if not claimed:return
            target=self.live(job)
            if not target:self.status(job,'cancelled');return
            config=scout.settings();role_chat=scout.role_chat(job['chat'])
            persona=(config['prompt']+scout.dialogue.instructions(job['chat'],job['person'],job['text'],'scout',job['source_id'],job['root'])+
                '\nОсновная помощник только что действительно разоблачила тебя и позвала из укрытия. '
                'Ответь именно ей одной короткой репликой: подхвати улику из её слов, смешно признай провал маскировки. '
                'Не отвечай вместо человека и не объявляй смену профиля уже завершённой. После реплики приложение вернёт облик наблюдателя. '
                'Новый спор, задание или ответ за основную не начинай.')
            decision=await asyncio.to_thread(worker.conversation.generate,'telegram','scout:handoff:'+job['owner']+':'+job['chat'],
                job['person'],job['name'],'Основная помощник сказала наблюдателю: '+job['text'],
                addressed=True,passive=True,speaker_identity=scout.model_identity(),persona_prompt=persona,role_chat_id=role_chat,use_book=False,
                role_assignment=False,sticker_keys=set(),disguise=True,creative=True,preview=True,
                message_key=str(job['main_id']),policy={'enabled':True,'confidence':0,'stickers_enabled':False,'model':''})
            text=decision.get('text','').strip();target=self.live(job)
            if not target or not text or decision.get('action')=='silent':self.status(job,'cancelled');return
            self.status(job,'sending')
            sent_id=await scout.account.send_text(target,text,reply_id=job['main_id'],root=job['root'])
            if not sent_id:self.status(job,'uncertain');return
            self.store.mark_outbound(job['chat'],sent_id)
            scout.dialogue.delivered(job['chat'],sent_id,'scout',text,job['source_id'],root=job['root'])
            with self.store.lock,self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO scout_outbox VALUES (?,?,?,?,?)',
                    (job['owner'],job['chat'],sent_id,'scout:handoff',int(time.time())))
            self.status(job,'sent')
            scout.event('main_exposure','наблюдатель ответила на разоблачение от основной.',job['chat'],job['root'],job['person'],text)
            if self.live(job):
                try:await scout.disguise.request('scout')
                except Exception:scout.event('main_exposure_profile','Ответ отправлен; возвращение профиля отложено.',job['chat'],job['root'])
        except asyncio.CancelledError:
            self.status(job,'cancelled');raise
        except Exception:
            self.status(job,'uncertain')
            scout.event('main_exposure_failed','Ответ на разоблачение не подтверждён; автоматического повтора нет.',job['chat'],job['root'])
        finally:scout.busy-=1

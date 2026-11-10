"""An optional scout retort can end one real main-bot glitch episode."""
import asyncio
import secrets
import time

from .glitch_context import topic


TEASE_GUIDE = (
    '\nУ основной помощник сейчас подтверждён активный сбой; наблюдатель ему не подвержена. '
    'Если странность основной прямо связана с текущей беседой, наблюдатель может мягко поддеть '
    'напарницу за ошибка или запутавшийся технический контекст. Не вставляй ошибка ради ключевого слова: '
    'подхвати конкретную странную деталь основной и сохрани тему. Можно отказаться от подкола. '
    'Только у строки scout добавь glitch_tease=true, если такой подкол действительно получился, иначе false. '
    'Не объявляй, что сбой уже снят, не отдавай приказов людям и не меняй собственную манеру.'
)


class ScoutGlitch:
    def __init__(self,scout):
        self.scout,self.worker,self.store=scout,scout.worker,scout.store
        with self.store.lock,self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS scout_glitch_retorts (
                owner TEXT NOT NULL,chat TEXT NOT NULL,episode TEXT NOT NULL,at INTEGER NOT NULL,
                status TEXT NOT NULL,PRIMARY KEY(owner,chat,episode))''')

    def offer(self,message,actors,previous=None):
        home=self.scout.destination()
        owner=self.scout.actor.get('id')
        if (not owner or home.get('owner')!=owner or home.get('chat')!=message['chat']
                or 'scout' not in actors or not (actors[0]=='main' or previous)
                or topic(message.get('text','')) in ('serious','technical')):return None
        episode=self.worker.conversation.glitch_active(message['chat'])
        if not episode or episode.get('phase')!='active' or not episode.get('bot_message_id'):return None
        with self.store.lock:
            seen=self.store.db.execute('SELECT 1 FROM scout_glitch_retorts WHERE owner=? AND chat=? AND (episode=? OR at>?)',
                (owner,message['chat'],episode['id'],int(time.time())-900)).fetchone()
        if seen or secrets.randbelow(100)>=self.scout.settings()['glitch_tease_chance']:return None
        return {'episode':episode['id'],'owner':owner,'chat':message['chat']}

    async def after_tease(self,destination,message_id,decision,offer,epoch):
        if not offer or decision.get('glitch_tease') is not True or not decision.get('text') or not message_id:return False
        account,owner=self.scout.account,self.scout.actor.get('id')
        def live():
            home=self.scout.destination()
            return bool(account and self.scout.account is account and owner==self.scout.actor.get('id')==offer['owner']
                and self.scout.epoch==epoch and self.scout.phase=='connected' and destination['chat']==offer['chat']
                and home.get('owner')==owner and home.get('chat')==offer['chat'])
        if not live():return False
        episode=self.worker.conversation.glitch_active(offer['chat'])
        if not episode or episode.get('id')!=offer['episode'] or episode.get('phase')!='active':return False
        with self.store.lock,self.store.db:
            claimed=self.store.db.execute('INSERT OR IGNORE INTO scout_glitch_retorts VALUES (?,?,?,?,?)',
                (owner,offer['chat'],offer['episode'],int(time.time()),'teased')).rowcount
        if not claimed:return False
        if secrets.randbelow(100)>=self.scout.settings()['glitch_recovery_chance']:return False
        try:
            response=await asyncio.to_thread(self.worker.conversation.generate,'telegram','scout:recovery:main:'+offer['chat'],
                '', 'Аккаунт-наблюдатель',decision['text'],preview=True,passive=True,addressed=True,glitch_immune=True,
                speaker_identity=self.scout.model_identity('main'),persona_prompt=self.scout.settings()['reaction_prompt']+'\nТы основная помощник. наблюдатель только что поддела твою странную манеру. '
                    'В этом ходе ты выходишь из сбоя: ответь ей обычным голосом, с самоиронией признай, что тебя занесло, '
                    'и подхвати конкретную деталь подкола. Одна реплика до 35 слов, без нового спора и без объяснения кода. '
                    'Не говори за человека или наблюдателя и не повторяй её фразу.',
                reply_context=[],sticker_keys=set(),use_book=False,
                policy={'enabled':True,'confidence':70,'stickers_enabled':False,'model':''})
            text=response.get('text','') if response.get('action')!='silent' else ''
            if not text or len(text)>450 or len(text.split())>45 or not live():return False
            if not self.worker.conversation.glitch_recover_from_scout(offer['chat'],offer['episode']):return False
            # The state change precedes delivery. A lost acknowledgement must
            # never cause a second recovery message or resurrect the glitch.
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE scout_glitch_retorts SET status="recovering" WHERE owner=? AND chat=? AND episode=?',
                    (owner,offer['chat'],offer['episode']))
            sent=await asyncio.to_thread(self.worker.send_reply,self.store.setting('telegram_token'),offer['chat'],message_id,text,
                                        queue_on_failure=False,reply_to_id=message_id)
            number=int(sent.get('message_id',0)) if isinstance(sent,dict) else 0
            if not number:raise ValueError('Ответ после сбоя не подтверждён.')
            self.scout.dialogue.delivered(offer['chat'],number,'main',text,message_id)
            self.scout.history.schedule(destination,number)
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE scout_glitch_retorts SET status="sent" WHERE owner=? AND chat=? AND episode=?',
                    (owner,offer['chat'],offer['episode']))
            self.scout.event('scout_glitch_recovered','Основная помощник вышла из сбоя после уместного подкола наблюдателя.',offer['chat'],answer=text)
            return True
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.scout.event('scout_glitch_recovery_error','Ответ на подкол не завершён: '+type(exc).__name__+'. Автоматического повтора нет.',offer['chat'])
            current=self.worker.conversation.glitch_active(offer['chat'])
            return not current or current.get('id')!=offer['episode']

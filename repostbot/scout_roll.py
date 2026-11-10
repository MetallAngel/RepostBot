"""The scout handles only /roll, and only in its confirmed revealed profile."""
import asyncio
import re
import time

from .dice import parse_roll,roll,format_result,is_native_d6,RollResult


class ScoutRoll:
    def __init__(self,scout):
        self.scout,self.worker,self.store=scout,scout.worker,scout.store
        with self.store.lock,self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS scout_roll_jobs (
                owner TEXT NOT NULL,chat TEXT NOT NULL,source_id INTEGER NOT NULL,status TEXT NOT NULL,
                at INTEGER NOT NULL,PRIMARY KEY(owner,chat,source_id))''')

    def delivered(self,message,target,number,body):
        self.store.mark_outbound(message['chat'],number)
        self.scout.dialogue.delivered(message['chat'],number,'scout',body,message['id'],root=message.get('root',0))
        self.scout.history.schedule(target,number)

    async def reaction(self,message,scene,result,native=False):
        context=scene or message.get('parent_text') or message.get('post') or 'Обычный бросок кубика по просьбе человека.'
        question=str(context)[:700]+'\nФактический результат: '+format_result(result)
        try:
            decision=await asyncio.to_thread(self.worker.conversation.generate,'telegram','scout:roll:'+message['chat'],
                message['person'],message['name'],question,preview=True,passive=True,addressed=True,glitch_immune=True,
                speaker_identity=self.scout.model_identity(),persona_prompt=self.scout.settings()['prompt']+'\nТы раскрыта и уже бросила кубик. '
                    'Ответь одной короткой живой репликой до 20 слов на приведённый настоящий результат и контекст. '
                    +('Кубик d6 уже показан анимацией, не дублируй строку с числами. ' if native else 'Числа будут показаны отдельной строкой, не повторяй их. ')+
                    'Не меняй числа, не бросай заново, не выдумывай действия человека и не меняй маскировку.',
                role_chat_id=self.scout.role_chat(message['chat']),role_assignment=False,
                reply_context=[],sticker_keys=set(),use_book=self.scout.settings()['book'],
                policy={'enabled':True,'confidence':0,'stickers_enabled':False,'model':''})
            comment=decision.get('text','') if isinstance(decision,dict) and decision.get('action')!='silent' else ''
            if isinstance(comment,str) and comment.strip() and len(comment)<=220:
                return self.worker.community.redact_terms(comment.strip())
        except Exception:pass
        # Keep the delivered roll useful even with an unavailable text model.
        value=result.total-result.spec.modifier
        if result.spec.count==1 and value==1:return 'Кубик выбрал самый сложный путь. Придётся выручать.'
        if result.spec.count==1 and value==result.spec.sides:return 'Отличный бросок. Можно вылезать из профиль B уверенно.'
        return 'Кубик решил: действуем осторожно.'

    async def handle(self,message,target,epoch):
        command=re.match(r'^\s*/roll(?:@([A-Za-z0-9_]+))?(?:\s|$)',message.get('text',''),re.I)
        if not command or message.get('forwarded') or message.get('bot'):return
        username=command.group(1)
        if username and username.casefold()!=self.scout.actor.get('username','').casefold():return
        account,owner=self.scout.account,self.scout.actor.get('id')
        def live():
            state=self.scout.disguise.state()
            return bool(self.scout.account is account and owner==self.scout.actor.get('id')
                and self.scout.live(target,epoch) and state.get('mode')=='scout' and state.get('desired','') in ('','scout'))
        if not live():
            if self.scout.live(target,epoch):
                self.scout.last_skip='Бросок пропущен: /roll доступен только в раскрытом облике наблюдателя; сейчас '+(self.scout.disguise.state().get('mode') or 'облик ещё не подтверждён')+'.'
                self.scout.event('roll_skipped',self.scout.last_skip,message['chat'])
            return
        if not username and self.worker.bot_id:
            try:
                member=await account.group_member(target,str(self.worker.bot_id))
            except Exception:member=None
            # Uncertain presence also yields the command to the primary bot.
            if member is not False:return
        if not live():return
        with self.store.lock,self.store.db:
            claimed=self.store.db.execute('INSERT OR IGNORE INTO scout_roll_jobs VALUES (?,?,?,?,?)',
                (owner,message['chat'],message['id'],'preparing',int(time.time()))).rowcount
        if not claimed:return
        status='cancelled';native_id=0
        try:
            spec,scene=parse_roll(message['text'])
            if self.scout.disguise.lock is None:self.scout.disguise.lock=asyncio.Lock()
            result=RollResult(spec,(),0,None,'')
            if is_native_d6(result):
                async with self.scout.disguise.lock:
                    if not live():return
                    status='uncertain'
                    native=await account.send_dice(target,reply_id=message['id'],root=message.get('root',0))
                native_id=int(native.get('id') or 0);value=int(native.get('value') or 0)
                if not native_id or not 1<=value<=6:raise ValueError('Бросок не подтверждён.')
                result=RollResult(spec,(value,),value,None,'');status='sent'
                self.delivered(message,target,native_id,format_result(result))
                self.scout.event('scout_roll_dice','Telegram подтвердил анимированный кубик и его значение.',message['chat'],answer=format_result(result))
            else:result=roll(spec)
            body=format_result(result)
            comment=await self.reaction(message,scene,result,native=bool(native_id))
            text=comment if native_id else body+'\n'+comment
            async with self.scout.disguise.lock:
                if not live():return
                status='uncertain'
                number=await account.send_text(target,text,reply_id=native_id or message['id'],root=message.get('root',0))
            if not number:raise ValueError('Бросок не подтверждён.')
            status='sent'
            self.delivered(message,target,number,text)
            self.scout.event('scout_roll','наблюдатель бросила кубики после раскрытия.',message['chat'],answer=body+'\n'+comment)
        except asyncio.CancelledError:raise
        except Exception as exc:
            reason='Кубик отправлен, но реакция не завершена: ' if native_id else 'Бросок не завершён: '
            self.scout.event('scout_roll_error',reason+type(exc).__name__+'. Автоматического повтора нет.',message['chat'])
        finally:
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE scout_roll_jobs SET status=? WHERE owner=? AND chat=? AND source_id=?',
                    (status,owner,message['chat'],message['id']))

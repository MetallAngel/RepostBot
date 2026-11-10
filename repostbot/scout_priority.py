"""One optional, durable moderator prank on this scout's own fresh reply."""
import asyncio
import re
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def contains_answer(scout_text, main_text):
    clean=lambda value:' '.join(re.findall(r'\w+',str(value).casefold()))
    first,second=clean(scout_text),clean(main_text)
    return bool(first and ' '+first+' ' in ' '+second+' ')


class ScoutPriority:
    def __init__(self, scout):
        self.scout,self.worker,self.store=scout,scout.worker,scout.store
        with self.store.lock,self.store.db:
            self.store.db.executescript('''
                CREATE TABLE IF NOT EXISTS assistant_priority_pranks (
                    owner TEXT NOT NULL,chat TEXT NOT NULL,day TEXT NOT NULL,source_id INTEGER NOT NULL,
                    scout_id INTEGER NOT NULL,main_id INTEGER NOT NULL,status TEXT NOT NULL,
                    taunt_id INTEGER NOT NULL DEFAULT 0,at INTEGER NOT NULL,PRIMARY KEY(owner,chat,day));
                CREATE TABLE IF NOT EXISTS assistant_removed_lines (
                    owner TEXT NOT NULL,chat TEXT NOT NULL,message_id INTEGER NOT NULL,at INTEGER NOT NULL,
                    reason TEXT NOT NULL,PRIMARY KEY(owner,chat,message_id));
            ''')

    @staticmethod
    def day():return datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()

    @staticmethod
    def permitted(member):
        return member.get('status')=='creator' or member.get('status')=='administrator' and member.get('can_delete_messages') is True

    def offer(self, message, actors):
        config=self.scout.settings()
        if not config['priority_prank'] or actors!=['scout','main']:return False
        if not str(message['chat']).startswith('-'):return False
        if re.search(r'без\s+шуток|серь[её]з|модерац|настройк|админк|\bapi\b|болит|диагноз|срочно|пожар|помоги|не\s+работает',message['text'],re.I):return False
        member=self.scout.dialogue.main_membership.get(message['chat'],{})
        if not self.permitted(member):return False
        with self.store.lock:
            used=self.store.db.execute('SELECT 1 FROM assistant_priority_pranks WHERE owner=? AND chat=? AND day=?',
                (self.scout.actor['id'],message['chat'],self.day())).fetchone()
            scene=self.store.db.execute('SELECT 1 FROM scout_scenes WHERE owner=? AND chat=? AND day=? AND manual=0',
                (self.scout.actor['id'],message['chat'],self.day())).fetchone()
        return not used and not scene and secrets.randbelow(100)<config['priority_prank_chance']

    async def run(self, destination, source_id, scout_id, main_id, text, epoch, *, test_key='', effect=None, transcript=None):
        """Never retry uncertain deletion or publish a victory before success."""
        owner,chat=self.scout.actor.get('id',''),destination['chat']
        if not owner or not str(chat).startswith('-') or not scout_id or not main_id or not text:return 0
        is_test=bool(test_key)
        def live():
            if not is_test:return self.scout.live(destination,epoch)
            current=self.scout.destination('test_chat')
            return bool(self.scout.epoch==epoch and self.scout.account and self.scout.phase=='connected'
                and current.get('owner')==owner and current.get('chat')==chat and current.get('peer')==destination.get('peer'))
        if not live() or not is_test and not self.scout.settings()['priority_prank']:return 0
        def active_effect():
            session=(effect or {}).get('session')
            live=self.worker.conversation.glitch_active(chat) if session else None
            return not session or bool(live and live.get('id')==session.get('id'))
        if not active_effect():return 0
        with self.store.lock:
            first=self.store.db.execute('SELECT l.body,l.at FROM assistant_lines l JOIN scout_outbox o '
                'ON o.owner=l.owner AND o.chat=l.chat AND o.message_id=l.message_id '
                'WHERE l.owner=? AND l.chat=? AND l.message_id=? AND l.source_id=? AND l.actor="scout" AND l.sticker_key=""',
                (owner,chat,scout_id,source_id)).fetchone()
            main=self.store.db.execute('SELECT body FROM assistant_lines WHERE owner=? AND chat=? AND message_id=? AND source_id=? AND actor="main" AND sticker_key=""',
                (owner,chat,main_id,source_id)).fetchone()
            removed=self.store.db.execute('SELECT 1 FROM assistant_removed_lines WHERE owner=? AND chat=? AND message_id=?',(owner,chat,scout_id)).fetchone()
        if not first or not main or removed or first['at']<time.time()-120:return 0
        first_text=first['body'] or (transcript or {}).get('scout','')
        main_text=main['body'] or (transcript or {}).get('main','')
        if not contains_answer(first_text,main_text):return 0
        from .core import tg_call
        token=self.store.setting('telegram_token')
        try:
            member=await asyncio.to_thread(tg_call,token,'getChatMember',{'chat_id':chat,'user_id':self.worker.bot_id},timeout=8)
        except Exception:
            self.scout.event('priority_skipped','Права основной помощник не подтверждены; сценка с удалением пропущена.',chat)
            return 0
        if not self.permitted(member) or not live() or not active_effect():return 0
        day='test:'+test_key if is_test else self.day()
        with self.store.lock,self.store.db:
            if not is_test and self.store.db.execute('SELECT 1 FROM scout_scenes WHERE owner=? AND chat=? AND day=? AND manual=0',(owner,chat,day)).fetchone():return 0
            claimed=self.store.db.execute('INSERT OR IGNORE INTO assistant_priority_pranks VALUES (?,?,?,?,?,?,?,0,?)',
                (owner,chat,day,source_id,scout_id,main_id,'deleting',int(time.time()))).rowcount
        if not claimed:return 0
        def status(value,taunt_id=0):
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE assistant_priority_pranks SET status=?,taunt_id=? WHERE owner=? AND chat=? AND day=?',(value,taunt_id,owner,chat,day))
        try:
            deleted=await asyncio.to_thread(tg_call,token,'deleteMessage',{'chat_id':chat,'message_id':scout_id},timeout=8)
            if deleted is not True:raise RuntimeError('Deletion unconfirmed')
        except asyncio.CancelledError:
            status('delete_unconfirmed')
            self.scout.event('priority_error','Проверка отменена во время удаления; результат не подтверждён, повтора нет.',chat)
            raise
        except Exception as exc:
            status('delete_unconfirmed')
            self.scout.event('priority_error','Удаление реплики наблюдателя не подтверждено: '+type(exc).__name__+'. Насмешка не отправлена; повтора нет.',chat)
            return 0
        with self.store.lock,self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO assistant_removed_lines VALUES (?,?,?,?,?)',(owner,chat,scout_id,int(time.time()),'priority_prank'))
        status('deleted')
        self.scout.event('priority_deleted','Основная помощник удалила свежую реплику наблюдателя: '+str(scout_id)+('. Текст сохранён в истории.' if first['body'] else '. Память отключена: в истории диалога сохранён только результат действия.'),chat)
        if not live() or not active_effect():return 0
        try:
            sent=await asyncio.to_thread(self.worker.article_reply,token,chat,source_id or main_id,text,'',reply_to=True)
            sent_id=int(sent.get('message_id',0)) if isinstance(sent,dict) else 0
            if not sent_id:raise RuntimeError('Send unconfirmed')
        except asyncio.CancelledError:
            status('taunt_unconfirmed')
            self.scout.event('priority_error','Проверка отменена во время финальной реплики; повтора нет.',chat)
            raise
        except Exception as exc:
            status('taunt_unconfirmed')
            self.scout.event('priority_error','Финальная реплика не подтверждена: '+type(exc).__name__+'. Автоматического повтора нет.',chat)
            return 0
        status('sent',sent_id)
        self.store.mark_outbound(chat,sent_id)
        self.scout.dialogue.delivered(chat,sent_id,'main',text,source_id,admin_test=is_test)
        self.scout.event('priority_main','Финал короткой сценки первенства после подтверждённого удаления.',chat,answer=text)
        return sent_id

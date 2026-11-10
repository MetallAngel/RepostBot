"""One Telegram episode; its origin keeps timers, quests and message anchors."""
import json
import re
import time


class GlitchScope:
    pointer_key='conversation_global_glitch_telegram'

    def __init__(self,conversation):
        self.conversation,self.store=conversation,conversation.store

    @staticmethod
    def telegram(chat):
        return bool(re.fullmatch(r'-?[1-9]\d*',str(chat)))

    def pointer(self):
        try:
            value=json.loads(self.store.state(self.pointer_key,'{}'))
            return value if isinstance(value,dict) else {}
        except (ValueError,TypeError):return {}

    def resolve(self,chat):
        if not self.telegram(chat):return str(chat)
        pointer=self.pointer()
        origin=str(pointer.get('chat',''))
        if self.telegram(origin):
            try:
                state=json.loads(self.store.state('conversation_glitch:'+origin,'{}'))
                if state.get('id')==pointer.get('id') and state.get('id'):return origin
            except (ValueError,TypeError,AttributeError):pass
        return str(chat)

    def begin(self,chat,episode):
        if self.telegram(chat):
            self.store.set_state(self.pointer_key,json.dumps({'chat':str(chat),'id':episode}))

    @staticmethod
    def source(state,chat,number):
        number=int(number or 0)
        if not number or not state.get('global') or str(chat)==str(state.get('chat')):return number
        peer=int(chat)
        return (1<<96)+(abs(peer)*2+int(peer<0))*(1<<32)+number

    def migrate(self):
        if self.store.state('glitch_global_v208')=='1':return
        now=int(time.time());records=[]
        with self.store.lock,self.store.db:
            for row in self.store.db.execute("SELECT key,value FROM state WHERE key LIKE 'conversation_glitch:%'"):
                chat=row['key'].split(':',1)[1]
                if not self.telegram(chat):continue
                try:state=json.loads(row['value'])
                except (TypeError,ValueError):continue
                if isinstance(state,dict) and state.get('id'):records.append((chat,state))
            live=[record for record in records if record[1].get('phase') in ('active','sending')
                  and self.conversation._glitch_hard_due(record[1],now,strict=True)>now]
            winner=max(live,key=lambda record:int(record[1].get('created_at',0)),default=None)
            for chat,state in records:
                if winner and chat==winner[0]:
                    state['global']=True;state['chat']=chat
                    state['style_signature']=self.conversation._glitch_signature(self.conversation.settings())
                    self.store.set_state('conversation_glitch:'+chat,json.dumps(state,ensure_ascii=False))
                    self.begin(chat,state['id'])
                elif state.get('phase') in ('pending','active','sending'):
                    state.update(phase='cooldown',cooldown_until=now,inflight_ids=[],expired_at=now,
                                 expiration_reason='global_scope_migration')
                    self.store.set_state('conversation_glitch:'+chat,json.dumps(state,ensure_ascii=False))
        self.store.set_state('glitch_global_v208','1')

    def current(self):
        pointer=self.pointer()
        if not self.telegram(pointer.get('chat','')):return None
        return self.conversation.glitch_active(pointer['chat'])

    def manual_active(self):
        pointer=self.pointer()
        if not self.telegram(pointer.get('chat','')):return False
        try:state=json.loads(self.store.state('conversation_glitch:'+pointer['chat'],'{}'))
        except (ValueError,TypeError):return False
        return bool(state.get('id')==pointer.get('id') and state.get('manual_test')
                    and state.get('phase') in ('pending','active','sending'))

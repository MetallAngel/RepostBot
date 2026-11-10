"""Optional per-user extensions; no preassigned people or conversation history."""
import re
OWNER='channel_owner'
KEEPER='community_helper'
MEMBER='community_host'
MEMBER_ID=''
DEFS={OWNER:('Владелец канала','positive',-100,100,set(),'Учитывай явно назначенную владельцем роль.'),
      KEEPER:('Помощник сообщества','positive',-100,100,set(),'Общайся уважительно и по существу.'),
      MEMBER:('Ведущий сообщества','positive',-100,100,set(),'Роль не даёт дополнительных прав доступа.')}
SERIOUS=re.compile(r'ошибк|настройк|серьёзно',re.I)
OWNER_NAME=KEEPER_NAME=SWEATER=ASK_JOKE=NAMED_JOKE=re.compile(r'(?!)')
TOPICS=MEMBER_GUIDES={}
FROG_JOKE=PUPA_JOKE=''
class PersonalRoles:
    def __init__(self,social):self.social,self.worker,self.store=social,social.worker,social.store
    def assignments(self,*args,**kwargs):return []
    def ready(self,*args,**kwargs):return False
    def observe(self,*args,**kwargs):return None
    def context(self,*args,**kwargs):return ''
    def record_reply(self,*args,**kwargs):return None
    def followup(self,*args,**kwargs):return False

"""One optional daily scene; people can join without starting bot loops."""
import asyncio
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .glitch_context import topic
from .reply_context import requested_subject
REACTION_PROMPT = 'Ты помощник сообщества. Отвечай по-русски, кратко, по текущему вопросу. Используй только подтверждённый контекст. Не выдумывай биографии, события, права или действия участников.'

class ScoutScenes:

    def __init__(self, scout):
        self.scout, self.store, self.worker = (scout, scout.store, scout.worker)
        with self.store.lock, self.store.db:
            self.store.db.executescript('\n                CREATE TABLE IF NOT EXISTS scout_scenes (\n                    owner TEXT NOT NULL, chat TEXT NOT NULL, day TEXT NOT NULL, at INTEGER NOT NULL,\n                    expires_at INTEGER NOT NULL, is_test INTEGER NOT NULL, report_id INTEGER NOT NULL,\n                    report TEXT NOT NULL, human_replies INTEGER NOT NULL DEFAULT 0, last_reply INTEGER NOT NULL DEFAULT 0,\n                    PRIMARY KEY(owner,chat,day));\n                CREATE TABLE IF NOT EXISTS scout_scene_messages (\n                    owner TEXT NOT NULL, chat TEXT NOT NULL, message_id INTEGER NOT NULL, day TEXT NOT NULL,\n                    kind TEXT NOT NULL, PRIMARY KEY(owner,chat,message_id));\n                CREATE TABLE IF NOT EXISTS scout_scene_inputs (\n                    owner TEXT NOT NULL, chat TEXT NOT NULL, message_id INTEGER NOT NULL,\n                    PRIMARY KEY(owner,chat,message_id));\n                CREATE TABLE IF NOT EXISTS scout_scene_people (\n                    owner TEXT NOT NULL, chat TEXT NOT NULL, day TEXT NOT NULL, person TEXT NOT NULL,\n                    vote TEXT NOT NULL, PRIMARY KEY(owner,chat,day,person));\n            ')
            if 'manual' not in {row[1] for row in self.store.db.execute('PRAGMA table_info(scout_scenes)')}:
                self.store.db.execute('ALTER TABLE scout_scenes ADD COLUMN manual INTEGER NOT NULL DEFAULT 0')

    def begin(self, destination, report_id, report, is_test=False, day='', manual=False):
        owner = self.scout.actor.get('id', '')
        if not owner or destination.get('owner') != owner:
            return False
        if manual and self.scout.destination('test_chat').get('chat') != destination['chat']:
            return False
        now = int(time.time())
        day = day or datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
        with self.store.lock, self.store.db:
            if not manual and (not day.startswith('test:')) and self.store.db.execute('SELECT 1 FROM assistant_priority_pranks WHERE owner=? AND chat=? AND day=?', (owner, destination['chat'], day)).fetchone():
                return False
            added = self.store.db.execute('INSERT OR IGNORE INTO scout_scenes(owner,chat,day,at,expires_at,is_test,report_id,report,manual) VALUES (?,?,?,?,?,?,?,?,?)', (owner, destination['chat'], day, now, now + 3600, int(is_test), report_id, report, int(manual))).rowcount
            if added and manual:
                self.store.db.execute('UPDATE scout_scenes SET expires_at=? WHERE owner=? AND chat=? AND manual=1 AND day<>?', (now, owner, destination['chat'], day))
        if added:
            self.mark(destination['chat'], report_id, day, 'scout')
        return bool(added)

    def mark(self, chat, message_id, day, kind):
        if message_id:
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO scout_scene_messages VALUES (?,?,?,?,?)', (self.scout.actor.get('id', ''), str(chat), int(message_id), day, kind))

    def recent(self, chat):
        with self.store.lock:
            lines = [row[0] for row in self.store.db.execute('SELECT answer FROM scout_events WHERE chat=? AND action IN ("main_reaction","banter","scene_main","scene_scout","test_scene_main","test_scene_scout","duo_main","duo_scout") AND answer<>"" ORDER BY id DESC LIMIT 5', (str(chat),))]
        return '\nНедавние реплики двух помощник: не повторяй их подколы дословно или по смыслу.\n' + '\n'.join((value[:220] for value in lines)) if lines else ''

    def mark_current(self, chat, message_id, kind):
        with self.store.lock:
            row = self.store.db.execute('SELECT day FROM scout_scenes WHERE owner=? AND chat=? AND expires_at>? ORDER BY at DESC,day DESC LIMIT 1', (self.scout.actor.get('id', ''), str(chat), int(time.time()))).fetchone()
        if row:
            self.mark(chat, message_id, row['day'], kind)

    def interested(self, message):
        config = self.scout.settings()
        if not config['scene_people'] or not self.scout.actor or (not self.scout.account):
            return None
        if self.scout.phase != 'connected' or time.time() < self.scout.cooldown_until:
            return None
        chat = str((message.get('chat') or {}).get('id', ''))
        person = str((message.get('from') or {}).get('id', ''))
        if not person or person in (self.scout.actor['id'], str(self.worker.bot_id)) or (message.get('from') or {}).get('is_bot'):
            return None
        text = str(message.get('text') or message.get('caption') or '')
        if text.lstrip().startswith('/'):
            return None
        if topic(text) in ('serious', 'technical'):
            return None
        subject = requested_subject(text)
        if subject and (not re.search('помощник|профиль A|наблюдатель|главн|шпион|сообщество', subject, re.I)):
            return None
        with self.store.lock:
            scene = self.store.db.execute('SELECT * FROM scout_scenes WHERE owner=? AND chat=? AND expires_at>? AND human_replies<4 ORDER BY at DESC LIMIT 1', (self.scout.actor['id'], chat, int(time.time()))).fetchone()
            if not scene or (not scene['manual'] and (not config['main_reaction'])) or (not scene['is_test'] and (not config['enabled'])):
                return None
            parent_id = int((message.get('reply_to_message') or {}).get('message_id') or 0)
            parent = self.store.db.execute('SELECT kind FROM scout_scene_messages WHERE owner=? AND chat=? AND message_id=? AND day=?', (scene['owner'], chat, parent_id, scene['day'])).fetchone() if parent_id else None
        voting = re.search('\\bя\\s+за\\s+(?:наблюдатель|шпионк|основн|обеих|профиль A)|\\bкто\\s+из\\s+вас.{0,20}(?:главн|старш)|какая\\s+помощник.{0,20}(?:главн|старш)', text, re.I)
        return dict(scene) if parent or voting else None

    def vote(self, text):
        if str(text).strip() == '📦':
            return 'box'
        if not re.search('\\bя\\s+за\\b|голосу|выбираю|поддерживаю', text, re.I):
            return ''
        if re.search('профиль A', text, re.I):
            return 'box'
        if re.search('обе|обоих|дружб', text, re.I):
            return 'both'
        if re.search('наблюдатель|шпион', text, re.I):
            return 'scout'
        if re.search('основн|главн', text, re.I):
            return 'main'
        return ''

    async def handle(self, message, destination, scene, epoch):
        if message.get('text', '').lstrip().startswith('/'):
            return
        if epoch != self.scout.epoch or not self.scout.actor or (not self.scout.settings()['scene_people']):
            return
        owner, chat, day = (scene['owner'], scene['chat'], scene['day'])
        vote = '' if message.get('forwarded') else self.vote(message['text'])
        now = int(time.time())
        with self.store.lock, self.store.db:
            added = self.store.db.execute('INSERT OR IGNORE INTO scout_scene_inputs VALUES (?,?,?)', (owner, chat, message['id'])).rowcount
            if not added:
                return
            if vote:
                self.store.db.execute('INSERT INTO scout_scene_people VALUES (?,?,?,?,?) ON CONFLICT(owner,chat,day,person) DO UPDATE SET vote=excluded.vote', (owner, chat, day, message['person'], vote))
            current = self.store.db.execute('SELECT * FROM scout_scenes WHERE owner=? AND chat=? AND day=?', (owner, chat, day)).fetchone()
            if not current or current['expires_at'] <= now or current['human_replies'] >= 4 or (current['last_reply'] > now - 30):
                return
            self.store.db.execute('UPDATE scout_scenes SET human_replies=human_replies+1,last_reply=? WHERE owner=? AND chat=? AND day=?', (now, owner, chat, day))
            parent = self.store.db.execute('SELECT kind FROM scout_scene_messages WHERE owner=? AND chat=? AND message_id=?', (owner, chat, message.get('parent_id', 0))).fetchone()
            votes = {row[0]: row[1] for row in self.store.db.execute('SELECT vote,COUNT(*) FROM scout_scene_people WHERE owner=? AND chat=? AND day=? GROUP BY vote', (owner, chat, day))}
        actor = parent[0] if parent else 'main' if vote == 'scout' else 'scout' if vote == 'main' else 'scout' if current['human_replies'] % 2 == 0 else 'main'
        config = self.scout.settings()
        home = self.scout.destination()
        roles = self.scout.role_chat(chat) if actor == 'scout' else chat
        scope = 'scout:scene:' + owner + ':' + chat + ':' + day
        self.worker.community.see_user('telegram', message['person'], message['name'], message.get('username', ''), chat)
        if config['book'] and (not message.get('forwarded')):
            self.worker.community.observe('telegram', message['person'], message['name'], chat + ':' + str(message['id']), message['text'], message['at'], notify_allowed=False, severe_notify_allowed=False, transition_notify_allowed=False)
        if config['remember'] and (not message.get('forwarded')):
            self.store.remember_chat_message(chat, message['person'], message['id'], message['name'], message['text'], at=message['at'])
        if self.scout.dialogue.collective(message):
            self.scout.dialogue.observe(message, 'both')
            image = await self.scout.account.image(message['_message']) if message.get('media') and message.get('_message') else None
            await self.scout.dialogue.together(message, {**destination, '_duo_scene': dict(current), 'comments_only': False}, epoch, image=image)
            return
        disguise_target = {**destination, '_duo_scene': dict(current), 'comments_only': False}
        await self.scout.disguise.before_reply(message, disguise_target, epoch)
        persona = config['prompt'] if actor == 'scout' else config['reaction_prompt']
        effect = self.scout.dialogue.main_effect(message, start=False) if actor == 'main' else {}
        persona += effect.get('overlay', '')
        persona += self.scout.style.context(actor, chat, text=message['text'], joint=True)
        persona += self.scout.disguise.context()
        persona += '\n' + config['duo_prompt']
        persona += '\nУчастник вмешался в игровой спор двух помощник. Ответь на ЕГО слова одной короткой репликой, только за свою помощник. Можно принять голос, подколоть другую помощник или предложить человеку своё шуточное решение. Реальные роли и права людей от этого не меняются. Не придумывай чужие ответы, не объявляй чужую реальную должность и не начинай новый автоматический обмен. Голоса в игре: ' + json.dumps(votes, ensure_ascii=False) + '\nСуть донесения: ' + scene['report'][:900]
        persona += self.recent(chat)
        stickers = self.scout.available_stickers() if actor == 'scout' else self.worker.sticker_catalog()
        refs = [{'kind': 'reply', 'author': 'помощник', 'bot': False, 'text': message.get('parent', '') or scene['report']}]
        image = await self.scout.account.image(message['_message']) if message.get('media') and message.get('_message') else None
        decision = await asyncio.to_thread(self.worker.conversation.generate, 'telegram', scope, message['person'], message['name'], self.scout.message_prompt(message), image_data=image, passive=True, addressed=True, speaker_identity=self.scout.model_identity(actor), persona_prompt=persona, role_chat_id=roles, reply_context=refs, sticker_keys=set(stickers) if config['stickers'] else set(), use_book=config['book'], role_assignment=actor == 'main' or roles == chat, disguise=actor == 'scout', glitch_immune=actor == 'scout', policy={'enabled': True, 'confidence': config['confidence'], 'stickers_enabled': config['stickers'], 'model': ''})
        if epoch != self.scout.epoch or not self.scout.account or (not self.scout.settings()['scene_people']):
            return
        if effect.get('forced_text'):
            decision = {**decision, 'text': effect['forced_text'], 'sticker_key': ''}
        text, key, sent = (decision['text'], decision['sticker_key'], 0)
        if actor == 'scout':
            if text:
                sent = await self.scout.account.send_text(destination, text, reply_id=message['id'])
            elif key and key in stickers:
                sent = await self.scout.send_catalog_sticker(destination, key, message['id'], epoch)
        else:
            token = self.store.setting('telegram_token')
            if token and self.worker.bot_id:
                answer = text or ('[[STICKER:' + key + ']]' if key in stickers else '')
                if answer:
                    result = await asyncio.to_thread(self.worker.send_reply, token, chat, message['id'], answer)
                    sent = int(result.get('message_id', 0)) if isinstance(result, dict) else 0
        if sent:
            self.store.mark_outbound(chat, sent)
            self.scout.dialogue.delivered(chat, sent, actor, text or '[[STICKER:' + key + ']]', message['id'])
            self.mark(chat, sent, day, actor)
            if config['remember']:
                self.store.remember_ai_context(scope, message['person'], message['text'], text or '[[STICKER:' + key + ']]', message['id'])
            self.scout.event('scene_' + actor, 'Один ответ участнику в ежедневной сценке.', chat, scene['report_id'], message['person'], text or key)
            if actor == 'scout':
                await self.scout.disguise.after_reply(message, disguise_target, epoch, decision)

"""Bounded, attributed Telegram reply/forward context, without another model call."""

import json
import re
import time


MAX_ITEMS = 3
MAX_CONTEXT = 1500


def requested_subject(question):
    """A named new subject is stronger evidence than an old reply chain."""
    text = str(question or '').casefold()
    match = re.search(
        r'\b(?:кто\s+так(?:ой|ая|ие)|что\s+такое|'
        r'(?:расскажи|расскажите|пошути|пошутите|шутк[ауи]|анекдот)'
        r'(?:\s+(?:нам|мне|ещ[её]|лучше))?\s+(?:про|об?|на\s+тему))\s+([^?!,;\n]+)', text)
    if not match:
        return ''
    subject = match[1].strip(' «»"')
    # These requests still need the quoted message to resolve their referent.
    if re.match(r'(?:эт[оауи]\w*|он[аи]?|н(?:его|е[её]|[её]м|[её]й|ими?|их)|тако[йе]\w*|его|е[её])\b', subject):
        return ''
    return subject


def subject_terms(question):
    subject = requested_subject(question)
    stop = {'пожалуйста', 'лучше', 'теперь', 'давай', 'помощник', 'помощник'}
    return {word[:5] for word in re.findall(r'[а-яёa-z0-9]{3,}', subject) if word not in stop}


def changes_topic(question, items):
    """Keep replies to humans/forwards; detach an obsolete bot conversation."""
    if not items or not items[0].get('bot'):
        return False
    terms = subject_terms(question)
    if not terms:
        return False
    previous = ' '.join(item.get('quote') or item.get('text', '') for item in items)
    found = {word[:5] for word in re.findall(r'[а-яёa-z0-9]{3,}', previous.casefold())}
    # A question about the quoted subject is still a follow-up.
    return not bool(terms & found)


def compact(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def author(message):
    origin = message.get('forward_origin') or {}
    # Telegram may hide the original sender; do not invent their user ID.
    user = origin.get('sender_user') or message.get('forward_from') or {}
    channel = origin.get('chat') or origin.get('sender_chat') or message.get('forward_from_chat') or {}
    if origin.get('type') == 'hidden_user' or message.get('forward_sender_name'):
        return compact(origin.get('sender_user_name') or message.get('forward_sender_name'), 100)
    if not user and not channel:
        user = message.get('from') or {}
        channel = message.get('sender_chat') or {}
    name = channel.get('title') or ' '.join(filter(None, (user.get('first_name'), user.get('last_name'))))
    login = user.get('username') or channel.get('username')
    return compact((name + (' (@' + login + ')' if login else '')) or 'Автор не указан', 100)


def forwarded_by(message):
    """Name the person who carried a forwarded message into this chat."""
    if not any(message.get(key) for key in ('forward_origin', 'forward_from', 'forward_sender_name', 'forward_from_chat')):
        return ''
    user = message.get('from') or {}
    sender = message.get('sender_chat') or {}
    name = sender.get('title') or ' '.join(filter(None, (user.get('first_name'), user.get('last_name'))))
    return compact(name or user.get('username') or user.get('id') or 'неизвестный участник', 100)


def reference_key(message):
    """Do not batch questions about different referenced messages together."""
    reply = message.get('reply_to_message') or {}
    external = message.get('external_reply') or {}
    if reply.get('message_id'):
        return ('reply', str(reply['message_id']))
    if external:
        return ('external', str((external.get('chat') or {}).get('id', '')),
                str(external.get('message_id', '')), compact((message.get('quote') or {}).get('text'), 200))
    if message.get('forward_origin') or message.get('forward_from') or message.get('forward_sender_name') or message.get('forward_from_chat'):
        return ('forward', str(message.get('message_id', '')))
    return None


class ReplyContext:
    def __init__(self, store):
        self.store = store

    def snapshot(self, message, kind, bot_id=None, quote=''):
        text = compact(message.get('text') or message.get('caption'), 650)
        if not text and message.get('rich_message'):
            from .rich_reply import rich_message_text
            text = compact(rich_message_text(message), 650)
        media = next((key for key in ('photo', 'sticker', 'animation', 'video', 'document', 'voice', 'audio') if message.get(key)), '')
        if not text and media:
            text = {'photo': '[Фото без подписи]', 'sticker': '[Стикер без подписи]',
                    'animation': '[GIF без подписи]', 'video': '[Видео без подписи]'}.get(media, '[Вложение без текста]')
        picture = (message.get('photo') or [{}])[-1] if message.get('photo') else (message.get('document') or {})
        media_picture = (message.get(media) or {}) if media in ('sticker', 'animation', 'video', 'voice', 'audio') else picture
        media_file_id = (media_picture.get('file_id') if isinstance(media_picture, dict) else '') or (picture.get('file_id') if isinstance(picture, dict) else '')
        is_forwarded = any(message.get(key) for key in ('forward_origin', 'forward_from', 'forward_sender_name', 'forward_from_chat'))
        return {'kind': kind, 'author': author(message), 'forwarded_by': forwarded_by(message) if is_forwarded else '',
                'forwarded': is_forwarded, 'text': text, 'quote': compact(quote, 350),
                'message_id': int(message.get('message_id') or 0),
                'bot': bool(bot_id is not None and (message.get('from') or {}).get('id') == bot_id),
                'media': media, 'media_file_id': media_file_id or '',
                'media_group_id': str(message.get('media_group_id') or '')}

    def immediate(self, message, bot_id=None):
        items = []
        batch = message.get('_batch_messages') or []
        if batch:
            # Store every member of a Telegram album under each member's ID.
            # The text context remains bounded later, while image generation
            # can still retrieve all source file IDs for an edit request.
            for member in batch[:10]:
                items.append(self.snapshot(member, 'album', bot_id))
        reply = message.get('reply_to_message') or {}
        quote = (message.get('quote') or {}).get('text', '')
        if reply:
            items.append(self.snapshot(reply, 'reply', bot_id, quote))
        elif message.get('external_reply'):
            external = message['external_reply']
            item = self.snapshot({'forward_origin': external.get('origin') or {}, **{
                key: value for key, value in external.items() if key in ('photo', 'sticker', 'animation', 'video', 'document', 'voice', 'audio')
            }}, 'external', quote=quote)
            # ExternalReplyInfo has no original text. Only use the supplied quote.
            if not item['text'] and not quote:
                item['text'] = '[Текст исходного сообщения Telegram не передал]'
            items.append(item)
        elif quote:
            items.append(self.snapshot({}, 'quote', quote=quote))
        if any(message.get(key) for key in ('forward_origin', 'forward_from', 'forward_sender_name', 'forward_from_chat')):
            forwarded = dict(message)
            if '_forward_text' in message:
                forwarded['text'] = message['_forward_text']
                forwarded.pop('caption', None)
            items.append(self.snapshot(forwarded, 'forward', bot_id))
        return items

    def media_file_ids(self, chat, message_id=0, message=None, bot_id=None, limit=10):
        """Return source Telegram image IDs attached to a reply or album."""
        chat = self.store.canonical_chat_id(chat)
        if message is not None:
            items = self.immediate(message, bot_id)
            parent = int((message.get('reply_to_message') or {}).get('message_id') or 0)
        elif message_id:
            items, parent = self._stored(chat, message_id)
        else:
            return []
        result, seen = [], set()
        for _ in range(MAX_ITEMS):
            for item in items:
                file_id = str(item.get('media_file_id') or '') if isinstance(item, dict) else ''
                if file_id and file_id not in seen:
                    seen.add(file_id)
                    result.append(file_id)
                    if len(result) >= limit:
                        return result
            if not parent:
                break
            parent_items, next_parent = self._stored(chat, parent)
            if parent in seen:
                break
            seen.add(parent)
            items, parent = parent_items, next_parent
        return result

    def remember(self, message, bot_id=None):
        items = self.immediate(message, bot_id)
        if not items or not message.get('message_id'):
            return
        chat = self.store.canonical_chat_id(message['chat']['id'])
        parent = int((message.get('reply_to_message') or {}).get('message_id') or 0)
        now = int(time.time())
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO telegram_reply_links(chat,message_id,parent_id,context,at) VALUES (?,?,?,?,?)',
                                  (chat, int(message['message_id']), parent, json.dumps(items, ensure_ascii=False), min(int(message.get('date') or now), now)))
            cutoff = self.store.memory_cutoff()
            if cutoff >= 0:
                self.store.db.execute('DELETE FROM telegram_reply_links WHERE at<?', (cutoff,))

    def _stored(self, chat, message_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT context,parent_id FROM telegram_reply_links WHERE chat=? AND message_id=? AND at>=?',
                                        (chat, int(message_id), self.store.memory_cutoff())).fetchone()
        if not row:
            return [], 0
        try:
            items = json.loads(row['context'])
        except (ValueError, TypeError):
            return [], 0
        return items if isinstance(items, list) else [], row['parent_id']

    def _fill_missing(self, chat, item):
        if item.get('text') or item.get('kind') != 'reply' or not item.get('message_id'):
            return item
        with self.store.lock:
            row = self.store.db.execute('SELECT name,text FROM chat_memory WHERE chat=? AND message_id=? AND at>=?',
                                        (chat, item['message_id'], self.store.memory_cutoff())).fetchone()
        if row:
            return {**item, 'author': row['name'], 'text': row['text']}
        return {**item, 'text': '[Текст исходного сообщения Telegram не передал]'}

    def get(self, chat, message_id=0, message=None, bot_id=None):
        chat = self.store.canonical_chat_id(chat)
        if message is not None:
            items = self.immediate(message, bot_id)
            parent = int((message.get('reply_to_message') or {}).get('message_id') or 0)
        elif message_id:
            items, parent = self._stored(chat, message_id)
        else:
            return []
        result, seen = [], {int(message_id or 0)}
        # Current exact target first; archived ancestors stay in this chat only.
        for _ in range(MAX_ITEMS):
            for item in items:
                if isinstance(item, dict):
                    result.append(self._fill_missing(chat, item))
            if not parent or parent in seen or len(result) >= MAX_ITEMS:
                break
            seen.add(parent)
            items, parent = self._stored(chat, parent)
        bounded, remaining = [], MAX_CONTEXT
        for item in result[:MAX_ITEMS]:
            entry = {**item, 'author': compact(item.get('author'), 100)}
            entry['quote'] = compact(item.get('quote'), min(350, remaining))
            remaining -= len(entry['quote'])
            entry['text'] = compact(item.get('text'), min(650, remaining))
            remaining -= len(entry['text'])
            if entry['text'] or entry['quote']:
                bounded.append(entry)
            if remaining <= 0:
                break
        return bounded

    @staticmethod
    def peer(items):
        return bool(items and not items[0].get('bot'))

    @staticmethod
    def topic(question, items):
        if changes_topic(question, items):
            return question
        return question + ' ' + ' '.join(item.get('quote') or item.get('text', '') for item in items)[:1000]

    @staticmethod
    def memory_question(question, items):
        if not items or changes_topic(question, items):
            return question
        item = items[0]
        text = item.get('quote') or item.get('text', '')
        if text and text not in question:
            return question[:1000] + '\n[Контекст от ' + compact(item.get('author'), 80) + ': ' + text[:300] + ']'
        return '[Контекст от ' + compact(item.get('author'), 80) + '] ' + question[:1000]

    @staticmethod
    def prompt(items):
        lines = []
        for item in reversed(items):
            relation = 'Пересланное сообщение' if item.get('kind') == 'forward' else 'Исходная реплика'
            line = relation + ', автор: ' + compact(item.get('author'), 100)
            if item.get('forwarded'):
                line += '; переслал(а) в чат: ' + compact(item.get('forwarded_by'), 100)
            if item.get('quote'):
                line += '\nВыбранная пользователем цитата: ' + item['quote']
            if item.get('text') and item['text'] != item.get('quote'):
                line += '\nТекст: ' + item['text']
            lines.append(line)
        return '\n\n'.join(lines)

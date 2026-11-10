"""User-account connection: discussions and optional private-inbox cleanup."""
import asyncio
import base64
import io
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit


def rpc_error(exc):
    names = {
        'PhoneNumberInvalidError': 'Проверьте номер телефона с кодом страны.',
        'PhoneCodeInvalidError': 'Неверный код входа.',
        'PhoneCodeExpiredError': 'Код истёк. Запросите новый код.',
        'PasswordHashInvalidError': 'Неверный пароль двухэтапной проверки.',
        'SessionRevokedError': 'Сессия отозвана. Войдите заново.',
        'AuthKeyUnregisteredError': 'Сессия недействительна. Войдите заново.',
        'ChatWriteForbiddenError': 'У аккаунта нет права писать в это обсуждение.',
        'UserBannedInChannelError': 'Аккаунту недоступно это обсуждение.',
        'InviteHashExpiredError': 'Ссылка-приглашение недействительна.',
        'ChannelsTooMuchError': 'Telegram ограничил число групп аккаунта.',
        'InviteRequestSentError': 'Заявка на вступление отправлена. После одобрения добавьте цель снова.',
        'ChannelPrivateError': 'Группа недоступна этому аккаунту.',
        'UserNotParticipantError': 'Аккаунт ещё не вступил в эту группу.',
    }
    if hasattr(exc, 'seconds'):
        return 'Telegram просит подождать %s с.' % max(1, min(86400, int(exc.seconds)))
    return names.get(type(exc).__name__, 'Telegram не выполнил запрос. Проверьте подключение и доступ аккаунта.')


class ScoutAccount:
    def __init__(self, api_id, api_hash, session, on_event):
        try:
            from telethon import TelegramClient, events, functions, types, utils
            from telethon.sessions import StringSession
        except ImportError:
            raise ValueError('Для наблюдателя нужен Telethon. Обновите зависимости штатным запускателем.') from None
        self.functions, self.types, self.utils = functions, types, utils
        self.client = TelegramClient(StringSession(session), int(api_id), api_hash, timeout=12,
                                     request_retries=1, connection_retries=1, flood_sleep_threshold=0)
        self.client.add_event_handler(on_event, events.NewMessage(incoming=True))
        self.documents, self.packs = {}, {}

    async def connect(self):
        await asyncio.wait_for(self.client.connect(), 20)
        if not await self.client.is_user_authorized():
            return None
        return await self.identity()

    async def identity(self):
        me = await self.client.get_me()
        if not me or getattr(me, 'bot', False):
            raise ValueError('Нужен отдельный пользовательский аккаунт Telegram, а не токен бота.')
        return {'id': str(me.id), 'username': me.username or '',
                'name': ' '.join(filter(None, (me.first_name, me.last_name)))[:100]}

    def session(self):
        return self.client.session.save()

    async def set_profile_name(self, name):
        result = await self.client(self.functions.account.UpdateProfileRequest(first_name=name, last_name=''))
        if not isinstance(result, self.types.User) or result.first_name != name or result.last_name:
            raise ValueError('Telegram не подтвердил новое имя и очистку фамилии.')

    @staticmethod
    def photo_data(photo):
        return {'id':int(photo.id), 'access_hash':int(photo.access_hash),
                'file_reference':base64.b64encode(photo.file_reference or b'').decode()}

    def input_photo(self, photo):
        return self.types.InputPhoto(int(photo['id']), int(photo['access_hash']),
                                     base64.b64decode(photo['file_reference']))

    async def upload_profile_avatar(self, mode, guard=lambda:True):
        from .scout_avatar_assets import avatar
        raw = io.BytesIO(avatar(mode))
        raw.name = 'assistant-' + mode + '.png'
        uploaded = await self.client.upload_file(raw)
        if not guard(): raise asyncio.CancelledError()
        result = await self.client(self.functions.photos.UploadProfilePhotoRequest(file=uploaded))
        photo = getattr(result, 'photo', None)
        if not isinstance(photo, self.types.Photo):
            raise ValueError('Telegram не подтвердил установку аватара.')
        return self.photo_data(photo)

    async def profile_avatar_is(self, photo):
        photos = await self.client.get_profile_photos('me', limit=1)
        return bool(photos and int(photos[0].id) == int(photo['id']))

    async def clear_profile_history(self, keep, guard=lambda:True):
        # The configured scout is a dedicated user account. Delete only its own
        # profile photos, after the replacement upload has been acknowledged.
        photos = await self.client.get_profile_photos('me', limit=100)
        if not photos or int(photos[0].id) != int(keep['id']):
            raise ValueError('Новый аватар больше не является текущим; история пока сохранена.')
        old = [photo for photo in photos if int(photo.id) != int(keep['id'])]
        if old:
            if not guard(): raise asyncio.CancelledError()
            deleted = await self.client(self.functions.photos.DeletePhotosRequest(
                [self.input_photo(self.photo_data(photo)) for photo in old]))
            if set(map(int, deleted or [])) != {int(photo.id) for photo in old}:
                raise ValueError('Telegram не подтвердил удаление прежних аватаров.')
        return len(photos) < 100

    async def code(self, phone):
        result = await self.client.send_code_request(phone)
        return result.phone_code_hash

    async def verify(self, phone, code, code_hash):
        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except Exception as exc:
            if type(exc).__name__ == 'SessionPasswordNeededError':
                return None
            raise
        return await self.identity()

    async def password(self, password):
        await self.client.sign_in(password=password)
        return await self.identity()

    async def disconnect(self, logout=False):
        if logout:
            await self.client.log_out()
        await self.client.disconnect()

    async def private_sender(self, event, own_id):
        """Resolve only an incoming human's private peer; never read its body."""
        msg = event.message
        peer = getattr(msg, 'peer_id', None)
        if (not isinstance(msg, self.types.Message) or not isinstance(peer, self.types.PeerUser)
                or msg.out or getattr(msg, 'action', None) or str(peer.user_id) == str(own_id)
                or str(event.chat_id) != str(peer.user_id)):
            return None
        sender = await event.get_sender()
        if (not isinstance(sender, self.types.User) or sender.bot or sender.is_self or sender.support
                or sender.id in (777000, 424000) or sender.id <= 0
                or sender.id != peer.user_id or str(sender.id) == str(own_id)):
            return None
        input_peer = await event.get_input_sender()
        if not isinstance(input_peer, self.types.InputPeerUser) or input_peer.user_id != sender.id:
            return None
        return {'person': str(sender.id), 'id': int(msg.id),
                'peer': {'kind': 'user', 'id': sender.id, 'access_hash': int(input_peer.access_hash)}}

    def private_peer(self, data):
        # This path deliberately cannot construct a group/channel/self peer.
        if data.get('kind') != 'user':
            raise ValueError('Операция доступна только для личного диалога.')
        user_id = int(data['id'])
        if user_id <= 0 or user_id in (777000, 424000):
            raise ValueError('Недопустимый адресат личного диалога.')
        return self.types.InputPeerUser(user_id, int(data['access_hash']))

    async def delete_private_dialog(self, peer):
        result = await self.client(self.functions.messages.DeleteHistoryRequest(
            self.private_peer(peer), max_id=0, just_clear=False, revoke=True))
        if result is False or result is None:
            raise ValueError('Telegram не подтвердил удаление личной истории.')
        # Telegram can require another request for a large history. Resume the
        # pending deletion until it confirms the dialog is gone.
        return not int(getattr(result, 'offset', 0) or 0)

    def peer_data(self, entity):
        return {'kind': 'chat' if isinstance(entity, self.types.Chat) else 'channel',
                'id': int(entity.id), 'access_hash': int(getattr(entity, 'access_hash', 0) or 0)}

    def peer(self, data):
        if data['kind'] == 'chat':
            return self.types.InputPeerChat(int(data['id']))
        return self.types.InputPeerChannel(int(data['id']), int(data['access_hash']))

    async def joined_group(self,event):
        msg=event.message
        if (not isinstance(msg,self.types.Message) or msg.out or getattr(msg,'action',None)
                or not isinstance(msg.peer_id,(self.types.PeerChat,self.types.PeerChannel))):return None
        sender=await event.get_sender()
        if not isinstance(sender,self.types.User) or sender.bot or sender.is_self:return None
        entity=await event.get_chat()
        if (not isinstance(entity,(self.types.Chat,self.types.Channel)) or getattr(entity,'left',False)
                or getattr(entity,'deactivated',False) or getattr(entity,'broadcast',False)
                or isinstance(entity,self.types.Channel) and not entity.megagroup):return None
        if str(self.utils.get_peer_id(entity))!=str(event.chat_id):return None
        input_peer=await event.get_input_chat()
        if str(self.utils.get_peer_id(input_peer))!=str(event.chat_id):return None
        data=self.peer_data(entity)
        if isinstance(input_peer,self.types.InputPeerChannel):data['access_hash']=int(input_peer.access_hash)
        return {'chat':str(event.chat_id),'title':entity.title,'peer':data,'username':getattr(entity,'username','') or ''}

    async def clear_group_history(self,target,max_id,guard=lambda:True):
        data=target.get('peer',{})
        if data.get('kind') not in ('chat','channel') or not 0<int(max_id)<2**31:
            raise ValueError('Для истории группы нужен подтверждённый адресат и граница сообщений.')
        peer=self.peer(data)
        if str(self.utils.get_peer_id(peer))!=str(target.get('chat')):raise ValueError('Группа не совпадает с адресатом очистки.')
        entity=await self.client.get_entity(peer)
        if (not isinstance(entity,(self.types.Chat,self.types.Channel)) or getattr(entity,'broadcast',False)
                or getattr(entity,'left',False) or isinstance(entity,self.types.Channel) and not entity.megagroup
                or str(self.utils.get_peer_id(entity))!=str(target.get('chat'))):
            raise ValueError('Очистка разрешена только для своей истории группы.')
        if not guard():return False
        if data['kind']=='channel':
            request=self.functions.channels.DeleteHistoryRequest(self.types.InputChannel(int(data['id']),int(data['access_hash'])),int(max_id),for_everyone=False)
        else:
            request=self.functions.messages.DeleteHistoryRequest(peer,int(max_id),just_clear=True,revoke=False)
        result=await self.client(request)
        if result is False or result is None:raise ValueError('Telegram не подтвердил очистку истории у наблюдателя.')
        return not int(getattr(result,'offset',0) or 0)

    async def group_histories(self):
        """Current joined groups only; no private chats, broadcast feeds or joins."""
        groups=[]
        async for dialog in self.client.iter_dialogs(limit=None):
            entity=dialog.entity
            if (not isinstance(entity,(self.types.Chat,self.types.Channel)) or getattr(entity,'left',False)
                    or getattr(entity,'deactivated',False) or getattr(entity,'broadcast',False)
                    or isinstance(entity,self.types.Channel) and not entity.megagroup):continue
            top=getattr(dialog,'message',None)
            cutoff=int(getattr(top,'id',0) or 0)
            if cutoff<=0:continue
            groups.append({'chat':str(self.utils.get_peer_id(entity)),'title':entity.title,
                           'peer':self.peer_data(entity),'username':getattr(entity,'username','') or '',
                           'comments_only':False,'channel_id':'','post_id':0,'max_id':cutoff})
        return groups

    async def group_history_context(self,target,cutoff):
        messages=await self.client.get_messages(self.peer(target['peer']),limit=60,max_id=int(cutoff)+1)
        messages=[msg for msg in messages if isinstance(msg,self.types.Message) and msg.id<=cutoff]
        by_id={msg.id:msg for msg in messages}
        result=[]
        for msg in messages:
            header=getattr(msg,'reply_to',None)
            root=None;candidate=msg;seen=set()
            for _ in range(6):
                if not candidate or candidate.id in seen:break
                seen.add(candidate.id)
                forward=getattr(candidate,'fwd_from',None)
                if getattr(getattr(forward,'from_id',None),'channel_id',0) and getattr(forward,'channel_post',0):
                    root=candidate;break
                reply=getattr(candidate,'reply_to',None)
                candidate=by_id.get(getattr(reply,'reply_to_top_id',0) or getattr(reply,'reply_to_msg_id',0))
            author=getattr(msg,'from_id',None)
            result.append({'id':msg.id,'text':str(msg.raw_text or '')[:4000],
                'person':str(getattr(author,'user_id','') or ''),'root':root.id if root else int(getattr(header,'reply_to_top_id',0) or 0) if getattr(header,'forum_topic',False) else 0,
                'post_id':int(root.fwd_from.channel_post) if root else 0,'post':str(root.raw_text or '')[:2000] if root else '',
                'origin_channel_id':str(root.fwd_from.from_id.channel_id) if root else ''})
        return result

    async def group_member(self,target,person):
        if not str(person).isdigit() or int(person)<=0:return None
        try:
            permissions=await self.client.get_permissions(self.peer(target['peer']),int(person))
            participant=getattr(permissions,'participant',None)
            return not bool(getattr(permissions,'has_left',False) or getattr(participant,'left',False)
                            or getattr(getattr(participant,'banned_rights',None),'view_messages',False))
        except Exception as exc:
            if type(exc).__name__ in ('UserNotParticipantError','ParticipantIdInvalidError'):return False
            raise

    async def lookup_group(self, reference):
        """Use this account's peers, including paginated and archived dialogs."""
        try:
            return await self.client.get_entity(reference)
        except ValueError:
            if isinstance(reference, int):
                # StringSession has no durable entity cache. Use the entity in
                # the dialog directly instead of retrying the same cache miss.
                try:
                    async for dialog in self.client.iter_dialogs(limit=None):
                        entity = dialog.entity
                        if (isinstance(entity, (self.types.Chat, self.types.Channel))
                                and self.utils.get_peer_id(entity) == reference):
                            return entity
                except ValueError:
                    pass
                raise ValueError('Аккаунт наблюдателя не видит группу ' + str(reference) + '. '
                    'По одному ID вступить в незнакомую группу нельзя. Вставьте ссылку-приглашение '
                    'вида https://t.me/+… и включите вступление либо добавьте аккаунт в группу вручную '
                    'и повторите указание ID.') from None
            raise ValueError('Аккаунт наблюдателя не нашёл группу по этому имени. '
                'Проверьте @имя открытой группы или используйте её ссылку-приглашение https://t.me/+….') from None

    async def resolve(self, reference, join=False, discussion=True):
        reference = str(reference or '').strip()
        if not reference or len(reference) > 300:
            raise ValueError('Укажите @имя, ID или ссылку Telegram.')
        if reference.startswith(('https://','http://','t.me/')):
            parsed = urlsplit(reference if '://' in reference else 'https://' + reference)
            if parsed.hostname not in ('t.me','telegram.me'):
                raise ValueError('Используйте ссылку Telegram.')
            reference = 'https://t.me' + parsed.path.rstrip('/')
        invite = re.fullmatch(r'(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)', reference)
        post = 0
        if invite:
            check = await self.client(self.functions.messages.CheckChatInviteRequest(invite[1]))
            entity = check.chat if isinstance(check, self.types.ChatInviteAlready) else None
            if entity is None or getattr(entity, 'left', False):
                if not join:
                    raise ValueError('Вступите этим аккаунтом в группу или включите вступление при добавлении.')
                result = await self.client(self.functions.messages.ImportChatInviteRequest(invite[1]))
                entity = next((item for item in getattr(result, 'chats', [])
                               if isinstance(item, (self.types.Chat, self.types.Channel)) and not getattr(item, 'left', False)), None)
                if entity is None:
                    raise ValueError('Telegram не подтвердил вступление в группу. Проверьте заявку на вступление и повторите добавление после одобрения.')
        else:
            private = re.fullmatch(r'https://t\.me/c/(\d+)/(\d+)', reference)
            link = re.fullmatch(r'(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:/(\d+))?/?', reference)
            if private:
                reference, post = str(-1000000000000 - int(private[1])), int(private[2])
            elif link:
                reference, post = link[1], int(link[2] or 0)
            elif not re.fullmatch(r'-?\d+|@[A-Za-z0-9_]{3,64}|[A-Za-z0-9_]{3,64}', reference):
                raise ValueError('Укажите @имя, ID или ссылку Telegram.')
            lookup = int(reference) if re.fullmatch(r'-?\d+', reference) else reference
            entity = await self.lookup_group(lookup)
        channel_id, username = '', getattr(entity, 'username', '') or ''
        source_title = str(getattr(entity, 'title', ''))
        if getattr(entity, 'broadcast', False):
            if not discussion:
                raise ValueError('Для основного чата укажите группу, а не канал.')
            channel_id = str(entity.id)
            full = await self.client(self.functions.channels.GetFullChannelRequest(entity))
            linked = getattr(full.full_chat, 'linked_chat_id', None)
            if not linked:
                raise ValueError('К каналу не привязана группа обсуждений.')
            entity = next((item for item in full.chats if item.id == linked), None)
            if entity is None:
                raise ValueError('Группа обсуждений недоступна аккаунту.')
        if not isinstance(entity, self.types.Chat) and not getattr(entity, 'megagroup', False):
            raise ValueError('Режим работает в группах и комментариях каналов, не в личных сообщениях.')
        if getattr(entity, 'left', False):
            if not join:
                raise ValueError('Аккаунт ещё не вступил в группу обсуждений.')
            if isinstance(entity, self.types.Chat):
                raise ValueError('Для вступления в эту группу нужна ссылка-приглашение https://t.me/+…. По одному ID вступить нельзя.')
            await self.client(self.functions.channels.JoinChannelRequest(entity))
        return {'chat': str(self.utils.get_peer_id(entity)), 'title': source_title or str(entity.title),
                'peer': self.peer_data(entity), 'channel_id': channel_id, 'username': username,
                'post_id': post, 'comments_only': bool(channel_id)}

    async def normalize(self, event, target, own_id, memory=None):
        msg = event.message
        if getattr(msg, 'out', False) or getattr(msg, 'action', None):
            return None
        sender = await event.get_sender()
        if not isinstance(sender, self.types.User) or sender.bot or str(sender.id) == str(own_id):
            return None
        text = str(getattr(msg, 'raw_text', '') or '')[:4000]
        parent_id = int(getattr(getattr(msg, 'reply_to', None), 'reply_to_msg_id', 0) or 0)
        top_id = int(getattr(getattr(msg, 'reply_to', None), 'reply_to_top_id', 0) or 0)
        parent = root = None
        peer = self.peer(target['peer'])
        if parent_id:
            parent = await self.client.get_messages(peer, ids=parent_id)
            if isinstance(parent,self.types.MessageEmpty):parent=None
        cached=memory(target['chat'],parent_id) if memory and parent_id and not parent else None
        if target['comments_only'] or target.get('_joined_chat') and parent_id:
            candidate = top_id or parent_id
            seen = set()
            for _ in range(6):
                if not candidate or candidate in seen:
                    break
                seen.add(candidate)
                item = parent if candidate == parent_id else await self.client.get_messages(peer, ids=candidate)
                if not item:
                    break
                forwarded = getattr(item, 'fwd_from', None)
                origin = getattr(forwarded, 'from_id', None)
                if getattr(origin,'channel_id',None) and (not target['comments_only'] or str(origin.channel_id)==target['channel_id']) and getattr(forwarded,'channel_post',0):
                    root = item
                    break
                reply = getattr(item, 'reply_to', None)
                candidate = int(getattr(reply, 'reply_to_top_id', 0) or getattr(reply, 'reply_to_msg_id', 0) or 0)
            if root is None and memory:
                cached=cached or memory(target['chat'],top_id or parent_id)
            if target['comments_only'] and root is None and not (cached and cached.get('post_id') and cached.get('channel_id')==target['channel_id']):return None
        post_id=int(root.fwd_from.channel_post) if root else int((cached or {}).get('post_id',0))
        if target['post_id'] and post_id!=target['post_id']:return None
        name = ' '.join(filter(None, (sender.first_name, sender.last_name))) or sender.username or str(sender.id)
        doc = getattr(msg, 'document', None)
        is_sticker = bool(doc and any(isinstance(attr, self.types.DocumentAttributeSticker) for attr in doc.attributes))
        is_visual = bool(msg.photo or is_sticker or doc and str(doc.mime_type).startswith(('image/','video/')))
        if is_sticker:
            self.documents[int(doc.id)] = doc
        link = ('https://t.me/' + target['username'] + '/' + str(post_id) + '?comment=' + str(msg.id)
                if target['channel_id'] and target['username'] else '')
        topic_reply=getattr(msg,'reply_to',None)
        topic_root=int(getattr(topic_reply,'reply_to_top_id',0) or getattr(topic_reply,'reply_to_msg_id',0) or 0) if getattr(topic_reply,'forum_topic',False) else 0
        return {'chat': target['chat'], 'id': int(msg.id), 'root': int(root.id) if root else topic_root or int((cached or {}).get('root',0)),
                'post_id': post_id, 'post': str(root.raw_text or '')[:2000] if root else str((cached or {}).get('post','')),
                'origin_channel_id':str(root.fwd_from.from_id.channel_id) if root else (cached or {}).get('channel_id',''),
                'person': str(sender.id), 'username': sender.username or '', 'name': name[:100],
                'text': text or ('[Стикер]' if is_sticker else '[Вложение без подписи]'),
                'user_text':text,
                'at': int((msg.date or datetime.now(timezone.utc)).timestamp()),
                'parent_id': parent_id, 'parent': str(getattr(parent, 'raw_text', '') or '')[:650] if parent else str((cached or {}).get('body',''))[:650],
                'parent_author': str(getattr(parent, 'sender_id', '') or '') if parent else str((cached or {}).get('author','')),
                'forwarded': bool(getattr(msg, 'fwd_from', None)), 'link': link,
                'mention_ids': [str(item.user_id) for item in (msg.entities or []) if isinstance(item, self.types.MessageEntityMentionName)],
                'mentioned': bool(getattr(msg, 'mentioned', False) or any(isinstance(item, self.types.MessageEntityMentionName)
                    and str(item.user_id) == str(own_id) for item in (msg.entities or []))),
                'document_id': str(doc.id) if is_sticker else '', 'media': is_visual,
                '_message': msg}

    async def image(self, message):
        try:
            raw = await asyncio.wait_for(self.client.download_media(message, bytes, thumb=-1), 15)
            if raw and len(raw) <= 3_000_000:
                return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()
        except Exception:
            pass
        return None

    async def send_text(self, target, text, reply_id=0, root=0, random_id=None):
        peer = self.peer(target['peer'])
        reply = self.types.InputReplyToMessage(reply_to_msg_id=reply_id, top_msg_id=root or None) if reply_id else None
        result = await self.client(self.functions.messages.SendMessageRequest(
            peer=peer, message=str(text), random_id=random_id or secrets.randbits(63) or 1,
            no_webpage=True, reply_to=reply))
        if getattr(result, 'id', None):
            return int(result.id)
        for update in getattr(result, 'updates', []):
            if isinstance(update, self.types.UpdateMessageID):
                return int(update.id)
            if getattr(update, 'message', None):
                return int(update.message.id)
        return 0

    async def send_dice(self,target,reply_id=0,root=0,random_id=None):
        reply=self.types.InputReplyToMessage(reply_to_msg_id=reply_id,top_msg_id=root or None) if reply_id else None
        result=await self.client(self.functions.messages.SendMediaRequest(
            peer=self.peer(target['peer']),media=self.types.InputMediaDice(emoticon='🎲'),message='',
            random_id=random_id or secrets.randbits(63) or 1,reply_to=reply))
        messages=[result]+[getattr(update,'message',None) for update in getattr(result,'updates',[])]
        for sent in messages:
            media=getattr(sent,'media',None)
            if isinstance(media,self.types.MessageMediaDice) and media.emoticon=='🎲' and 1<=media.value<=6 and getattr(sent,'id',0):
                return {'id':int(sent.id),'value':int(media.value)}
        # The dice may have reached Telegram even if the acknowledgement is
        # incomplete. Never replace an uncertain native roll with another one.
        raise ValueError('Telegram не подтвердил сообщение и значение кубика.')

    async def send_sticker(self, target, document_id, pack, reply_id, source=None):
        document_id = int(document_id)
        doc = self.documents.get(document_id)
        if doc is None and source:
            old = await self.client.get_messages(self.peer(source['peer']), ids=int(source['message_id']))
            if old and old.document and int(old.document.id) == document_id:
                doc = old.document
        if doc is None and re.fullmatch(r'[A-Za-z0-9_]{3,64}', pack):
            if pack not in self.packs:
                result = await self.client(self.functions.messages.GetStickerSetRequest(
                    stickerset=self.types.InputStickerSetShortName(short_name=pack), hash=0))
                self.packs[pack] = result.documents
            doc = next((item for item in self.packs[pack] if int(item.id) == document_id), None)
        if doc is None:
            raise ValueError('Стикер ещё недоступен аккаунту наблюдателя.')
        try:
            sent = await self.client.send_file(self.peer(target['peer']), doc, reply_to=reply_id)
        except Exception as exc:
            if type(exc).__name__ in ('FileReferenceExpiredError', 'FileReferenceInvalidError'):
                self.documents.pop(document_id, None)
                self.packs.pop(pack, None)
            raise
        self.documents[document_id] = doc
        return int(sent.id)

    async def upload_sticker(self, target, raw, format, reply_id, document_id=''):
        extension, mime = {'video': ('webm', 'video/webm'), 'animated': ('tgs', 'application/x-tgsticker')}.get(format, ('webp', 'image/webp'))
        stream = io.BytesIO(raw)
        stream.name = 'sticker.' + extension
        sent = await self.client.send_file(self.peer(target['peer']), stream, force_document=True,
            mime_type=mime, attributes=[self.types.DocumentAttributeSticker(alt='', stickerset=self.types.InputStickerSetEmpty())],
            reply_to=reply_id)
        if document_id and getattr(sent, 'document', None):
            self.documents[int(document_id)] = sent.document
        return int(sent.id)

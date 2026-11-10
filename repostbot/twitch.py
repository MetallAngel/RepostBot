"""Twitch chat with separate bot and moderator accounts, without extra packages."""

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


IRC_HOST = "irc.chat.twitch.tv"
EVENTSUB_HOST = "eventsub.wss.twitch.tv"


def _json_request(url, token="", client_id="", payload=None, form=False):
    from .core import _https_context
    body = None if payload is None else (urlencode(payload).encode() if form else json.dumps(payload).encode())
    headers = {"User-Agent": "RepostBot/1.0"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        headers["Authorization"] = ("OAuth " if url.startswith("https://id.twitch.tv/oauth2/validate") else "Bearer ") + token
    if client_id:
        headers["Client-Id"] = client_id
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=25, context=_https_context(True)) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise RuntimeError("Twitch HTTP " + str(exc.code) + ": " + detail[:300]) from None
    except (URLError, OSError) as exc:
        raise RuntimeError("Twitch: соединение недоступно: " + str(exc)[:200]) from None


def parse_irc_line(line):
    tags = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for part in raw[1:].split(";"):
            key, _, value = part.partition("=")
            tags[key] = re.sub(r"\\([sn:r\\])", lambda m: {"s": " ", "n": "\n", "r": "\r", ":": ";", "\\": "\\"}[m.group(1)], value)
    match = re.match(r":([^ ]+) PRIVMSG #([^ ]+) :(.*)$", line)
    if not match:
        return None
    login = match.group(1).split("!", 1)[0]
    sent=tags.get('tmi-sent-ts','')
    return {"message_id": tags.get("id", ""), "chatter_user_id": tags.get("user-id", ""), "chatter_user_login": login,
            'sent_at':int(sent)//1000 if sent.isdigit() and len(sent)<=16 and 0<int(sent)//1000<=int(time.time())+300 else int(time.time()),
            "chatter_user_name": tags.get("display-name") or login, "channel_login":match.group(2).casefold(), "channel_points_custom_reward_id": tags.get("custom-reward-id"),
            "reply_parent_user_id": tags.get("reply-parent-user-id", ""),
            "reply_parent_message_id": tags.get("reply-parent-msg-id", ""), "reply_parent_message_body":tags.get("reply-parent-msg-body", ""), "reply_parent_user_login":tags.get("reply-parent-user-login", ""),
            "message": {"text": match.group(3), "fragments": irc_emotes(match.group(3), tags.get("emotes", ""))}}


def irc_emotes(text, tags):
    """Twitch IRC uses character offsets for native emotes."""
    found = []
    for group in tags.split("/"):
        emote_id, sep, spans = group.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",emote_id):
            continue
        for span in spans.split(","):
            match = re.fullmatch(r"(\d+)-(\d+)", span)
            if match:
                start, end = map(int, match.groups())
                if 0 <= start <= end < len(text):
                    found.append({"type": "emote", "text": text[start:end + 1], "emote": {"id": emote_id}})
    return found


def _read_exact(sock, length):
    chunks = []
    while length:
        chunk = sock.recv(length)
        if not chunk:
            raise ConnectionError("Twitch закрыл WebSocket")
        chunks.append(chunk)
        length -= len(chunk)
    return b"".join(chunks)


class EventSocket:
    def __init__(self, url):
        parts = urlsplit(url)
        if parts.scheme != "wss" or parts.hostname != EVENTSUB_HOST or parts.port not in (None, 443):
            raise ValueError("Twitch вернул посторонний адрес WebSocket")
        from .core import _https_context
        raw = socket.create_connection((EVENTSUB_HOST, 443), timeout=20)
        try:
            self.sock = _https_context(True).wrap_socket(raw, server_hostname=EVENTSUB_HOST)
            self.sock.settimeout(45)
            key = base64.b64encode(os.urandom(16)).decode()
            path = parts.path or "/ws"
            if parts.query:
                path += "?" + parts.query
            request = (f"GET {path} HTTP/1.1\r\nHost: {EVENTSUB_HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n")
            self.sock.sendall(request.encode())
            header = b""
            while b"\r\n\r\n" not in header and len(header) < 8192:
                header += _read_exact(self.sock, 1)
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if not header.startswith(b"HTTP/1.1 101 ") or ("sec-websocket-accept: " + expected).casefold().encode() not in header.lower():
                raise RuntimeError("Twitch WebSocket не подтвердил соединение")
        except Exception:
            raw.close()
            raise

    def send_control(self, opcode, payload=b""):
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def receive(self):
        while True:
            head = _read_exact(self.sock, 2)
            opcode = head[0] & 15
            if head[0] & 0x70 or not head[0] & 0x80 or head[1] & 0x80:
                raise RuntimeError("Некорректный кадр Twitch WebSocket")
            length = head[1] & 127
            if length == 126:
                length = struct.unpack("!H", _read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _read_exact(self.sock, 8))[0]
            if length > 2 * 1024 * 1024:
                raise RuntimeError("Слишком большой кадр Twitch")
            payload = _read_exact(self.sock, length)
            if opcode == 9:
                self.send_control(10, payload[:125])
            elif opcode == 8:
                raise ConnectionError("Twitch закрыл WebSocket")
            elif opcode == 1:
                return json.loads(payload)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


from .reply_metrics import timed


class TwitchIntegration:
    def __init__(self, worker, community):
        self.worker, self.store, self.community = worker, worker.store, community
        self.reply_metrics = worker.reply_metrics
        self.auth_lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="twitch_reply")
        self.live_future = None
        self.live_lock = threading.RLock()

    @property
    def channel(self):return self.store.setting('twitch_channel')

    @property
    def login(self):return self.store.setting('twitch_chat_login')

    @property
    def user_id(self):return self.store.setting('twitch_chat_user_id')

    def answer_question(self,event,question):
        return self.worker.answer(question,chat_id='twitch:'+self.channel,person='twitch:'+str(event['chatter_user_id']),
            sender_username=event.get('chatter_user_login',''),event_message_key=event['message_id'])

    def style_reply(self,*args,**kwargs):return self.worker.style_community_reply(*args,**kwargs)

    def initiative_enabled(self,chat):return self.worker.conversation.enabled_for("twitch",chat)

    def status(self):
        return {"client_id": self.store.setting("twitch_client_id"), "redirect_uri": self.store.setting("twitch_redirect_uri"),
                "channel": self.store.setting("twitch_channel"), "telegram_target": self.store.setting("twitch_telegram_target"),
                "chat_login": self.store.setting("twitch_chat_login"), "moderator_login": self.store.setting("twitch_moderator_login"),
                "chat_connected": bool(self.store.setting("twitch_chat_refresh")), "moderator_connected": bool(self.store.setting("twitch_moderator_refresh")),
                "chat_state": self.store.state("twitch_chat_state", "Отключён"), "moderator_state": self.store.state("twitch_moderator_state", "Отключён")}

    def configure(self, data):
        client_id = str(data.get("client_id") or "").strip()
        channel = str(data.get("channel") or "").strip().lstrip("#").casefold()
        redirect = str(data.get("redirect_uri") or "").strip()
        target = str(data.get("telegram_target") or "").strip()
        parsed = urlsplit(redirect)
        if not re.fullmatch(r"[A-Za-z0-9]{20,80}", client_id) or not re.fullmatch(r"[a-z0-9_]{3,25}", channel):
            raise ValueError("Укажите Client ID приложения Twitch и логин канала")
        if parsed.path != "/api/twitch/callback" or parsed.query or parsed.fragment or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))):
            raise ValueError("Redirect URI: HTTPS-адрес панели с путём /api/twitch/callback (или localhost для локального входа)")
        if target and not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]{5,}", target):
            raise ValueError("Telegram: укажите ID чата или @имя")
        values = {"twitch_client_id": client_id, "twitch_channel": channel, "twitch_redirect_uri": redirect, "twitch_telegram_target": target}
        secret = str(data.get("client_secret") or "").strip()
        if secret:
            values["twitch_client_secret"] = secret
        if self.store.setting("twitch_client_id") != client_id or self.store.setting("twitch_channel") != channel:
            for role in ("chat", "moderator"):
                values.update({f"twitch_{role}_{key}": "" for key in ("access", "refresh", "login", "user_id", "expires")})
            values["twitch_broadcaster_id"] = ""
        self.store.set_settings(values)
        return self.status()

    def begin(self, role):
        if role not in ("chat", "moderator"):
            raise ValueError("Выберите аккаунт бота или модератора")
        status = self.status()
        if not status["client_id"] or not status["redirect_uri"] or not self.store.setting("twitch_client_secret"):
            raise ValueError("Сначала сохраните Client ID, Client Secret и Redirect URI")
        nonce = secrets.token_urlsafe(32)
        self.store.set_state("twitch_oauth_pending", json.dumps({"state": nonce, "role": role, "expires": int(time.time()) + 600}))
        scope = "chat:read user:write:chat" if role == "chat" else "user:read:chat"
        return "https://id.twitch.tv/oauth2/authorize?" + urlencode({"response_type": "code", "client_id": status["client_id"], "redirect_uri": status["redirect_uri"], "scope": scope, "state": nonce, "force_verify": "true"})

    def complete(self, state, code):
        try:
            pending = json.loads(self.store.state("twitch_oauth_pending", "{}"))
        except ValueError:
            pending = {}
        if not state or not secrets.compare_digest(str(pending.get("state") or ""), str(state)) or int(pending.get("expires") or 0) < time.time():
            raise ValueError("Сессия входа Twitch истекла. Начните вход заново")
        self.store.set_state("twitch_oauth_pending", "{}")
        role = pending["role"]
        result = _json_request("https://id.twitch.tv/oauth2/token", payload={"client_id": self.store.setting("twitch_client_id"), "client_secret": self.store.setting("twitch_client_secret"), "code": code, "grant_type": "authorization_code", "redirect_uri": self.store.setting("twitch_redirect_uri")}, form=True)
        identity = _json_request("https://id.twitch.tv/oauth2/validate", token=result["access_token"])
        if identity.get("client_id") != self.store.setting("twitch_client_id"):
            raise ValueError("Токен Twitch принадлежит другому приложению")
        other = "moderator" if role == "chat" else "chat"
        if str(identity.get("user_id")) == self.store.setting(f"twitch_{other}_user_id"):
            raise ValueError("Для бота и модератора нужны разные аккаунты Twitch")
        self.store.set_settings({f"twitch_{role}_access": result["access_token"], f"twitch_{role}_refresh": result["refresh_token"], f"twitch_{role}_login": identity["login"],
                                 f"twitch_{role}_user_id": str(identity["user_id"]), f"twitch_{role}_expires": str(int(time.time()) + int(result.get("expires_in", 3600)))})
        return {"role": role, "login": identity["login"]}

    def complete_from_url(self, callback_url):
        """Finish a loopback OAuth redirect pasted into the local admin panel."""
        raw = str(callback_url or "").strip()
        if len(raw) > 4096:
            raise ValueError("Адрес Twitch слишком длинный")
        parsed = urlsplit(raw)
        configured = self.store.setting("twitch_redirect_uri")
        if not configured or parsed._replace(query="", fragment="").geturl() != configured or parsed.fragment:
            raise ValueError("Вставьте полный адрес возврата Twitch из строки браузера, без изменений")
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        if "error" in query:
            raise ValueError("Вход Twitch не подтверждён: " + str(query["error"][0])[:100])
        if len(query.get("code", [])) != 1 or len(query.get("state", [])) != 1 or not query["code"][0]:
            raise ValueError("В адресе нет кода и состояния входа Twitch. Завершите авторизацию и скопируйте адрес из строки браузера")
        return self.complete(query["state"][0], query["code"][0])

    def disconnect(self, role):
        if role not in ("chat", "moderator"):
            raise ValueError("Неизвестный аккаунт Twitch")
        self.store.set_settings({f"twitch_{role}_{key}": "" for key in ("access", "refresh", "login", "user_id", "expires")})

    def token(self, role):
        with self.auth_lock:
            token = self.store.setting(f"twitch_{role}_access")
            if token and int(self.store.setting(f"twitch_{role}_expires", "0") or 0) > time.time() + 120:
                return token
            refresh = self.store.setting(f"twitch_{role}_refresh")
            if not refresh:
                raise RuntimeError("Аккаунт Twitch не подключён: " + role)
            result = _json_request("https://id.twitch.tv/oauth2/token", payload={"client_id": self.store.setting("twitch_client_id"), "client_secret": self.store.setting("twitch_client_secret"), "grant_type": "refresh_token", "refresh_token": refresh}, form=True)
            self.store.set_settings({f"twitch_{role}_access": result["access_token"], f"twitch_{role}_refresh": result.get("refresh_token", refresh), f"twitch_{role}_expires": str(int(time.time()) + int(result.get("expires_in", 3600)))})
            return result["access_token"]

    def send(self, message, reply_id=""):
        channel_id = self.broadcaster_id()
        body = {"broadcaster_id": channel_id, "sender_id": self.store.setting("twitch_chat_user_id"), "message": self.community.redact_terms(message).replace("\n", " ")[:500]}
        if reply_id:
            body["reply_parent_message_id"] = reply_id
        response = _json_request("https://api.twitch.tv/helix/chat/messages", token=self.token("chat"), client_id=self.store.setting("twitch_client_id"), payload=body)
        if not response.get("data") or not response["data"][0].get("is_sent"):
            raise RuntimeError("Twitch не принял сообщение: " + str(response.get("data", ""))[:200])
        message_id = response["data"][0].get("message_id")
        chat = "twitch:" + self.store.setting("twitch_channel")
        self.worker.conversation.note_output(chat, message_id, body["message"])
        with self.store.lock:
            author = self.store.db.execute("SELECT user_id FROM twitch_messages WHERE id=?", (str(reply_id),)).fetchone()
        self.worker.social.personal.record_reply(chat, "twitch:" + author[0] if author else "", body["message"], "sent:" + str(message_id))
        return message_id

    def broadcaster_id(self):
        channel = self.store.setting("twitch_channel")
        if not channel:
            raise RuntimeError("Укажите канал Twitch")
        cached = self.store.setting("twitch_broadcaster_id")
        if cached:
            return cached
        result = _json_request("https://api.twitch.tv/helix/users?" + urlencode({"login": channel}),
                               token=self.token("chat"), client_id=self.store.setting("twitch_client_id"))
        users = result.get("data") or []
        if not users or users[0].get("login", "").casefold() != channel:
            raise ValueError("Канал Twitch не найден")
        channel_id = str(users[0]["id"])
        self.store.set_settings({"twitch_broadcaster_id": channel_id})
        return channel_id

    def resolve_login(self, login):
        login = str(login).strip().removeprefix("@").casefold()
        if not re.fullmatch(r"[a-z0-9_]{4,25}", login):
            raise ValueError("Введите ник Twitch латиницей")
        result = _json_request("https://api.twitch.tv/helix/users?" + urlencode({"login": login}),
                               token=self.token("chat"), client_id=self.store.setting("twitch_client_id"))
        user = next((item for item in result.get("data", []) if item.get("login", "").casefold() == login), None)
        if not user:
            raise ValueError("Ник Twitch не найден")
        return {"id": str(user["id"]), "name": user.get("display_name") or user["login"], "username": user["login"]}

    def _reply(self, event, question):
        person = str(event["chatter_user_id"])
        name = str(event["chatter_user_name"])
        try:
            policy = self.community.attitude("twitch", person)
            chat = "twitch:" + self.channel
            role_mode = self.worker.social.has_role(chat, person)
            help_answer = self.community.debt_help("twitch", person, question, context=self.community.reconciliation_context("twitch", person, chat) and not role_mode)
            if help_answer:
                answer = help_answer
            elif policy == "refuse" and not role_mode:
                answer = self.community.reply_policy("twitch", person)[1]
            elif policy in ("hint", "strong") and not role_mode and not self.community.answer_allowed(policy):
                remark = self.community.reply_policy("twitch", person)[1]
                remark = self.community.grudge_reply(remark, 'twitch', person, chat, event['message_id'])
                answer = self.style_reply(remark, question, policy, "twitch:" + self.channel, "twitch", person)
                if answer:
                    answer = self.community.grudge_reply(answer, "twitch", person, chat, event["message_id"])
                    answer = self.community.transition_reply(answer, 'twitch', person, chat, event['message_id'])
                    if not answer:
                        return
                    self.send("@" + name + " " + answer, event["message_id"])
                    if policy in ("hint", "strong", "refuse") and not role_mode:
                        self.store.set_state("community_reconcile_context:" + chat + ":" + person, int(time.time()))
                return
            else:
                if re.search(r"\bкниг[а-яё]*\s+обид\b|\bдаммаз\s+крон\b", question, re.I):
                    answer = self.community.book_context(short=True)
                else:
                    trigger = self.worker.matching_trigger(question, {"id": "twitch:" + self.channel, "username": ""}, True)
                    if trigger and trigger["action"] == "fact":
                        fact = next((f for f in self.store.fixed_facts() if str(f["id"]) == trigger["payload"]), None)
                        answer = fact["value"] if fact else "Данные сейчас недоступны."
                    elif trigger and trigger["action"] == "answer":
                        answer = self.worker.expand_facts(trigger["payload"], self.store.fixed_facts())
                    else:
                        answer = self.answer_question(event,question)
                if answer:
                    answer = self.worker.community_tone("twitch", person, answer, question, "twitch:" + self.channel)
            if answer:
                if not role_mode:
                    answer = self.community.grudge_reply(answer, 'twitch', person, chat, event['message_id'])
                level = "help" if help_answer else "normal" if role_mode else "refuse" if policy == "refuse" else policy
                if level in ("help", "refuse"):
                    answer = self.style_reply(answer, question, level, "twitch:" + self.channel, "twitch", person)
                if answer:
                    if not role_mode:
                        answer = self.community.transition_reply(answer, 'twitch', person, chat, event['message_id'])
                    if not answer:
                        return
                    self.send("@" + name + " " + answer, event["message_id"])
                    if policy in ("hint", "strong", "refuse") and not role_mode:
                        self.store.set_state("community_reconcile_context:" + chat + ":" + person, int(time.time()))
        except Exception as exc:
            self.store.event("error", "Twitch ответ: " + str(exc)[:300])

    @timed("total")
    def on_chat(self, event, recorded=False):
        scout=getattr(self.worker,"twitch_scout",None)
        from .twitch_bots import ignored
        if ignored(event.get('chatter_user_login'),scout.settings().get('ignored_bots','') if scout else ''):return
        if not getattr(self,"is_scout",False) and scout and scout.claims_channel(self.channel):return
        reply = event.get("reply") or {}
        if reply:
            event = {**event, "reply_parent_message_id": event.get("reply_parent_message_id") or reply.get("parent_message_id", ""), "reply_parent_user_id": event.get("reply_parent_user_id") or reply.get("parent_user_id", "")}
        if str(event.get("chatter_user_id")) == self.user_id:
            return
        reward_id = str(event.get("channel_points_custom_reward_id") or "")
        if reward_id:
            self.on_reward({"id": "chat:" + str(event["message_id"]), "user_id": event["chatter_user_id"],
                            "user_login": event.get("chatter_user_login", ""), "user_name": event.get("chatter_user_name", ""),
                            "user_input": (event.get("message") or {}).get("text", ""),
                            "reward": {"id": reward_id, "title": self.community.reward_title(reward_id), "cost": 0}})
        if not recorded and not self.community.record_chat(event):
            return
        text = event["message"]["text"].strip()
        person = str(event["chatter_user_id"])
        name = str(event["chatter_user_name"])
        assessment = self.worker.assess_offense(text) if not self.community.profile("twitch", person)["protected"] else None
        response = self.community.observe("twitch", person, name, event["message_id"], text, assessment=assessment, transition_notify_allowed=False)
        chat = "twitch:" + self.channel
        self.worker.social.personal.observe(chat, "twitch:" + person, text)
        bot_login = self.login
        from .core import split_items
        aliases = split_items(self.store.setting("reply_names")) + [bot_login]
        addressed = bool(re.match(r"!assistant\b", text, re.I) or str(event.get("reply_parent_user_id", "")) == (self.user_id or "missing") or any(alias and re.search(r"(?<!\w)@?" + re.escape(alias) + r"(?!\w)", text, re.I) for alias in aliases))
        parent_id = str(event.get("reply_parent_message_id") or "")
        with self.store.lock:
            human = self.store.db.execute("SELECT user_id,text,at FROM twitch_messages WHERE id=?", (parent_id,)).fetchone() if parent_id else None
            bot = self.store.db.execute("SELECT answer FROM conversation_outputs WHERE chat=? AND message_key=?", (chat, parent_id)).fetchone() if parent_id else None
        quest_event = self.worker.quests.observe("twitch", chat, person, name, event["message_id"], text, violation=bool(assessment or list(self.community.matching_terms("twitch", text))), addressed=addressed, replied_bot=bot[0] if bot else "", replied_human=human["text"] if human else "", human_person=human["user_id"] if human else "", human_message_key="twitch:" + parent_id if human else "", human_message_at=human["at"] if human else 0)
        self.worker.quests.auto.on_message("twitch", chat, person, name, event["message_id"], text,
                                          penalty_hint=self.worker.moderation.incident_points(text, "", assessment))
        notice = self.worker.quests.notice(quest_event, "twitch", name)
        if notice:
            response = (response + " " if response else "") + notice
        threshold_notice = self.community.transition_notice('twitch', person, chat, event['message_id']) if not addressed else None
        if threshold_notice:
            response = threshold_notice
        if self.worker.social.has_role(chat, person) and not self.community.reconciliation_question(text):
            response, threshold_notice = notice, None
        if response:
            try:
                self.send(response)
            except Exception as exc:
                self.store.event("error", "Twitch реакция: " + str(exc)[:250])
            if notice or threshold_notice:
                return
            if self.community.attitude("twitch", person) == "refuse":
                return
        link = re.fullmatch(r"!assistant\s+link\s+([A-Za-z0-9]{8,30})", text, re.I)
        if link:
            try:
                answer = "Аккаунты связаны, репутация общая." if self.community.finish_link(person, link.group(1)) else "Код не найден или истёк."
                self.send("@" + name + " " + answer, event["message_id"])
            except ValueError as exc:
                self.send("@" + name + " " + str(exc), event["message_id"])
            return
        if re.fullmatch(r"!assistant\s+(?:book|книга)", text, re.I):
            try:
                self.send(self.community.book_summary(short=True), event["message_id"])
            except Exception as exc:
                self.store.event("error", "Twitch Книга обид: " + str(exc)[:250])
            return
        if re.fullmatch(r"!(?:rep|assistant\s+(?:rep|репутация))", text, re.I):
            try:
                self.send("@" + name + " " + self.community.own_reputation("twitch", person, chat), event["message_id"])
            except Exception as exc:
                self.store.event("error", "Twitch репутация: " + str(exc)[:250])
            return
        quest_answer = self.worker.quests.command("twitch", chat, person, name, text)
        if quest_answer:
            self.send(quest_answer[:450], event["message_id"])
            return
        bot_login = self.login.casefold()
        match = re.match(r"^!assistant(?:\s+(.+))?$", text, re.I)
        if match:
            question = (match.group(1) or "").strip()
        elif bot_login and re.search(r"(?<!\w)@" + re.escape(bot_login) + r"(?!\w)", text, re.I):
            question = re.sub(r"(?<!\w)@" + re.escape(bot_login) + r"(?!\w)", "", text, flags=re.I).strip()
        elif event.get("reply_parent_user_id") and str(event["reply_parent_user_id"]) == self.user_id:
            question = text
        else:
            from .core import split_items
            aliases = split_items(self.store.setting("reply_names"))
            chat = "twitch:" + self.channel
            context = self.community.reconciliation_context("twitch", person, chat) and not self.worker.social.has_role(chat, person)
            reconcile = self.community.reconciliation_question(text, context) and (context or self.store.setting("community_enabled", "0") == "1" and self.community.attitude("twitch", person) != "normal")
            if reconcile or any(alias and re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.I) for alias in aliases):
                question = text
            else:
                chat = "twitch:" + self.channel
                if not response and self.initiative_enabled(chat):
                    with self.live_lock:
                        if self.live_future is None or self.live_future.done():
                            self.live_future = self.pool.submit(self._live_reply, event)
                return
        if question:
            self.pool.submit(self._reply, event, question[:1500])

    def _live_reply(self, event):
        person = str(event["chatter_user_id"])
        chat = "twitch:" + self.channel
        text = str(event["message"]["text"]).strip()
        def send(decision):
            answer = self.worker.community_tone("twitch", person, decision["text"], text, chat)
            if not answer:
                return False
            if not self.worker.social.has_role(chat, person):
                answer = self.community.grudge_reply(answer, "twitch", person, chat, event["message_id"])
                answer = self.community.transition_reply(answer, 'twitch', person, chat, event['message_id'])
            if not answer:
                return False
            sent = self.send(answer, event["message_id"])
            if sent:
                self.store.remember_ai_context(chat, "twitch:" + person, text, self.worker.fact_markers(answer, self.store.fixed_facts()))
            return bool(sent)
        self.worker.conversation.consider("twitch", chat, person, event["chatter_user_name"], text, event["message_id"], send)

    def chat_loop(self):
        failures = 0
        while not self.worker.stop.is_set():
            if not self.store.setting("twitch_chat_refresh") or not self.channel:
                self.worker.stop.wait(5)
                continue
            try:
                token = self.token("chat")
                channel = self.channel
                with socket.create_connection((IRC_HOST, 6697), timeout=20) as raw:
                    from .core import _https_context
                    with _https_context(True).wrap_socket(raw, server_hostname=IRC_HOST) as sock:
                        sock.settimeout(60)
                        commands = f"PASS oauth:{token}\r\nNICK {self.login}\r\nCAP REQ :twitch.tv/tags twitch.tv/commands\r\nJOIN #{channel}\r\n"
                        sock.sendall(commands.encode())
                        self.store.set_state("twitch_chat_state", "Подключён к #" + channel)
                        self.store.event("info", "Twitch: чат подключён к #" + channel)
                        failures = 0
                        buffer = b""
                        while not self.worker.stop.is_set() and self.store.setting("twitch_chat_refresh"):
                            try:
                                chunk = sock.recv(8192)
                            except socket.timeout:
                                sock.sendall(b"PING :repostbot\r\n")
                                continue
                            if not chunk:
                                raise ConnectionError("Twitch закрыл чат")
                            buffer += chunk
                            if len(buffer) > 100_000:
                                buffer = b""
                            while b"\r\n" in buffer:
                                raw_line, buffer = buffer.split(b"\r\n", 1)
                                line = raw_line.decode("utf-8", "replace")
                                if line.startswith("PING "):
                                    sock.sendall(("PONG " + line[5:] + "\r\n").encode())
                                elif "Login authentication failed" in line:
                                    raise RuntimeError("Twitch не принял токен чат-аккаунта. Подключите его повторно")
                                else:
                                    event = parse_irc_line(line)
                                    if event:
                                        self.on_chat(event)
            except Exception as exc:
                failures += 1
                self.store.set_state("twitch_chat_state", "Ошибка: " + str(exc)[:200])
                if failures in (1, 10):
                    self.store.event("error", "Twitch чат: " + str(exc)[:300])
                self.worker.stop.wait(min(60, 5 * failures))

    def _subscribe_moderator_chat(self, session_id):
        payload = {"type": "channel.chat.message", "version": "1",
                   "condition": {"broadcaster_user_id": self.broadcaster_id(), "user_id": self.store.setting("twitch_moderator_user_id")},
                   "transport": {"method": "websocket", "session_id": session_id}}
        _json_request("https://api.twitch.tv/helix/eventsub/subscriptions", token=self.token("moderator"), client_id=self.store.setting("twitch_client_id"), payload=payload)

    def on_reward(self, event):
        result = self.community.record_reward(event)
        if not result:
            return
        reward, name = event.get("reward") or {}, str(event.get("user_name") or "Участник")
        rule = result["rule"]
        if not rule or not self.community.notification_enabled("reward", "twitch"):
            return
        message = self.community.choose_message("reward", "twitch", str(event["user_id"]), rule["response"],
                                                {"user": name[:50], "reward": str(reward.get("title") or "")[:100], "score": result["profile"]["reputation"]})
        try:
            self.send(message[:500])
        except Exception as exc:
            self.store.event("error", "Twitch награда, ответ в чат: " + str(exc)[:250])

    def moderator_loop(self):
        failures = 0
        reconnect = ""
        while not self.worker.stop.is_set():
            if not self.store.setting("twitch_moderator_refresh") or not self.store.setting("twitch_chat_refresh"):
                self.worker.stop.wait(5)
                continue
            try:
                sock = EventSocket(reconnect or "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30")
                try:
                    intentional_reconnect = bool(reconnect)
                    reconnect = ""
                    while not self.worker.stop.is_set() and self.store.setting("twitch_moderator_refresh"):
                        data = sock.receive()
                        kind = data.get("metadata", {}).get("message_type")
                        if kind == "session_welcome":
                            session_id = data["payload"]["session"]["id"]
                            if not intentional_reconnect:
                                self._subscribe_moderator_chat(session_id)
                            self.store.set_state("twitch_moderator_state", "Чат подключён; видны награды с сообщением")
                            failures = 0
                        elif kind == "notification" and data.get("payload", {}).get("subscription", {}).get("type") == "channel.chat.message":
                            self.on_chat(data["payload"]["event"])
                        elif kind == "session_reconnect":
                            reconnect = data["payload"]["session"].get("reconnect_url", "")
                            break
                        elif kind == "revocation":
                            raise RuntimeError("Подписка Twitch на чат отозвана: " + str(data.get("payload", {}).get("subscription", {}).get("status")))
                finally:
                    sock.close()
            except (socket.timeout, TimeoutError):
                reconnect = ""
                self.store.set_state("twitch_moderator_state", "Переподключение")
            except Exception as exc:
                reconnect = ""
                failures += 1
                self.store.set_state("twitch_moderator_state", "Ошибка: " + str(exc)[:200])
                if failures in (1, 10):
                    self.store.event("error", "Twitch модератор: " + str(exc)[:300])
                self.worker.stop.wait(min(60, 5 * failures))

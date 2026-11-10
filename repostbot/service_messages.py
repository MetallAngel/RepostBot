"""Compact/private informational replies and durable deletion of public replies."""

import hashlib
import json
import re
import threading
import time


DEFAULTS = {"mode": "public", "delete_seconds": 120, "delete_commands": True, "command_delete_seconds": 3,
            "delete_book_requests": True, "compact_quests": True, "quest": True, "rep": True,
            "book": True, "context": True, "help": True}


class ServiceMessages:
    def __init__(self, worker):
        self.worker, self.store = worker, worker.store
        self.lock = threading.RLock()
        self.member_cache = {}
        with self.store.lock, self.store.db:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS service_deletions (
                    chat TEXT NOT NULL, message_id INTEGER NOT NULL, token_hash TEXT NOT NULL,
                    created INTEGER NOT NULL, due INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    user_command INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat,message_id));
                CREATE INDEX IF NOT EXISTS service_deletions_due ON service_deletions(due);
                CREATE TABLE IF NOT EXISTS service_hidden_updates (
                    token_hash TEXT NOT NULL, update_id INTEGER NOT NULL, at INTEGER NOT NULL,
                    PRIMARY KEY(token_hash,update_id));
                CREATE TABLE IF NOT EXISTS service_pending_sources (
                    chat TEXT NOT NULL, message_id INTEGER NOT NULL, token_hash TEXT NOT NULL,
                    created INTEGER NOT NULL, chat_type TEXT NOT NULL, source_kind TEXT NOT NULL,
                    PRIMARY KEY(chat,message_id));
            """)

    def settings(self):
        result = {}
        for key, default in DEFAULTS.items():
            fallback = self.store.setting("service_book", "1") if key == "context" else str(int(default)) if isinstance(default, bool) else str(default)
            value = self.store.setting("service_" + key, fallback)
            result[key] = value == "1" if isinstance(default, bool) else int(value) if isinstance(default, int) else value
        return result

    def configure(self, data):
        config = self.settings()
        for key, default in DEFAULTS.items():
            if key not in data:
                continue
            config[key] = data[key] is True if isinstance(default, bool) else int(data[key]) if isinstance(default, int) else str(data[key])
        if config["mode"] not in ("public", "ephemeral") or not 0 <= config["delete_seconds"] <= 86400 or not 1 <= config["command_delete_seconds"] <= 3600:
            raise ValueError("Выберите режим ответа и срок удаления от 0 до 86400 секунд")
        self.store.set_settings({"service_" + key: str(int(value)) if isinstance(value, bool) else str(value) for key, value in config.items()})
        return config

    @staticmethod
    def fingerprint(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def supported_key(self, token):
        return "service_ephemeral_supported:" + self.fingerprint(token)

    def publish_commands(self, token):
        """Explicit panel action: preserve other commands and verify API support."""
        from .core import tg_call
        if not token:
            raise ValueError("Сначала подключите Telegram")
        config = self.settings()
        scope = {"type": "all_group_chats"}
        original = tg_call(token, "getMyCommands", {"scope": scope}, timeout=15)
        commands = original or tg_call(token, "getMyCommands", timeout=15)
        obsolete_dice = {"roll", "d4", "d8", "d10", "d12", "d20", "roll_advantage"}
        prepared = {item["command"]: dict(item) for item in commands if item.get("command") not in obsolete_dice}
        wanted = {
            "quest": "Доска добрых дел и прогресс", "rep": "Моё отношение с ботом",
            "book": "Страницы Книги обид", "d6": "Нативный кубик d6 Telegram",
        }
        wanted["quest"] = "Мой личный квест и прогресс"
        for command, description in wanted.items():
            item = prepared.setdefault(command, {"command": command, "description": description})
            item["is_ephemeral"] = command == "quest" or config["mode"] == "ephemeral" and config.get(command, False)
        if len(prepared) > 100:
            raise ValueError("В меню Telegram уже слишком много команд")
        self.store.set_state(self.supported_key(token), "0")
        tg_call(token, "setMyCommands", {"scope": scope, "commands": list(prepared.values())}, timeout=15)
        if any(prepared[command].get("is_ephemeral") for command in wanted):
            actual = {item["command"]: item for item in tg_call(token, "getMyCommands", {"scope": scope}, timeout=15)}
            hidden = [command for command in wanted if prepared[command].get("is_ephemeral")]
            if not hidden or not all(actual.get(command, {}).get("is_ephemeral") is True for command in hidden):
                # Old Bot API servers may ignore unknown fields. Never test support
                # by sending the user's full answer as a potentially public message.
                tg_call(token, "setMyCommands", {"scope": scope, "commands": original}, timeout=15)
                raise ValueError("Telegram не подтвердил скрытые команды. Обновите Bot API; личные свитки не будут публиковаться открыто")
            self.store.set_state(self.supported_key(token), "1")
        return "Меню групп обновлено: доступен нативный кубик /d6; остальные броски работают текстом."

    def ensure_d6_command(self, token):
        """Keep the only public dice command in Telegram's menu after restart."""
        from .core import tg_call
        if not token:
            return False
        scope = {"type": "all_group_chats"}
        current = tg_call(token, "getMyCommands", {"scope": scope}, timeout=15) or []
        obsolete_dice = {"roll", "d4", "d8", "d10", "d12", "d20", "roll_advantage"}
        prepared = {item["command"]: dict(item) for item in current if item.get("command") not in obsolete_dice and item.get("command") != "d6"}
        prepared["d6"] = {"command": "d6", "description": "Нативный кубик d6 Telegram"}
        tg_call(token, "setMyCommands", {"scope": scope, "commands": list(prepared.values())}, timeout=15)
        return True

    def private_notice(self, token, chat, person, answer, thread_id=0):
        """Proactive private delivery never marks a normal message as answered."""
        if self.store.state(self.supported_key(token), "0") != "1":
            raise ValueError("Обновите меню команд Telegram: скрытая доставка ещё не подтверждена")
        if not str(person).isdigit() or not str(chat).startswith("-"):
            raise ValueError("Личный свиток требует известного участника Telegram-группы")
        if self.member(token, chat).get("status") not in ("creator", "administrator"):
            raise ValueError("Для скрытой выдачи свитка боту нужны права администратора группы")
        payload = {"chat_id": chat, "text": self.worker.community.redact_terms(answer)[:3500], "disable_notification": True,
                   "ephemeral_message_parameters": {"receiver_user_id": int(person)}}
        if thread_id:payload["message_thread_id"] = int(thread_id)
        sent = self.worker.telegram_delivery(token, "sendMessage", payload, timeout=12)
        if not isinstance(sent, dict) or not sent.get("ephemeral_message_id") or sent.get("message_id"):
            self.store.set_state(self.supported_key(token), "0")
            if isinstance(sent, dict) and sent.get("message_id"):
                from .core import tg_call
                actual = (sent.get("chat") or {}).get("id", chat)
                try:tg_call(token, "deleteMessage", {"chat_id": actual, "message_id": sent["message_id"]}, timeout=8)
                except Exception:self.schedule(token, actual, sent["message_id"], 1)
            raise ValueError("Telegram не подтвердил приватность; повторная открытая отправка запрещена")
        return sent

    def member(self, token, chat):
        from .core import tg_call
        key = (self.fingerprint(token), str(chat), self.worker.bot_id)
        cached = self.member_cache.get(key)
        if cached and cached[0] > time.time() - 60:
            return cached[1]
        if not self.worker.bot_id:
            self.worker.refresh_telegram_identity(token)
        info = self.worker.telegram_delivery(token, "getChatMember", {"chat_id": chat, "user_id": self.worker.bot_id}, timeout=10)
        self.member_cache[key] = (time.time(), info)
        return info

    def schedule(self, token, chat, message_id, seconds, user_command=False, created=None):
        if not message_id or seconds <= 0:
            return
        now = int(time.time())
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO service_deletions(chat,message_id,token_hash,created,due,user_command) VALUES (?,?,?,?,?,?)", (str(chat), int(message_id), self.fingerprint(token), int(created or now), now + int(seconds), int(user_command)))

    def cancel_for_reply(self, chat, message_id):
        """Keep a bot message when a participant explicitly replies to it."""
        if not message_id:
            return
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM service_deletions WHERE chat=? AND message_id=?", (str(chat), int(message_id)))

    def standalone_book_request(self, msg):
        """Only a whole, plain-text request may be removed; never a mixed message."""
        if any(msg.get(key) for key in ("photo", "video", "animation", "sticker", "document", "audio", "voice", "caption")):
            return False
        text = msg.get("text", "").strip()
        from .core import split_items
        names = split_items(self.store.setting("reply_names")) + [self.worker.bot_name, self.worker.bot_username]
        for name in sorted(filter(None, names), key=len, reverse=True):
            text = re.sub(r"^@?" + re.escape(name) + r"(?!\w)[\s,:!—-]*", "", text, flags=re.I)
        subject = r"(?:книг(?:а|у|е|ой|и)\s+обид|даммаз\s+крон)"
        request = r"(?:(?:покажи|открой|выведи|расскажи|объясни|опиши)(?:\s+(?:мне|нам))?(?:\s+(?:про|о|об|по))?|что\s+(?:такое|за)|как\s+работает|что\s+расскажешь\s+(?:про|о|по))"
        return bool(re.fullmatch(r"(?:пожалуйста[,\s]+)?(?:" + request + r"\s+)?" + subject + r"(?:[,\s]+(?:пожалуйста|плиз|пж|спасибо))?[.!?\s]*", text, re.I))

    def source_kind(self, msg, kind):
        if msg.get("ephemeral_message_id") or msg.get("message_id", 0) <= 0 or msg["chat"].get("type") not in ("group", "supergroup"):
            return ""
        text = msg.get("text", "").strip()
        command = re.match(r"^/(quest|квест|rep|репутация|book|книга)(?:@([A-Za-z0-9_]+))?(?:\s|$)", text, re.I)
        if command and (not command[2] or command[2].casefold() == self.worker.bot_username.casefold()):
            return "command"
        return "book_request" if kind == "context" and self.standalone_book_request(msg) else ""

    def track_source(self, token, msg, kind):
        source = self.source_kind(msg, kind)
        if source:
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO service_pending_sources VALUES (?,?,?,?,?,?)", (str(msg["chat"]["id"]), msg["message_id"], self.fingerprint(token), int(msg.get("date") or time.time()), msg["chat"]["type"], source))

    def cleanup_source(self, token, chat, message_id, actual_chat=None):
        """Called only after successful delivery, including a queued public retry."""
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT * FROM service_pending_sources WHERE chat=? AND message_id=?", (str(chat), message_id)).fetchone()
            if not row:
                return
            source = dict(row)
            self.store.db.execute("DELETE FROM service_pending_sources WHERE chat=? AND message_id=?", (str(chat), message_id))
        config = self.settings()
        enabled = config["delete_commands"] if source["source_kind"] == "command" else config["delete_book_requests"]
        if not enabled or source["token_hash"] != self.fingerprint(token) or (actual_chat is not None and str(actual_chat) != str(chat)) or int(time.time()) - source["created"] >= 47 * 3600:
            return
        try:
            member = self.member(token, chat)
            if member.get("status") == "creator" or member.get("status") == "administrator" and (source["chat_type"] == "group" or member.get("can_delete_messages")):
                self.schedule(token, chat, message_id, config["command_delete_seconds"], user_command=True, created=source["created"])
            elif int(self.store.state("service_delete_rights:" + str(chat), "0")) < int(time.time()) - 300:
                self.store.set_state("service_delete_rights:" + str(chat), int(time.time()))
                self.store.event("info", "Удаление запросов Telegram: дайте боту право удаления сообщений в группе " + str(chat))
        except Exception as exc:
            # A rights-check failure must never enqueue or repeat an already sent answer.
            self.store.event("info", "Удаление запроса Telegram: " + str(exc).replace(token, "[скрыто]")[:220])

    def send(self, token, msg, answer, kind):
        config, chat, mid = self.settings(), msg["chat"], msg.get("message_id", 0)
        if not msg.get("ephemeral_message_id") and self.store.reply_handled(chat["id"], mid):
            return None
        hidden = kind == "quest" or bool(msg.get("ephemeral_message_id")) or config["mode"] == "ephemeral"
        try:
            body = self.worker.community.redact_terms(answer)[:3500]
            payload = {"chat_id": chat["id"], "text": body, "disable_notification": True}
            if msg.get("message_thread_id"):
                payload["message_thread_id"] = msg["message_thread_id"]
            if hidden:
                person = (msg.get("from") or {}).get("id")
                if not person or msg.get("sender_chat"):
                    raise ValueError("Скрытый ответ требует известного получателя")
                if msg.get("ephemeral_message_id"):
                    if int(time.time()) - int(msg.get("date", 0)) >= 15:
                        raise ValueError("Истекло время скрытой команды; повторите её из меню")
                    payload["reply_parameters"] = {"ephemeral_message_id": msg["ephemeral_message_id"]}
                else:
                    if self.store.state(self.supported_key(token), "0") != "1":
                        raise ValueError("Сначала опубликуйте и проверьте скрытые команды в настройках Telegram")
                    if self.member(token, chat["id"]).get("status") not in ("creator", "administrator"):
                        raise ValueError("Для скрытого ответа на публичную команду нужны права администратора")
                payload["ephemeral_message_parameters"] = {"receiver_user_id": int(person)}
            elif mid:
                payload["reply_parameters"] = {"message_id": mid, "allow_sending_without_reply": True}
            sent = self.worker.telegram_delivery(token, "sendMessage", payload, timeout=12)
            actual_chat = (sent.get("chat") or {}).get("id", self.store.canonical_chat_id(chat["id"])) if isinstance(sent, dict) else chat["id"]
            if hidden:
                if not isinstance(sent, dict) or not sent.get("ephemeral_message_id") or sent.get("message_id"):
                    self.store.set_state(self.supported_key(token), "0")
                    if isinstance(sent, dict) and sent.get("message_id"):
                        self.schedule(token, actual_chat, sent["message_id"], 1)
                    raise ValueError("Telegram не подтвердил приватность ответа; скрытый режим остановлен")
                # Ephemeral IDs can be reused after expiry. Do not persist a delayed
                # deletion of such an ID and accidentally delete a newer message.
            else:
                self.store.remember_bot_reply(actual_chat, mid, sent, body)
                self.schedule(token, actual_chat, sent.get("message_id"), config["delete_seconds"])
            if not msg.get("ephemeral_message_id"):
                self.store.mark_reply_delivered(actual_chat, mid)
            self.track_source(token, msg, kind)
            self.cleanup_source(token, chat["id"], mid, actual_chat)
            if kind == "quest" and re.fullmatch(r"/(?:quest|квест)(?:@\w+)?(?:\s+(?:status|прогресс))?", msg.get("text", "").strip(), re.I):
                self.worker.quests.confirm_command_delivery(str((msg.get("from") or {}).get("id", "")), answer)
            return sent
        except Exception as exc:
            # No plaintext retry queue and no public fallback for a private answer.
            if not msg.get("ephemeral_message_id"):
                self.store.mark_reply_delivered(chat["id"], mid)
            self.store.event("info", "Служебный ответ Telegram: " + str(exc).replace(token, "[скрыто]")[:220])
            return None

    def delete_due(self, token):
        from .core import tg_call, TelegramChatMigrated
        if not token:
            return
        with self.lock:
            now, fingerprint = int(time.time()), self.fingerprint(token)
            with self.store.lock:
                jobs = [dict(row) for row in self.store.db.execute("SELECT * FROM service_deletions WHERE due<=? ORDER BY due LIMIT 50", (now,))]
            with self.store.lock, self.store.db:
                self.store.db.execute("DELETE FROM service_pending_sources WHERE created<? OR token_hash<>?", (now - 47 * 3600, fingerprint))
            for job in jobs:
                done = job["token_hash"] != fingerprint or now - job["created"] >= 47 * 3600
                if not done:
                    try:
                        # IDs belong to the original chat. Never retarget a deletion
                        # after migration to a different message in the new chat.
                        tg_call(token, "deleteMessage", {"chat_id": job["chat"], "message_id": job["message_id"]}, timeout=8)
                        done = True
                    except Exception as exc:
                        detail = str(exc).replace(token, "[скрыто]")[:220]
                        terminal = any(part in detail.lower() for part in ("not found", "can't be deleted", "not enough rights", "forbidden", "chat not found"))
                        done = terminal or isinstance(exc, TelegramChatMigrated) or job["attempts"] >= 3
                        if done:
                            self.store.event("info", "Автоудаление Telegram: " + detail)
                        else:
                            with self.store.lock, self.store.db:
                                self.store.db.execute("UPDATE service_deletions SET attempts=attempts+1,due=? WHERE chat=? AND message_id=?", (now + 15 * 2 ** job["attempts"], job["chat"], job["message_id"]))
                if done:
                    with self.store.lock, self.store.db:
                        self.store.db.execute("DELETE FROM service_deletions WHERE chat=? AND message_id=?", (job["chat"], job["message_id"]))

    def loop(self):
        while not self.worker.stop.is_set():
            try:
                self.delete_due(self.store.setting("telegram_token"))
            except Exception as exc:
                self.store.event("info", "Автоудаление Telegram: " + type(exc).__name__)
            self.worker.stop.wait(2)

    def handle_hidden(self, token, msg, update_id):
        """Hidden interactions never enter public memory, routes or message-ID 0 queues."""
        if msg["chat"].get("type") not in ("group", "supergroup") or msg.get("from", {}).get("is_bot"):
            return
        if self.store.setting("reply_enabled", "1") != "1":
            return
        from .core import split_items
        allowed = split_items(self.store.setting("reply_chats"))
        if allowed and str(msg["chat"]["id"]) not in allowed and ("@" + msg["chat"].get("username", "")).casefold() not in [value.casefold() for value in allowed]:
            return
        with self.store.lock, self.store.db:
            if not self.store.db.execute("INSERT OR IGNORE INTO service_hidden_updates VALUES (?,?,?)", (self.fingerprint(token), int(update_id), int(time.time()))).rowcount:
                return
            self.store.db.execute("DELETE FROM service_hidden_updates WHERE at<?", (int(time.time()) - 30 * 86400,))
        if int(time.time()) - int(msg.get("date", 0)) >= 15:
            return
        text, user = msg.get("text", "").strip(), msg.get("from") or {}
        if not user.get("id") or msg.get("sender_chat"):
            return
        person = str(user["id"])
        name = user.get("username") or user.get("first_name") or person
        self.worker.community.see_user("telegram", person, name, user.get("username", ""), msg["chat"]["id"])
        config = self.settings()
        answer = self.worker.quests.command("telegram", str(msg["chat"]["id"]), person, name, text, self.worker.bot_username, compact=config["compact_quests"])
        kind = "quest"
        if answer is None and config["rep"] and re.fullmatch(r"/rep(?:@" + re.escape(self.worker.bot_username) + r")?", text, re.I):
            answer = self.worker.community.own_reputation("telegram", person)
            kind = "rep"
        if answer is None and config["book"] and re.fullmatch(r"/book(?:@" + re.escape(self.worker.bot_username) + r")?", text, re.I):
            answer = self.worker.community.book_summary()
            kind = "book"
        if answer is None and re.fullmatch(r"/(?:quest|квест)(?:@" + re.escape(self.worker.bot_username) + r")?", text, re.I):
            glitch = self.worker.conversation.glitch_quest_status(str(msg["chat"]["id"]), person)
            if glitch and glitch["status"] == "pending":
                answer = ("🎲 Личный квест ещё ждёт ответа. Выполнение простое: ответь обычным сообщением «да» — он засчитается; "
                          "«нет» отменит его без штрафа.")
            elif glitch and glitch["status"] == "accepted":
                answer = "✅ Личный квест уже выполнен. Больше ничего делать не нужно."
            elif glitch and glitch["status"] == "rejected":
                answer = "Личный квест уже отменён без штрафа."
            else:
                answer = "Сейчас активного личного квеста сбоя нет. Если он появится, в личном сообщении будут точные условия и способ выполнения."
        self.send(token, msg, answer or "Скрытый свиток поддерживает только /quest, /rep и /book. Прочие просьбы здесь не выполняются.", kind)

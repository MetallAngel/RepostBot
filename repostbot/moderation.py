"""Telegram moderation actions backed by the shared grudge ledger."""

import time


class Moderation:
    def __init__(self, store, community, semantic_checker=None):
        self.store = store
        self.community = community
        # Worker.assess_offense is injected to keep the manual recheck able
        # to recognize a masked word that is not in the local lexicon yet.
        self.semantic_checker = semantic_checker

    def settings(self):
        get = self.store.setting
        return {
            "mode": "notify" if get("moderation_telegram_mode", "assistant") == "notify" else "assistant",
            "moderators": get("moderation_moderators", ""),
            "notify_min_points": int(get("moderation_notify_min_points", "20")),
            "notify_cooldown_minutes": int(get("moderation_notify_cooldown_minutes", "240")),
            "warning_min_points": int(get("moderation_warning_min_points", "10")),
            "warning_limit": int(get("moderation_warning_limit", "3")),
            "warning_days": int(get("moderation_warning_days", "7")),
            "delete_points": int(get("moderation_delete_points", "30")),
            "delete_banwords": get("moderation_delete_banwords", "1") == "1",
            "ban_enabled": get("moderation_ban_enabled", "0") == "1",
            "ban_points": int(get("moderation_ban_points", "100")),
            "warning_notice": get("moderation_warning_notice", "0") == "1",
            "ai_scan_enabled": get("community_ai_scan_enabled", "1") == "1",
            "ai_scan_mode": get("community_ai_scan_mode", "all"),
            "white_confirmations": int(get("community_white_confirmations", "1")),
            "media_scan_enabled": get("community_media_scan_enabled", "1") == "1",
        }

    def save_settings(self, data):
        mode = str(data.get("mode", "assistant"))
        scan_mode = str(data.get("ai_scan_mode", "all"))
        if mode not in ("notify", "assistant") or scan_mode not in ("all", "suspicious"):
            raise ValueError("Выберите способ модерации и режим проверки")
        confirmations = int(data.get("white_confirmations", self.settings()["white_confirmations"]))
        if confirmations not in (1, 2, 3):
            raise ValueError("Для белого списка укажите от 1 до 3 разных подтверждений")
        limits = {
            "notify_min_points": (1, 100), "notify_cooldown_minutes": (1, 10080),
            "warning_min_points": (1, 100), "warning_limit": (1, 50),
            "warning_days": (1, 365), "delete_points": (21, 100), "ban_points": (21, 100),
        }
        numbers = {}
        current = self.settings()
        for name, (minimum, maximum) in limits.items():
            value = int(data.get(name, current[name]))
            if not minimum <= value <= maximum:
                raise ValueError(name + f": допустимо {minimum}–{maximum}")
            numbers["moderation_" + name] = str(value)
        moderators = self.community.validate_mentions(data.get("moderators", ""))
        self.store.set_settings({"moderation_telegram_mode": mode,
                                 "moderation_moderators": moderators, "moderation_ban_enabled": "1" if data.get("ban_enabled") is True else "0",
                                 "moderation_delete_banwords": "1" if data.get("delete_banwords", True) is True else "0",
                                 "moderation_warning_notice": "1" if data.get("warning_notice") is True else "0",
                                 "community_ai_scan_enabled": "1" if data.get("ai_scan_enabled") is True else "0",
                                 "community_ai_scan_mode": scan_mode,
                                 "community_white_confirmations": str(confirmations),
                                 "community_media_scan_enabled": "1" if data.get("media_scan_enabled") is True else "0", **numbers})
        return self.settings()

    def recent(self, limit=80):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute(
                "SELECT e.*,p.name FROM moderation_events e LEFT JOIN community_participants p ON p.platform='telegram' AND p.user_id=e.user_id ORDER BY e.at DESC,e.message_id DESC LIMIT ?",
                (min(200, int(limit)),))]

    def incident_points(self, text, media_text, assessment):
        rules = list(self.community.matching_terms("telegram", text, media_text))
        return min(100, max(sum(term["points"] for term, _ in rules), (assessment or {}).get("points", 0)))

    def _notify_mods(self, token, chat_id, message_id, points, reason, settings):
        from .core import tg_call
        mentions = settings["moderators"]
        if not mentions or points < settings["notify_min_points"]:
            return False
        key = "moderation_last_ping:" + str(chat_id)
        now = int(time.time())
        with self.store.lock:
            if now - int(self.store.state(key, "0")) < 60 * settings["notify_cooldown_minutes"]:
                return False
            # Claim before network I/O so concurrent incidents cannot spam.
            self.store.set_state(key, now)
        payload = {"chat_id": chat_id, "text": mentions + ", проверьте запись Книги обид: " + reason + ". Нарушение оценено в " + str(points) + " баллов. Слова не повторяю."}
        if message_id and reason != "удалённое сообщение":
            payload["reply_parameters"] = {"message_id": message_id, "allow_sending_without_reply": True}
        try:
            tg_call(token, "sendMessage", payload)
            return True
        except Exception as exc:
            with self.store.lock:
                if self.store.state(key, "0") == str(now):
                    self.store.set_state(key, "0")
            self.store.event("error", "Модерация: не удалось позвать модератора: " + type(exc).__name__)
            return False

    def handle_telegram(self, token, msg, user_id, nick, points, edited=False, assessment=None):
        """Execute once per group message; return True when the offender message is gone."""
        from .core import tg_call
        chat = msg["chat"]
        if chat.get("type") not in ("group", "supergroup") or points <= 0:
            return False
        settings = self.settings()
        chat_id, message_id = str(chat["id"]), int(msg["message_id"])
        at = int(msg.get("date") or time.time())
        with self.store.lock, self.store.db:
            existing = self.store.db.execute("SELECT action,status FROM moderation_events WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone()
            if existing and not edited:
                return existing['action'] in ('delete', 'ban') and existing['status'].startswith(('выполнено', 'удалено'))
            if existing and edited:
                self.store.db.execute("DELETE FROM moderation_events WHERE chat_id=? AND message_id=?", (chat_id, message_id))
            warn = settings["warning_min_points"] <= points < settings["delete_points"]
            if warn:
                self.store.db.execute("INSERT OR IGNORE INTO moderation_warnings(chat_id,user_id,message_id,points,at) VALUES (?,?,?,?,?)",
                                      (chat_id, str(user_id), message_id, points, at))
            warnings = self.store.db.execute("SELECT COUNT(*) FROM moderation_warnings WHERE chat_id=? AND user_id=? AND at>=?",
                                             (chat_id, str(user_id), at - settings["warning_days"] * 86400)).fetchone()[0]
            has_banword = bool(self.community.matching_terms("telegram", msg.get("text", ""), ""))
            # The model's explicit severe classification is already in its
            # 21–100 range.  Do not turn it into an ordinary answer only
            # because the local deletion threshold is configured higher.
            semantic_severe = isinstance(assessment, dict) and assessment.get("kind") == "severe" and int(assessment.get("points", 0) or 0) >= 21
            should_delete = points >= settings["delete_points"] or semantic_severe or warnings >= settings["warning_limit"] or settings["delete_banwords"] and has_banword
            should_ban = should_delete and settings["ban_enabled"] and points >= settings["ban_points"]
            action = "ban" if should_ban else "delete" if should_delete else "warn" if warn else "record"
            self.store.db.execute("INSERT INTO moderation_events(chat_id,message_id,user_id,points,warnings,action,executor,status,at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (chat_id, message_id, str(user_id), points, warnings, action, settings["mode"], "recorded", at))
            if warn:
                self.store.db.execute("INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,reason,source_key,at) VALUES ('telegram',?,?,?,?,?)",
                                      (str(user_id), str(nick)[:100], f"Предупреждение модерации: {warnings}/{settings['warning_limit']} за {settings['warning_days']} дн. в чате {chat_id}", f"moderation_warning:telegram:{chat_id}:{message_id}", at))
        status = "только запись" if action == "record" else "предупреждение" if action == "warn" else "ожидает модератора"
        removed = False
        if action in ("delete", "ban") and settings["mode"] == "assistant":
            try:
                # Deleting a harmful message does not require checking its author.
                # An unavailable member lookup or an admin title must not delay deletion.
                tg_call(token, "deleteMessage", {"chat_id": chat["id"], "message_id": message_id})
                removed = True
                status = "выполнено"
                if action == "ban":
                    member = tg_call(token, "getChatMember", {"chat_id": chat["id"], "user_id": int(user_id)})
                    if member.get("status") in ("administrator", "creator"):
                        status = "удалено; бан администратора не выполнялся"
                    else:
                        tg_call(token, "banChatMember", {"chat_id": chat["id"], "user_id": int(user_id), "revoke_messages": False})
            except Exception as exc:
                detail = str(exc).replace(str(token), '[скрыто]')[:140]
                status = ("удалено; ошибка бана: " if removed else "удаление не выполнено: ") + type(exc).__name__ + ': ' + detail
                self.store.event("error", f"Модерация Telegram {chat_id}/{message_id}: {action}: {status}")
        elif action == "warn" and settings["warning_notice"] and not self.community.quiet_penalty(points):
            try:
                tg_call(token, "sendMessage", {"chat_id": chat["id"], "text": f"{nick}, писарь вынес предупреждение: {warnings}/{settings['warning_limit']}. Запись занесена в Книгу обид.",
                                               "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True}})
                status = "предупреждение отправлено"
            except Exception as exc:
                status = "предупреждение сохранено, отправка не удалась"
                self.store.event("error", "Модерация: не удалось отправить предупреждение: " + type(exc).__name__)
        if (settings["mode"] == "notify" and not self.community.quiet_penalty(points)) or action in ("delete", "ban") and not removed:
            if self._notify_mods(token, chat["id"], None if removed else message_id, points, "удалённое сообщение" if removed else action, settings):
                status += "; модератор вызван"
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE moderation_events SET status=? WHERE chat_id=? AND message_id=?", (status, chat_id, message_id))
        self.store.event("info", f"Модерация Telegram {chat_id}/{message_id}: {action}, оценка {points}, предупреждений {warnings}, {status}")
        return removed

    def scan_chat(self, token, chat_id, limit=500):
        """Recheck stored Telegram messages and delete clear local matches.

        Telegram does not expose group history to bots.  This therefore only
        checks messages that were actually saved while the bot was present.
        The Bot API also refuses most deletions after 48 hours, so those are
        reported separately instead of being shown as generic failures.
        """
        from .core import tg_call
        chat_id = str(chat_id)
        if not chat_id.startswith("-"):
            raise ValueError("Укажите отрицательный ID группы Telegram")
        limit = max(1, min(2000, int(limit)))
        settings = self.settings()
        threshold = settings["delete_points"]

        # Fail before scanning when the bot plainly cannot delete messages.
        # This gives the panel a useful explanation instead of 500 repeated
        # `message can't be deleted` errors.
        try:
            me = tg_call(token, "getMe", timeout=10)
            member = tg_call(token, "getChatMember", {"chat_id": chat_id, "user_id": me["id"]}, timeout=10)
        except Exception as exc:
            raise ValueError("Не удалось проверить права бота в группе: " + str(exc)[:220]) from exc
        status = str(member.get("status") or "")
        if status not in ("administrator", "creator"):
            raise ValueError("Бот не является администратором группы, поэтому Telegram не разрешит удаление сообщений")
        if status == "administrator" and member.get("can_delete_messages") is not True:
            raise ValueError("У бота нет права «Удаление сообщений» в этой группе")

        with self.store.lock:
            rows = [dict(row) for row in self.store.db.execute(
                "SELECT message_id,person,text,at FROM chat_memory WHERE chat=? ORDER BY message_id DESC LIMIT ?",
                (chat_id, limit))]
        now = int(time.time())
        cutoff = now - 48 * 3600
        bot_id = str(me.get("id") or "")
        scanned = matched = failed = already_gone = skipped_old = skipped_bot = 0
        deleted = model_checked = model_matches = model_errors = not_deletable = 0
        failures = []
        for row in rows:
            if bot_id and str(row.get("person") or "") == bot_id:
                skipped_bot += 1
                continue
            scanned += 1
            matches = list(self.community.matching_terms("telegram", row.get("text", ""), ""))
            points = self.incident_points(row.get("text", ""), "", None)
            assessment = None
            # A manual recheck is the recovery path for messages whose
            # obfuscation was not yet known to the local rules.  Run the same
            # semantic checker used by live moderation only for unmatched
            # rows, so ordinary configured banwords stay local and cheap.
            if not matches and self.semantic_checker:
                try:
                    assessment = self.semantic_checker(row.get("text", ""), "")
                    model_checked += 1
                    if isinstance(assessment, dict) and int(assessment.get("points", 0) or 0) > 0:
                        model_matches += 1
                except Exception as exc:
                    model_errors += 1
                    self.store.event("error", f"Сканирование модерации {chat_id}/{row['message_id']}: модель: {type(exc).__name__}")
            points = self.incident_points(row.get("text", ""), "", assessment)
            semantic_severe = isinstance(assessment, dict) and assessment.get("kind") == "severe" and int(assessment.get("points", 0) or 0) >= 21
            if points < threshold and not (settings["delete_banwords"] and matches) and not semantic_severe:
                continue
            matched += 1
            if int(row.get("at") or 0) and int(row["at"]) < cutoff:
                skipped_old += 1
                continue
            try:
                tg_call(token, "deleteMessage", {"chat_id": chat_id, "message_id": int(row["message_id"])})
                deleted += 1
            except Exception as exc:
                detail = str(exc)[:180]
                lower = detail.casefold()
                if "message to delete not found" in lower or "message not found" in lower:
                    already_gone += 1
                    # Do not offer the same vanished message on every scan.
                    with self.store.lock, self.store.db:
                        self.store.db.execute("DELETE FROM chat_memory WHERE chat=? AND message_id=?", (chat_id, int(row["message_id"])))
                    continue
                if "message can't be deleted" in lower or "message cant be deleted" in lower:
                    not_deletable += 1
                    failures.append({"message_id": row["message_id"], "kind": "not_deletable", "error": detail})
                else:
                    failed += 1
                    failures.append({"message_id": row["message_id"], "kind": "error", "error": detail})
                self.store.event("error", f"Сканирование модерации {chat_id}/{row['message_id']}: {type(exc).__name__}")
        return {"scanned": scanned, "matched": matched, "deleted": deleted, "failed": failed,
                "already_gone": already_gone, "not_deletable": not_deletable,
                "skipped_old": skipped_old, "skipped_bot": skipped_bot,
                "model_checked": model_checked, "model_matches": model_matches, "model_errors": model_errors,
                "failures": failures, "threshold": threshold,
                "rights": {"status": status, "can_delete_messages": member.get("can_delete_messages", status == "creator")}}

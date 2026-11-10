"""Chat statistics and the shared Twitch/Telegram reputation ledger."""

import hashlib
from difflib import SequenceMatcher
import importlib.util
import math
import re
import json
import secrets
import sqlite3
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .reply_behavior import GrudgeReply, StandaloneReply, TransitionReply, UnavailableReply, without_solicitation

BANWORD_PACK_FORMAT = "repostbot-banwords"
HONOR_TITLES = {"owner": "Владелец канала", "moderator": "Модератор", "king": "Король сообщество", "donor": "Топ донатер"}
IMMUNE_HONOR_CATEGORIES = frozenset(("owner", "moderator"))
DEFAULT_HONOR_MESSAGE = "Твой вклад в историю сообщества неизмерим."
RANK_PHRASES = {
    "neutral": "путник пока не оставил заметных следов в Книге",
    "positive_low": "добрая беседа только начинается",
    "positive_mid": "верный собеседник помощник",
    "positive_high": "почётный друг и союзник",
    "negative_low": "помощник хранит лёгкую обиду",
    "negative_mid": "в Книге осталась сильная обида",
    "negative_high": "обид накопилось слишком много",
}

# Fold common homoglyphs and leetspeak for a conservative, rule-based fallback.
# Only whole words (and explicitly suffixed rules) can match after folding.
_LOOKALIKES = str.maketrans({
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "н",
    "i": "и", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "r": "р",
    "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "у", "z": "з",
    "0": "о", "1": "и", "3": "з", "4": "ч", "5": "с", "6": "б", "8": "в", "@": "а", "$": "с",
})


def fold_banword_text(value):
    """Normalize a message for matching configured words, preserving word boundaries."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Cyrillic ``пи`` is often replaced with the leet-looking ``3.14``.
    value = re.sub(r"(?<!\w)3[.,]?14", "пи", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    # Common romanization uses a final c for Cyrillic ц (for example, in GIFs).
    value = re.sub(r"(?<=[a-z])c\b", "ц", value)
    value = value.translate(_LOOKALIKES)
    # Treat common evasive bars/leet punctuation as letters when they are
    # embedded in a word (for example ``|‑|ЕГРЫ`` -> ``негры``).
    value = re.sub(r"(?<!\w)[/\\|!]+[^\w\s]*(?=[а-яё])", "н", value)
    value = re.sub(r"[|!](?:[^\w\s]*[|!])+(?=\w)", "н", value)
    value = re.sub(r"[|]+", "н", value)
    value = re.sub(r"(?<=\w)[^\w\s]+(?=\w)", "", value)
    # Join separated letters, or letters divided by punctuation; never join
    # two ordinary words across a space.
    value = re.sub(r"(?<=\w)[._*\-~](?=\w)", "", value)
    value = re.sub(r"(?<!\w)(?:\w\s+){2,}\w(?!\w)", lambda match: re.sub(r"\s+", "", match.group()), value)
    return value


def lexicon_word(value):
    """Stable key for a verified whole word; preserve distinct languages."""
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _bounded(value, low, high):
    return max(low, min(high, int(value)))


def _fixed_score(value, default=100):
    if value is None or str(value).strip() == "":
        value = default
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise ValueError("Фиксированный уровень должен быть числом от −100 до +100 с шагом 0,5") from None
    if not math.isfinite(score) or not -100 <= score <= 100 or score * 2 != int(score * 2):
        raise ValueError("Фиксированный уровень должен быть от −100 до +100 с шагом 0,5")
    return score


def _honor_fixed_score(data, category):
    if category in ("king", "donor"):
        return None  # The separate immunity entry owns this person's fixed level.
    enabled = data.get("fixed_enabled")
    if enabled is None:
        enabled = category in IMMUNE_HONOR_CATEGORIES or data.get("fixed_score") not in (None, "")
    elif enabled not in (True, False):
        enabled = str(enabled).casefold() in ("1", "true", "on")
    if enabled and category == "custom" and data.get("fixed_score") in (None, ""):
        raise ValueError("Для своего закреплённого звания укажите уровень от −100 до +100")
    return _fixed_score(data.get("fixed_score")) if enabled else None


DEFAULT_TEMPLATES = {
    "hint": "Перо писаря дрогнуло: старая обида ещё не стёрта.\nНа полях Книги осталась обида после нашей размолвки.\nпомощник немного обижена, но дверь к беседе пока открыта.",
    "strong": "Чернила тяжёлой обиды ещё не высохли.\nСтрока в Книге потемнела: обида всё ещё сильна.\nМежду нами тяжёлая глава обиды в летописи, путник.",
    "refuse": "Книга обид захлопнута. После прежних поступков я не расположена к беседе.\nСлишком тяжёлые записи обид лежат между нами. Сегодня обойдёмся без разговора.\nПисарь отложил перо: обид слишком много. На обычный вопрос сейчас не отвечу.",
    "help": "Даже тяжёлую запись можно погасить: используй подходящую награду за баллы канала или попроси владельца пересмотреть её.\nКнига не закрыта навеки. Награда канала или решение владельца помогут погасить обиду.\nЧтобы погасить обиду, узнай о награде за баллы канала или обратись к владельцу: он может снять запись вручную.",
    "greeting": "Рада видеть тебя снова, {user}! На доброй странице Книги нашлось место для тебя. 💚\nО, {user}! помощник выглянула из-за Книги и машет тебе лапой. 💚\nС возвращением, {user}! Сегодня писарь приготовил для тебя тёплую строку. 💚",
    "improvement": "помощник фыркает и всё ещё плохо к тебе относится, но твоя репутация всё-таки становится лучше.\nПисарь хмурится, однако видит улучшение: твоя репутация понемногу растёт.\nОбида ещё держится, но Книга уже замечает добрые перемены — рейтинг стал лучше.",
    "banword": "{user}, писарь услышал запретное слово и сделал пометку в Книге обид.\nОх, {user}! Перо само потянулось к странице обид.\n{user}, ещё одна строка обиды появилась в Книге. Надеюсь, мы уладим эту историю.",
    "severe": "{user}, тяжёлая запись занесена в Книгу обид. {mentions}\n{user}, писарь отметил тяжёлую обиду в Книге. {mentions}\n{user}, Книга обид пополнилась тяжёлой записью. {mentions}",
    "activity": "{user}, писарь заметил твой вклад в беседу. Добрая строка в Книге прибавилась.\n{user}, помощник кивнула: твои слова добавили немного почёта.\nЛетопись отмечает твоё участие, {user}. Продолжай беседу!",
    "reward": "{user} принёс награду «{reward}». Писарь отметил событие в Книге.\nО, награда «{reward}» от {user}! помощник перелистнула страницу с улыбкой.\nВ летописи появилась запись о награде «{reward}» от {user}.",
    "reply_grudge": "📖 В Книге обид появилась новая строка. Такое обращение я запомню.\n📖 Писарь занёс новую обиду в Книгу. Эта реплика не останется без пометки.\n📖 Перо скрипнуло: ещё одна обида записана в Книгу.",
    "reply_roleplay": "📖 Шалость занесена в Книгу обид. Лапы помнят, и писарь тоже.\n📖 В Книге обид появилась игровая пометка. Эту проделку я запомню.\n📖 Писарь записал новую игровую обиду. Теперь у этой шалости есть своя строка.",
    "transition_hint": "📖 {user}, на спокойной странице летописи появилась обида. помощник нахмурилась.\n📖 {user}, писарь сделал новую пометку: между нами поселилась лёгкая обида.\n📖 {user}, перо коснулось страницы обид. помощник запомнит эту размолвку.",
    "transition_strong": "📖 {user}, эта запись утяжелила летопись: обида стала сильной. помощник теперь заметно холоднее.\n📖 {user}, писарь перевернул страницу: обида стала сильной. Прежней теплоты в беседе пока не будет.\n📖 {user}, новая строка перевесила терпение помощник. В летописи теперь сильная обида.",
    "transition_refuse": "📖 {user}, последняя запись переполнила страницу обид. Книга захлопнулась; обычная беседа теперь закрыта.\n📖 {user}, писарь отложил перо: накопилось слишком много обид. помощник больше не отвечает на обычные вопросы.\n📖 {user}, после этой записи летопись потемнела окончательно. помощник не расположена к обычной беседе.",
}
OLD_TEMPLATES = {
    "hint": "Я помню прежнюю обиду, но готова поговорить.",
    "strong": "Я сильно обижена, но отвечу тебе.",
    "refuse": "Накопилось слишком много обид. Пока они не будут улажены, я не готова к обычной беседе. Спроси, как погасить обиду.",
    "help": "Обида сильная, но её можно погасить наградой канала или попросить владельца пересмотреть запись. Донат владелец может учесть вручную — уточни у него.",
}
PREVIOUS_TEMPLATES = {
    "refuse": ("В Книге обид скопилось слишком много твоих записей. Обычную беседу отложим; спроси, как уладить дело.\nПисарь захлопнул Книгу: обид накопилось слишком много. Спроси меня, как примириться.\nпомощник хранит слишком тяжёлую обиду для обычного разговора. Давай сперва узнаем, как её погасить.",
               "Книга обид захлопнута. Сперва уладь старые записи; спроси, как примириться.\nС такими обидами беседы не будет. Сначала погаси запись в Книге.\nПисарь отложил перо: обид слишком много. Сперва примирение — потом разговор."),
    "hint": ("Перо писаря дрогнуло: я ещё обижена, но слушаю тебя.\nНа полях Книги осталась обида. Говори, путник, я отвечу.\nпомощник немного обижена, но дверь к беседе пока открыта.",
             "Перо писаря дрогнуло: старая обида ещё не стёрта. Но я слушаю.\nНа полях Книги осталась пометка о нашей размолвке. Говори, путник.\nпомощник припомнила прежнюю запись, но дверь к беседе пока открыта.",
             "Перо писаря дрогнуло: старая обида ещё не стёрта.\nНа полях Книги осталась пометка о нашей размолвке.\nпомощник немного обижена, но дверь к беседе пока открыта."),
    "strong": ("Чернила на странице обиды ещё не высохли. помощник пока хранит молчание.\nСтрока в Книге потемнела; обычные вопросы останутся без ответа.\nМежду нами тяжёлая глава летописи. Писарь пока не поднимает пера.",),
}


try:
    _GREETING_ZONE = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    _GREETING_ZONE = timezone(timedelta(hours=3))


class Community:
    def __init__(self, store):
        self.store = store
        self._term_cache = None
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS community_transitions (platform TEXT NOT NULL,user_id TEXT NOT NULL,message_key TEXT NOT NULL,nick TEXT NOT NULL,level TEXT NOT NULL,notice TEXT NOT NULL DEFAULT '',notified INTEGER NOT NULL DEFAULT 0,at INTEGER NOT NULL,PRIMARY KEY(platform,user_id,message_key))")

    @staticmethod
    def template_key(kind):
        if kind not in DEFAULT_TEMPLATES:
            raise ValueError("Неизвестный шаблон Книги")
        return "community_" + ("grudge_" if kind in ("hint", "strong", "refuse", "help") else "message_") + kind

    def template_text(self, kind):
        value = self.store.setting(self.template_key(kind), DEFAULT_TEMPLATES[kind])
        return DEFAULT_TEMPLATES[kind] if value == OLD_TEMPLATES.get(kind) or value in PREVIOUS_TEMPLATES.get(kind, ()) else value

    @staticmethod
    def validate_templates(value):
        variants = [line.strip() for line in str(value).splitlines() if line.strip()]
        if not 1 <= len(variants) <= 12 or len(str(value)) > 2400 or any(len(line) > 450 for line in variants):
            raise ValueError("Для каждого шаблона укажите от 1 до 12 строк, до 450 символов в строке")
        return "\n".join(dict.fromkeys(variants))

    def choose_message(self, kind, platform, user_id, custom=None, values=None):
        source = str(custom if custom else self.template_text(kind))
        variants = [line.strip() for line in source.splitlines() if line.strip()]
        if not variants:
            variants = [DEFAULT_TEMPLATES[kind].splitlines()[0]]
        accounts = self._accounts(platform, user_id)
        memory_key = "community_variant:" + kind + ":" + ":".join(p + "=" + uid for p, uid in sorted(accounts))
        with self.store.lock:
            previous = [self.store.state(memory_key, "")] + [self.store.state("community_variant:account:" + kind + ":" + p + ":" + uid, "") for p, uid in accounts]
            available = [line for line in variants if line not in previous] or [line for line in variants if line != previous[0]] or variants
            chosen = secrets.choice(available)
            self.store.set_state(memory_key, chosen)
            for p, uid in accounts:
                self.store.set_state("community_variant:account:" + kind + ":" + p + ":" + uid, chosen)
        for key, value in (values or {}).items():
            chosen = chosen.replace("{" + key + "}", str(value))
        chosen = without_solicitation(chosen, DEFAULT_TEMPLATES[kind].splitlines()[0])
        return chosen[:500]

    @staticmethod
    def validate_mentions(value):
        names = [item.lstrip("@") for item in re.split(r"[,\s]+", str(value or "").strip()) if item]
        if len(names) > 5 or any(not re.fullmatch(r"[A-Za-z0-9_]{4,25}", name) for name in names):
            raise ValueError("Укажите до пяти ников вида @name через запятую")
        return ", ".join("@" + name for name in dict.fromkeys(names))

    def offender_mention(self, platform, user_id, fallback):
        with self.store.lock:
            row = self.store.db.execute("SELECT username FROM community_usernames WHERE platform=? AND user_id=?", (platform, str(user_id))).fetchone()
        return row[0] if row and re.fullmatch(r"[A-Za-z0-9_]{4,25}", row[0]) else str(fallback)[:50]

    def notification_enabled(self, kind, platform):
        return (self.store.setting("community_notify_" + kind, "1" if kind == "transition" else "0") == "1"
                and self.store.setting("community_notify_" + kind + "_" + platform, "1") == "1")

    def quiet_penalty(self, points):
        """Small incidents stay in the ledger and inside a reply that was already due."""
        return 0 < points <= int(self.store.setting("community_quiet_penalty_max", "10"))

    def record_transition(self, platform, user_id, nick, message_key, before, after, at=None):
        """Called with the ledger lock held, only after a real new penalty."""
        order = {"normal": 0, "hint": 1, "strong": 2, "refuse": 3}
        old, new = self.score_attitude(before), self.score_attitude(after)
        if order[new] <= order[old]:
            return
        key = (platform, str(user_id), str(message_key))
        row = self.store.db.execute("SELECT level,notified FROM community_transitions WHERE platform=? AND user_id=? AND message_key=?", key).fetchone()
        if row and (row['notified'] or order[row['level']] >= order[new]):
            return
        self.store.db.execute("INSERT INTO community_transitions(platform,user_id,message_key,nick,level,at) VALUES (?,?,?,?,?,?) ON CONFLICT(platform,user_id,message_key) DO UPDATE SET level=excluded.level,notice=''",
                              (*key, str(nick).replace('@', '')[:100], new, int(at or time.time())))

    def transition_notice(self, platform, user_id, chat, message_keys, claim=True):
        keys = message_keys if isinstance(message_keys, (list, tuple)) else [message_keys]
        keys = sorted({str(chat) + ':' + str(key) if platform == 'telegram' and chat and not str(key).startswith(str(chat) + ':') else str(key) for key in keys if key})[:20]
        if not keys or not user_id or self.store.setting('community_enabled', '0') != '1':
            return None
        with self.store.lock, self.store.db:
            rows = self.store.db.execute("SELECT * FROM community_transitions WHERE platform=? AND user_id=? AND message_key IN (" + ','.join('?' for _ in keys) + ") AND notified=0 ORDER BY at DESC,message_key DESC", (platform, str(user_id), *keys)).fetchall()
            rows = sorted(rows, key=lambda row: {'hint': 1, 'strong': 2, 'refuse': 3}[row['level']], reverse=True)
            if not rows:
                return None
            row = rows[0]
            key = (platform, str(user_id), row['message_key'])
            accounts = self._accounts(platform, user_id)
            clocks = ['community_transition_last:' + row['level'] + ':' + p + ':' + uid for p, uid in accounts]
            now = int(time.time())
            pause = int(self.store.setting('community_transition_cooldown_minutes', '5')) * 60
            last = max((int(self.store.state(clock, '0')) for clock in clocks), default=0)
            enabled = (self.notification_enabled('transition', platform)
                       and self.store.setting('community_transition_' + row['level'], '0' if row['level'] == 'hint' else '1') == '1')
            order = {'normal': 0, 'hint': 1, 'strong': 2, 'refuse': 3}
            enabled = enabled and order[self.attitude(platform, user_id)] >= order[row['level']]
            if not enabled or last and now - last < pause:
                if claim:
                    self.store.db.executemany('UPDATE community_transitions SET notified=2 WHERE platform=? AND user_id=? AND message_key=?', [(platform, str(user_id), item['message_key']) for item in rows])
                return None
            notice = row['notice'] or self.redact_terms(self.choose_message('transition_' + row['level'], platform, user_id, values={'user': row['nick']}))
            if not row['notice']:
                self.store.db.execute('UPDATE community_transitions SET notice=? WHERE platform=? AND user_id=? AND message_key=?', (notice, *key))
            if claim:
                self.store.db.executemany('UPDATE community_transitions SET notified=1 WHERE platform=? AND user_id=? AND message_key=?', [(platform, str(user_id), item['message_key']) for item in rows])
                for clock in clocks:
                    self.store.set_state(clock, now)
            return TransitionReply(notice, platform=platform, claimed=claim, message_keys=keys)

    def transition_reply(self, answer, platform, user_id, chat, message_keys, claim=True):
        if isinstance(answer, TransitionReply) and answer.claimed:
            return answer
        if isinstance(answer, TransitionReply) and answer.message_keys:
            message_keys = answer.message_keys
        notice = self.transition_notice(platform, user_id, chat, message_keys, claim=claim)
        body = answer.body if isinstance(answer, TransitionReply) else answer
        if not notice:
            return body
        if body and not isinstance(body, UnavailableReply):
            # Replace stock tone/receipt prefixes, retaining the actual answer or help.
            stock = {line.strip() for kind in ('hint', 'strong', 'refuse', 'reply_grudge', 'reply_roleplay') for line in self.template_text(kind).splitlines()}
            gap = '  |  ' if platform == 'twitch' else '\n\n'
            for _ in range(4):
                head, sep, tail = str(body).partition(gap)
                if head.strip() not in stock:
                    break
                body = tail if sep else ''
            if str(body).strip() in stock:
                body = ''
        return TransitionReply(str(notice), body, platform, claimed=claim, message_keys=notice.message_keys)

    def current_grudge(self, platform, user_id, chat, message_keys):
        """Bounded lookup of actual penalties for this message, never an old debt."""
        if not user_id or self.store.setting("community_enabled", "0") != "1":
            return None
        keys = message_keys if isinstance(message_keys, (tuple, list)) else [message_keys]
        keys = {str(key) for key in keys if key}
        full = {str(chat) + ":" + key if platform == "telegram" and not key.startswith(str(chat) + ":") else key for key in keys}
        for key in sorted(full):
            word, semantic, roleplay = "word:" + platform + ":" + key + ":", "semantic:" + platform + ":" + key, "roleplay:" + platform + ":" + key
            with self.store.lock:
                row = self.store.db.execute("SELECT source_key FROM grudge_ledger WHERE platform=? AND user_id=? AND grudge_delta>0 AND (source_key IN (?,?) OR substr(source_key,1,?)=?) ORDER BY id DESC LIMIT 1", (platform, str(user_id), semantic, roleplay, len(word), word)).fetchone()
            if row:
                return {"kind": "roleplay" if row[0] == roleplay else "grudge"}
        return None

    def grudge_reply(self, answer, platform, user_id, chat, message_keys):
        """A receipt stays inside an existing reply; it never starts a notification."""
        if answer and not isinstance(answer, UnavailableReply):
            answer = self.transition_reply(answer, platform, user_id, chat, message_keys, claim=False)
        if isinstance(answer, TransitionReply):
            return answer
        if not answer or isinstance(answer, UnavailableReply) or self.store.setting("community_reply_receipt_" + platform, "1") != "1":
            return answer
        event = self.current_grudge(platform, user_id, chat, message_keys)
        if not event or re.fullmatch(r"\[\[STICKER:[^\]]+\]\]", str(answer).strip()):
            return answer
        if any(line.strip() and line.strip() in answer for kind in ("reply_grudge", "reply_roleplay") for line in self.template_text(kind).splitlines()):
            return answer
        if re.search(r"(?:внес[лё]\w*|занес[лё]\w*|записал\w*|добавил\w*|попал\w*).{0,55}(?:книг\w*\s+обид|игров\w*\s+(?:обид|помет))|(?:книг\w*\s+обид).{0,55}(?:появил\w*|нов\w*\s+(?:строк|запис|помет))|(?:обид\w*|пометк\w*).{0,35}(?:записан\w*|занес[её]н\w*)", str(answer), re.I):
            return answer
        receipt = self.redact_terms(self.choose_message("reply_roleplay" if event["kind"] == "roleplay" else "reply_grudge", platform, user_id))
        return receipt + " · " + answer if platform == "twitch" else GrudgeReply(receipt, answer, platform)

    def roleplay_memory(self, platform, user_id, limit=5):
        """Summarize remembered mascot-directed roleplay incidents."""
        if not user_id or self.store.setting("community_enabled", "0") != "1":
            return {"count": 0, "recent": []}
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT reason,at,favor_delta,grudge_delta FROM grudge_ledger WHERE platform=? AND user_id=? AND (source_key LIKE 'roleplay:%' OR source_key LIKE 'roleplay_good:%') ORDER BY at DESC,id DESC LIMIT ?",
                (platform, str(user_id), max(1, min(10, int(limit))))).fetchall()
        return {"count": len(rows), "positive": sum(1 for row in rows if float(row["favor_delta"] or 0) > 0),
                "negative": sum(1 for row in rows if float(row["grudge_delta"] or 0) > 0),
                "recent": [str(row["reason"] or "")[:160] for row in rows]}

    def compiled_terms(self):
        with self.store.lock:
            if self._term_cache is None:
                rows = [dict(row) for row in self.store.db.execute("SELECT * FROM grudge_terms ORDER BY id")]
                variants = {}
                for row in self.store.db.execute("SELECT group_id,phrase FROM grudge_variants ORDER BY id"):
                    variants.setdefault(row["group_id"], []).append(row["phrase"])
                self._term_cache = tuple((dict(row) | {"variant": phrase}, re.compile(self.term_pattern(phrase), re.I))
                                         for row in rows for phrase in [row["phrase"], *variants.get(row["id"], ())])
            return self._term_cache

    @staticmethod
    def term_matches(term, pattern, message, folded=None):
        if pattern.search(message):
            return True
        # Do not turn a very short rule into a broad fuzzy match.
        if len(term.get("variant", term["phrase"]).rstrip("*")) < 4:
            return False
        variant = term.get("variant", term["phrase"])
        normalized = fold_banword_text(variant)
        if normalized == variant and folded == message:
            return False
        return bool(re.search(Community.term_pattern(normalized), folded if folded is not None else fold_banword_text(message), re.I))

    def matching_terms(self, platform, text, media_text=""):
        text = str(text or "")
        media_text = str(media_text or "")
        folded_text = fold_banword_text(text)
        folded_media = fold_banword_text(media_text)
        seen = set()
        for term, pattern in self.compiled_terms():
            if term["enabled"] and term["scope"] in (platform, "both"):
                caption = self.term_matches(term, pattern, text, folded_text)
                if caption or self.term_matches(term, pattern, media_text, folded_media):
                    normalized = fold_banword_text(term["phrase"])
                    if term["id"] in seen or normalized in seen:
                        continue
                    seen.add(term["id"])
                    seen.add(normalized)
                    yield {key: value for key, value in term.items() if key != "variant"}, caption

    def seen_message(self, platform, message_key):
        with self.store.lock:
            return self.store.db.execute("SELECT 1 FROM community_activity WHERE platform=? AND message_key=?",
                                         (platform, str(message_key))).fetchone() is not None

    def terms(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute("SELECT * FROM grudge_terms ORDER BY id")]

    def term_count(self):
        with self.store.lock:
            return self.store.db.execute("SELECT COUNT(*) FROM grudge_terms").fetchone()[0]

    def check_terms(self, platform, message):
        """Preview the same matcher used for live messages without writing a ledger entry."""
        if platform not in ("twitch", "telegram"):
            raise ValueError("Выберите Twitch или Telegram")
        message = str(message or "")
        if not 1 <= len(message) <= 2000:
            raise ValueError("Введите сообщение длиной от 1 до 2000 символов")
        normalized = fold_banword_text(message)
        matches = [{"id": term["id"], "phrase": term["phrase"], "points": term["points"], "enabled": bool(term["enabled"])}
                   for term, _ in self.matching_terms(platform, message)]
        disabled = {item["id"] for item in matches}
        for term, pattern in self.compiled_terms():
            if term["id"] not in disabled and not term["enabled"] and term["scope"] in (platform, "both") and self.term_matches(term, pattern, message, normalized):
                matches.append({"id": term["id"], "phrase": term["phrase"], "points": term["points"], "enabled": False})
                disabled.add(term["id"])
        return {"book_enabled": self.store.setting("community_enabled", "0") == "1", "matches": matches,
                "would_apply": [item for item in matches if item["enabled"] and self.store.setting("community_enabled", "0") == "1"]}

    @staticmethod
    def term_pattern(phrase):
        """A trailing * matches inflected endings, without matching inside words."""
        stem = phrase[:-1] if phrase.endswith("*") else phrase
        return r"(?<!\w)" + re.escape(stem) + (r"\w*" if phrase.endswith("*") else "") + r"(?!\w)"

    def redact_terms(self, message):
        if isinstance(message, GrudgeReply):
            return GrudgeReply(self.redact_terms(message.notice), self.redact_terms(message.body), message.platform)
        result = str(message or "")
        for _, pattern in sorted(self.compiled_terms(), key=lambda item: len(item[0]["phrase"]), reverse=True):
            result = pattern.sub("[запретное слово]", result)
        return result

    def export_terms(self):
        with self.store.lock:
            groups = self.terms()
            return {"format": BANWORD_PACK_FORMAT, "version": 2,
                    "terms": [{**{key: item[key] for key in ("phrase", "scope", "points", "response")},
                               "enabled": bool(item["enabled"]), "variants": [row[0] for row in self.store.db.execute(
                                   "SELECT phrase FROM grudge_variants WHERE group_id=? ORDER BY phrase", (item["id"],))]}
                              for item in groups],
                    "white": [row[0] for row in self.store.db.execute("SELECT word FROM lexicon_allow ORDER BY word")]}

    def import_terms_pack(self, pack, preview=False, overwrite=False):
        if not isinstance(pack, dict) or pack.get("format") != BANWORD_PACK_FORMAT or pack.get("version") not in (1, 2):
            raise ValueError("Ожидается JSON-файл банвордов RepostBot версии 1 или 2")
        entries = pack.get("terms")
        if not isinstance(entries, list) or len(entries) > 2000:
            raise ValueError("В файле должно быть не более 2000 правил")
        prepared = {}
        imported_variants = {}
        seen_variants = set()
        for index, item in enumerate(entries, 1):
            if not isinstance(item, dict):
                raise ValueError(f"Правило {index}: неверная запись")
            phrase = str(item.get("phrase", "")).strip().casefold()
            scope = item.get("scope", "both")
            points = item.get("points")
            response = item.get("response", "")
            enabled = item.get("enabled", True)
            if (not 2 <= len(phrase) <= 80 or phrase.endswith("*") and len(phrase) < 4 or phrase.count("*") > 1
                    or ("*" in phrase and not phrase.endswith("*")) or "|" in phrase or "\n" in phrase):
                raise ValueError(f"Правило {index}: недопустимая фраза")
            if scope not in ("both", "twitch", "telegram") or type(points) is not int or not 1 <= points <= 100 or type(enabled) is not bool or not isinstance(response, str):
                raise ValueError(f"Правило {index}: неверная площадка, баллы, ответ или состояние")
            if response:
                response = self.validate_templates(response)
            if phrase in prepared:
                raise ValueError(f"Правило {index}: повтор фразы в файле")
            prepared[phrase] = (phrase, scope, points, response, int(enabled))
            variants = item.get("variants", []) if pack["version"] == 2 else []
            if not isinstance(variants, list) or len(variants) > 500:
                raise ValueError(f"Правило {index}: слишком много вариантов")
            imported_variants[phrase] = []
            for raw in variants:
                variant = self._lexicon_phrase(raw)
                if variant == phrase or variant in seen_variants or variant in prepared:
                    continue
                imported_variants[phrase].append(variant)
                seen_variants.add(variant)
        if sum(map(len, imported_variants.values())) > 20000:
            raise ValueError("В файле слишком много вариантов")
        white = pack.get("white", []) if pack["version"] == 2 else []
        if not isinstance(white, list) or len(white) > 50000 or any(not isinstance(word, str) or not re.fullmatch(r"\w{1,48}", lexicon_word(word), re.UNICODE) for word in white):
            raise ValueError("Неверный белый список")
        with self.store.lock, self.store.db:
            existing = {row["phrase"]: tuple(row[key] for key in ("phrase", "scope", "points", "response", "enabled"))
                        for row in self.store.db.execute("SELECT * FROM grudge_terms")}
            added = updated = unchanged = conflicts = 0
            for phrase, value in prepared.items():
                previous = existing.get(phrase)
                if previous is None:
                    added += 1
                    if not preview:
                        self.store.db.execute("INSERT INTO grudge_terms(phrase,scope,points,response,enabled) VALUES (?,?,?,?,?)", value)
                elif previous == value:
                    unchanged += 1
                elif overwrite:
                    updated += 1
                    if not preview:
                        self.store.db.execute("UPDATE grudge_terms SET scope=?,points=?,response=?,enabled=? WHERE phrase=?", (value[1], value[2], value[3], value[4], phrase))
                else:
                    conflicts += 1
            if not preview and (added or updated):
                self._term_cache = None
            variants_added = white_added = 0
            for phrase, variants in imported_variants.items():
                group = self.store.db.execute("SELECT id FROM grudge_terms WHERE phrase=?", (phrase,)).fetchone()
                if group or preview and phrase in prepared:
                    for variant in variants:
                        if self.store.db.execute("SELECT 1 FROM grudge_variants WHERE phrase=?", (variant,)).fetchone():
                            continue
                        if self.store.db.execute("SELECT 1 FROM grudge_terms WHERE phrase=?", (variant,)).fetchone():
                            continue
                        variants_added += 1
                        if not preview:
                            self.store.db.execute("INSERT INTO grudge_variants(group_id,phrase,origin,at) VALUES (?,?,'import',?)", (group["id"], variant, int(time.time())))
                            self.store.db.execute("DELETE FROM lexicon_allow WHERE word=?", (variant,))
            if not preview and variants_added:
                self._term_cache = None
            for raw in white:
                word = lexicon_word(raw)
                if self.store.db.execute("SELECT 1 FROM lexicon_allow WHERE word=?", (word,)).fetchone() or any(any(self.matching_terms(p, word)) for p in ("telegram", "twitch")):
                    continue
                white_added += 1
                if not preview:
                    self.store.db.execute("INSERT INTO lexicon_allow(word,at,source) VALUES (?,?,'import')", (word, int(time.time())))
        return {"added": added, "updated": updated, "unchanged": unchanged, "conflicts": conflicts, "variants_added": variants_added, "white_added": white_added, "preview": bool(preview)}

    def save_term(self, data):
        phrase = str(data.get("phrase", "")).strip().casefold()
        scope = str(data.get("scope", "both"))
        response = str(data.get("response", "")).strip()
        if (not 2 <= len(phrase) <= 80 or phrase.endswith("*") and len(phrase) < 4 or phrase.count("*") > 1
                or ("*" in phrase and not phrase.endswith("*")) or "|" in phrase or "\n" in phrase
                or scope not in ("twitch", "telegram", "both")):
            raise ValueError("Укажите слово/фразу от 2 до 80 символов и площадку")
        if response:
            response = self.validate_templates(response)
        points = _bounded(data.get("points", 3), 1, 100)
        with self.store.lock, self.store.db:
            try:
                owner = self.store.db.execute("SELECT group_id FROM grudge_variants WHERE phrase=?", (phrase,)).fetchone()
                if owner and str(owner[0]) != str(data.get("id") or ""):
                    raise ValueError("Это написание уже находится в другой группе")
                if data.get("id"):
                    cur = self.store.db.execute("UPDATE grudge_terms SET phrase=?,scope=?,points=?,response=?,enabled=? WHERE id=?", (phrase, scope, points, response, int(bool(data.get("enabled", True))), int(data["id"])))
                    if not cur.rowcount:
                        raise ValueError("Правило не найдено")
                    self._term_cache = None
                    return int(data["id"])
                row_id = self.store.db.execute("INSERT INTO grudge_terms(phrase,scope,points,response,enabled) VALUES (?,?,?,?,?)", (phrase, scope, points, response, int(bool(data.get("enabled", True))))).lastrowid
                self._term_cache = None
                return row_id
            except sqlite3.IntegrityError:
                raise ValueError("Такая фраза уже добавлена") from None

    def delete_term(self, row_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM grudge_variants WHERE group_id=?", (int(row_id),))
            self.store.db.execute("DELETE FROM grudge_terms WHERE id=?", (int(row_id),))
            self._term_cache = None

    def set_term_enabled(self, row_id, enabled):
        if not str(row_id).isdigit() or type(enabled) is not bool:
            raise ValueError("Укажите правило и состояние: включено или выключено")
        with self.store.lock, self.store.db:
            cur = self.store.db.execute("UPDATE grudge_terms SET enabled=? WHERE id=?", (int(enabled), int(row_id)))
            if not cur.rowcount:
                raise ValueError("Банворд не найден")
            self._term_cache = None
        return {"id": int(row_id), "enabled": enabled}

    @staticmethod
    def _lexicon_phrase(value):
        phrase = lexicon_word(value)
        if not 2 <= len(phrase) <= 80 or "|" in phrase or any(ch in phrase for ch in "\r\n<>"):
            raise ValueError("Слово или вариант: 2–80 символов без переноса строки")
        return phrase

    def lexicon(self, query="", offset=0, limit=50, kind="both"):
        query = lexicon_word(query)[:80]
        offset, limit = max(0, min(1000000, int(offset))), min(100, max(1, int(limit)))
        if kind not in ("both", "groups", "white"):
            raise ValueError("Неизвестный список слов")
        groups, white, total, white_total = [], [], 0, 0
        with self.store.lock:
            where = "WHERE phrase LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM grudge_variants v WHERE v.group_id=g.id AND v.phrase LIKE ? ESCAPE '\\')" if query else ""
            needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params = (needle,) * 2 if query else ()
            if kind != "white":
                total = self.store.db.execute("SELECT COUNT(*) FROM grudge_terms g " + where, params).fetchone()[0]
                groups = [dict(row) for row in self.store.db.execute("SELECT g.*, (SELECT COUNT(*) FROM grudge_variants v WHERE v.group_id=g.id) AS variant_count FROM grudge_terms g " + where + " ORDER BY g.phrase LIMIT ? OFFSET ?", (*params, limit, offset))]
                if kind == "both":
                    for item in groups:
                        item["variants"] = [dict(row) for row in self.store.db.execute("SELECT id,phrase,origin FROM grudge_variants WHERE group_id=? ORDER BY phrase", (item["id"],))]
            if kind != "groups":
                allow_query = "WHERE word LIKE ? ESCAPE '\\'" if query else ""
                white_total = self.store.db.execute("SELECT COUNT(*) FROM lexicon_allow " + allow_query, (needle,) if query else ()).fetchone()[0]
                white = [dict(row) for row in self.store.db.execute("SELECT word,at,source FROM lexicon_allow " + allow_query + " ORDER BY word LIMIT ? OFFSET ?", ((needle,) if query else ()) + (limit, offset))]
        return {"groups": groups, "groups_total": total, "white": white, "white_total": white_total, "offset": offset, "limit": limit}

    def lexicon_group(self, group_id, offset=0, limit=40):
        offset, limit = max(0, min(1000000, int(offset))), min(100, max(1, int(limit)))
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM grudge_terms WHERE id=?", (int(group_id),)).fetchone()
            if row is None:
                raise ValueError("Группа банворда не найдена")
            group = dict(row)
            group["variant_count"] = self.store.db.execute("SELECT COUNT(*) FROM grudge_variants WHERE group_id=?", (group_id,)).fetchone()[0]
            group["variants"] = [dict(item) for item in self.store.db.execute(
                "SELECT id,phrase,origin FROM grudge_variants WHERE group_id=? ORDER BY phrase LIMIT ? OFFSET ?", (group_id, limit, offset))]
        return {"group": group, "offset": offset, "limit": limit}

    def candidate_group_heads(self, text):
        tokens = [fold_banword_text(part) for part in re.findall(r"[\w@$!]+", str(text or "")[:1500]) if len(part) >= 4][:30]
        if not tokens:
            return []
        candidates = []
        for group in self.terms():
            if not group["enabled"]:
                continue
            head = fold_banword_text(group["phrase"].rstrip("*"))
            score = max((SequenceMatcher(None, token, head).ratio() for token in tokens), default=0)
            if score >= 0.55:
                candidates.append((score, group["phrase"]))
        return [phrase for _, phrase in sorted(candidates, reverse=True)[:5]]

    def add_variant(self, group_id, value, origin="manual"):
        phrase = self._lexicon_phrase(value)
        with self.store.lock, self.store.db:
            group = self.store.db.execute("SELECT id,phrase FROM grudge_terms WHERE id=?", (int(group_id),)).fetchone()
            if not group:
                raise ValueError("Группа банворда не найдена")
            if phrase == group["phrase"]:
                return {"group_id": group["id"], "phrase": phrase}
            if self.store.db.execute("SELECT 1 FROM grudge_terms WHERE phrase=?", (phrase,)).fetchone():
                raise ValueError("Это слово уже является отдельной группой")
            existing = self.store.db.execute("SELECT group_id FROM grudge_variants WHERE phrase=?", (phrase,)).fetchone()
            if existing and existing[0] != group["id"]:
                raise ValueError("Этот вариант уже находится в другой группе")
            self.store.db.execute("INSERT OR IGNORE INTO grudge_variants(group_id,phrase,origin,at) VALUES (?,?,?,?)", (group["id"], phrase, origin, int(time.time())))
            self.store.db.execute("DELETE FROM lexicon_allow WHERE word=?", (phrase,))
            self.store.db.execute("DELETE FROM lexicon_candidates WHERE word=?", (phrase,))
            self._term_cache = None
        return {"group_id": group["id"], "phrase": phrase}

    def delete_variant(self, variant_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM grudge_variants WHERE id=?", (int(variant_id),))
            self._term_cache = None

    def set_white(self, value, enabled=True):
        word = lexicon_word(value)
        if not re.fullmatch(r"\w{1,48}", word, re.UNICODE):
            raise ValueError("Белый список содержит отдельные слова длиной до 48 символов")
        with self.store.lock, self.store.db:
            if enabled:
                if any(any(self.matching_terms(platform, word)) for platform in ("telegram", "twitch")):
                    raise ValueError("Слово совпадает с чёрным списком")
                self.store.db.execute("INSERT OR REPLACE INTO lexicon_allow(word,at,source) VALUES (?,?,?)", (word, int(time.time()), "manual"))
            else:
                self.store.db.execute("DELETE FROM lexicon_allow WHERE word=?", (word,))
            self.store.db.execute("DELETE FROM lexicon_candidates WHERE word=?", (word,))
        return {"word": word, "enabled": bool(enabled)}

    def white_only(self, text):
        text = unicodedata.normalize("NFKC", str(text or "")).casefold()
        tokens = re.findall(r"\w+", text, re.UNICODE)
        if not tokens or len(tokens) > 80 or len(text) > 1500:
            return False
        # A string of separated letters or unrecognized symbols may hide a banword.
        if re.search(r"(?:\b\w\b[\s._-]+){2,}\b\w\b", text) or re.sub(r"\w+|[\s.,!?;:()\[\]{}\"'«»—–-]+", "", text):
            return False
        with self.store.lock:
            allowed = {row[0] for row in self.store.db.execute("SELECT word FROM lexicon_allow WHERE word IN (" + ",".join("?" for _ in set(tokens)) + ")", tuple(set(tokens)))}
        return all(token in allowed for token in tokens)

    def learn_safe_words(self, text, proposed):
        if not isinstance(proposed, list):
            return 0
        tokens = set(re.findall(r"\w+", lexicon_word(text), re.UNICODE))
        digest = hashlib.sha256(str(text).encode()).hexdigest()
        confirmations = max(1, min(3, int(self.store.setting("community_white_confirmations", "1"))))
        learned = 0
        with self.store.lock, self.store.db:
            for raw in proposed[:80]:
                word = lexicon_word(raw)
                if word not in tokens or not re.fullmatch(r"\w{1,48}", word, re.UNICODE) or any(any(self.matching_terms(p, word)) for p in ("telegram", "twitch")):
                    continue
                row = self.store.db.execute("SELECT hits,last_hash FROM lexicon_candidates WHERE word=?", (word,)).fetchone()
                if self.store.db.execute("SELECT 1 FROM lexicon_allow WHERE word=?", (word,)).fetchone() or row and row["last_hash"] == digest:
                    continue
                hits = 1 + (row["hits"] if row else 0)
                if hits >= confirmations:
                    self.store.db.execute("INSERT OR IGNORE INTO lexicon_allow(word,at,source) VALUES (?,?,?)", (word, int(time.time()), "model"))
                    self.store.db.execute("DELETE FROM lexicon_candidates WHERE word=?", (word,))
                    learned += 1
                else:
                    self.store.db.execute("INSERT INTO lexicon_candidates(word,hits,last_hash,at) VALUES (?,?,?,?) ON CONFLICT(word) DO UPDATE SET hits=excluded.hits,last_hash=excluded.last_hash,at=excluded.at", (word, hits, digest, int(time.time())))
        return learned

    def learn_offense_terms(self, terms, platform, points, source_text=""):
        """High confidence model output; existing group severity is never rewritten."""
        if not isinstance(terms, list):
            return 0
        source = lexicon_word(source_text)
        learned = 0
        with self.store.lock, self.store.db:
            for item in terms[:5]:
                if not isinstance(item, dict):
                    continue
                try:
                    head, variant = self._lexicon_phrase(item.get("head")), self._lexicon_phrase(item.get("variant"))
                except ValueError:
                    continue
                if not (re.search(r"\w", head) and re.search(r"\w", variant)) or variant not in source:
                    continue
                group = self.store.db.execute("SELECT id FROM grudge_terms WHERE phrase=?", (head,)).fetchone()
                if not group:
                    if (self.store.db.execute("SELECT 1 FROM grudge_variants WHERE phrase=?", (head,)).fetchone()
                            or self.store.db.execute("SELECT 1 FROM grudge_variants WHERE phrase=?", (variant,)).fetchone()
                            or self.store.db.execute("SELECT 1 FROM grudge_terms WHERE phrase=?", (variant,)).fetchone()):
                        continue
                    severity = item.get("points", points)
                    if type(severity) is not int or not 1 <= severity <= 100:
                        severity = points
                    self.store.db.execute("INSERT INTO grudge_terms(phrase,scope,points,response,enabled,origin) VALUES (?,?,?,?,1,'model')", (head, "both", severity, ""))
                    group = self.store.db.execute("SELECT id FROM grudge_terms WHERE phrase=?", (head,)).fetchone()
                    learned += 1
                if variant != head:
                    try:
                        self.add_variant(group["id"], variant, "model")
                        learned += 1
                    except ValueError:
                        pass
                self.store.db.execute("DELETE FROM lexicon_allow WHERE word IN (?,?)", (head, variant))
            if learned:
                self._term_cache = None
        return learned

    def protected_people(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute("SELECT * FROM community_protected ORDER BY category,nick")]

    @staticmethod
    def nickname_key(platform, nickname):
        nick = str(nickname or "").strip().removeprefix("@")
        pattern = r"[A-Za-z0-9_]{4,25}" if platform == "twitch" else r"[A-Za-z][A-Za-z0-9_]{4,31}"
        if platform not in ("telegram", "twitch") or not re.fullmatch(pattern, nick):
            raise ValueError("Для отложенной привязки укажите логин Twitch или @username Telegram, без пробелов и отображаемого имени")
        return nick.casefold()

    def pending_people(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute("SELECT * FROM community_pending ORDER BY at DESC,id DESC")]

    def delete_pending(self, row_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM community_pending WHERE id=?", (int(row_id),))

    def save_by_nickname(self, kind, data):
        """Save a stable username now; attach it to the platform ID once verified."""
        platform = str(data.get("platform") or "")
        raw = data.get("user_id") or data.get("nick")
        key = self.nickname_key(platform, raw)
        nick = str(raw).strip().removeprefix("@")
        category = str(data.get("category") or "")
        if kind not in ("honor", "protected", "person") or category not in ((*HONOR_TITLES, "custom") if kind == "honor" else ("king", "donor") if kind == "protected" else ("member",)):
            raise ValueError("Неверная категория почётной записи")
        title = str(data.get("title") or HONOR_TITLES.get(category, "")).strip()
        message = str(data.get("message") or "").strip()
        fixed_score = (_fixed_score(data.get("fixed_score")) if kind == "protected" else
                       _honor_fixed_score(data, category) if kind == "honor" else None)
        if (kind != "person" and not 1 <= len(title) <= 60) or len(title) > 60 or len(message) > 300 or any("\n" in s or "\r" in s for s in (title, message)):
            raise ValueError("Звание — до 60, личная фраза — до 300 символов и без переносов строк")
        with self.store.lock, self.store.db:
            known = self.store.db.execute("SELECT user_id FROM community_usernames WHERE platform=? AND username=?", (platform, key)).fetchone()
            if not known:
                names = {row[0] for row in self.store.db.execute("SELECT user_id,name FROM community_participants WHERE platform=?", (platform,))
                         if str(row[1]).casefold() == key or str(row[1]).casefold().endswith("(@" + key + ")")}
                if len(names) > 1:
                    raise ValueError("Это имя есть у нескольких участников. Выберите нужного человека в Книге")
                if names:
                    known = (next(iter(names)),)
            if known:
                if kind == "protected":
                    result = self.set_protected(platform, known[0], nick, category, title, message, fixed_score)
                elif kind == "honor":
                    result = {"id": self.save_honor({**data, "platform": platform, "user_id": known[0], "nick": nick, "title": title, "message": message, "fixed_score": fixed_score})}
                else:
                    result = self.profile(platform, known[0])
                if data.get("pending_id"):
                    self.store.db.execute("DELETE FROM community_pending WHERE id=? AND kind=?", (int(data["pending_id"]), kind))
                return result
            if category in ("king", "donor") and kind == "honor" and not self.store.db.execute(
                    "SELECT 1 FROM community_pending WHERE platform=? AND nick_key=? AND kind='protected' AND category=?", (platform, key, category)).fetchone():
                raise ValueError("Сначала сохраните этот ник в списке иммунитета")
            if data.get("pending_id"):
                old = self.store.db.execute("SELECT kind FROM community_pending WHERE id=?", (int(data["pending_id"]),)).fetchone()
                if not old or old[0] != kind:
                    raise ValueError("Отложенная запись не найдена")
            if kind == "protected":
                self.store.db.execute("DELETE FROM community_pending WHERE platform=? AND nick_key=? AND kind='protected' AND category<>?", (platform, key, category))
            self.store.db.execute("""INSERT INTO community_pending(platform,nick_key,nick,kind,category,title,message,at,fixed_score)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,nick_key,kind,category) DO UPDATE SET nick=excluded.nick,title=excluded.title,message=excluded.message,at=excluded.at,fixed_score=excluded.fixed_score""",
                (platform, key, nick, kind, category, title, message, int(time.time()), fixed_score))
            if data.get("pending_id"):
                self.store.db.execute("DELETE FROM community_pending WHERE id=? AND NOT (platform=? AND nick_key=? AND kind=? AND category=?)",
                                      (int(data["pending_id"]), platform, key, kind, category))
            row = self.store.db.execute("SELECT id FROM community_pending WHERE platform=? AND nick_key=? AND kind=? AND category=?", (platform, key, kind, category)).fetchone()
            return {"pending": True, "id": row[0], "nick": nick, "platform": platform}

    def bind_pending(self, platform, user_id, username):
        if not str(username or "").strip():
            return
        try:
            key = self.nickname_key(platform, username)
        except ValueError:
            return
        with self.store.lock, self.store.db:
            rows = [dict(row) for row in self.store.db.execute(
                "SELECT * FROM community_pending WHERE platform=? AND nick_key=? ORDER BY CASE kind WHEN 'protected' THEN 0 ELSE 1 END,id", (platform, key))]
            for row in rows:
                if row["kind"] == "protected":
                    self.set_protected(platform, user_id, row["nick"], row["category"], row["title"], row["message"], row["fixed_score"])
                elif row["kind"] == "honor":
                    self.save_honor({"platform": platform, "user_id": user_id, "nick": row["nick"], "category": row["category"],
                                     "title": row["title"], "message": row["message"], "fixed_enabled": row["fixed_score"] is not None,
                                     "fixed_score": row["fixed_score"]})
                self.store.db.execute("DELETE FROM community_pending WHERE id=?", (row["id"],))

    def set_protected(self, platform, user_id, nick, category, title=None, message=None, fixed_score=100):
        if platform not in ("telegram", "twitch") or not str(user_id).isdigit() or category not in ("king", "donor"):
            raise ValueError("Укажите площадку, числовой ID и категорию")
        if title is not None and (len(str(title)) > 60 or "\n" in str(title)) or message is not None and (len(str(message)) > 300 or "\n" in str(message)):
            raise ValueError("Звание — до 60, личная фраза — до 300 символов")
        fixed_score = _fixed_score(fixed_score)
        with self.store.lock, self.store.db:
            previous = self.store.db.execute("SELECT category FROM community_protected WHERE platform=? AND user_id=?", (platform, str(user_id))).fetchone()
            self.store.db.execute("INSERT INTO community_protected(platform,user_id,nick,category,at,fixed_score) VALUES (?,?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET nick=excluded.nick,category=excluded.category,fixed_score=excluded.fixed_score", (platform, str(user_id), str(nick or user_id)[:100], category, int(time.time()), fixed_score))
            if previous and previous["category"] != category:
                self.store.db.execute("DELETE FROM community_honors WHERE platform=? AND user_id=? AND category=?", (platform, str(user_id), previous["category"]))
            if title is not None or message is not None:
                honor = self.store.db.execute("SELECT id FROM community_honors WHERE platform=? AND user_id=? AND category=? ORDER BY id LIMIT 1", (platform, str(user_id), category)).fetchone()
                self.save_honor({"id": honor["id"] if honor else None, "platform": platform, "user_id": user_id,
                                 "nick": str(nick or user_id)[:100], "category": category, "title": title, "message": message})
        return self.profile(platform, user_id)

    def remove_protected(self, platform, user_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM community_protected WHERE platform=? AND user_id=?", (str(platform), str(user_id)))

    def honors(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute("SELECT * FROM community_honors ORDER BY category,nick,id")]

    def save_honor(self, data):
        platform, user_id = str(data.get("platform") or ""), str(data.get("user_id") or "").strip()
        category = str(data.get("category") or "").strip()
        nick = str(data.get("nick") or user_id).strip()
        title = str(data.get("title") or HONOR_TITLES.get(category, "")).strip()
        message = str(data.get("message") or "").strip()
        fixed_score = _honor_fixed_score(data, category)
        if (platform not in ("telegram", "twitch") or not user_id.isdigit() or category not in (*HONOR_TITLES, "custom")
                or not 1 <= len(nick) <= 100 or not 1 <= len(title) <= 60 or len(message) > 300
                or any("\n" in value or "\r" in value for value in (nick, title, message))):
            raise ValueError("Укажите площадку, числовой ID, звание до 60 и фразу до 300 символов")
        if category in ("king", "donor") and not any(item["category"] == category for item in self.profile(platform, user_id)["protected"]):
            raise ValueError("Сначала внесите короля или топ донатера в список фиксированного уровня")
        with self.store.lock, self.store.db:
            if data.get("id"):
                previous = self.store.db.execute("SELECT platform,user_id FROM community_honors WHERE id=?", (int(data["id"]),)).fetchone()
                if not previous:
                    raise ValueError("Почётная запись не найдена")
                if previous["platform"] == platform and previous["user_id"] == user_id:
                    self.store.db.execute("UPDATE community_honors SET nick=?,category=?,title=?,message=?,at=?,fixed_score=? WHERE id=?",
                                          (nick, category, title, message, int(time.time()), fixed_score, int(data["id"])))
                    return int(data["id"])
                # Editing a selected card's identity creates a new person;
                # it must never silently overwrite the original honor.
            return self.store.db.execute("INSERT INTO community_honors(platform,user_id,nick,category,title,message,at,fixed_score) VALUES (?,?,?,?,?,?,?,?)",
                                         (platform, user_id, nick, category, title, message, int(time.time()), fixed_score)).lastrowid

    def delete_honor(self, row_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM community_honors WHERE id=?", (int(row_id),))

    def honor_message(self):
        return self.store.setting("community_honor_message", DEFAULT_HONOR_MESSAGE)

    def rank_phrase(self, person):
        score = person["reputation"]
        if score > 50:
            level = "positive_high"
        elif score > 20:
            level = "positive_mid"
        elif score > 0:
            level = "positive_low"
        elif score >= 0:
            level = "neutral"
        elif score >= -20:
            level = "negative_low"
        elif score >= -50:
            level = "negative_mid"
        else:
            level = "negative_high"
        return self.store.setting("community_rank_" + level, RANK_PHRASES[level])

    def book_summary(self, short=False):
        people = self.recent_profiles(200)
        seen = {tuple(sorted(self._accounts(person["platform"], person["user_id"]))) for person in people}
        with self.store.lock:
            special_accounts = [(row["platform"], row["user_id"]) for row in self.store.db.execute(
                "SELECT platform,user_id FROM community_honors UNION SELECT platform,user_id FROM community_protected")]
            pending = [dict(row) for row in self.store.db.execute("SELECT * FROM community_pending ORDER BY id")]
        for platform, user_id in special_accounts:
            key = tuple(sorted(self._accounts(platform, user_id)))
            if key not in seen:
                people.append(self.profile(platform, user_id))
                seen.add(key)
        if hasattr(self,'participant_roles'):
            for person in people:
                person['display_titles']=self.participant_roles.profile(person['platform'],person['user_id'],base=person)['titles']
        pending_people = {}
        for entry in pending:
            key = (entry["platform"], entry["nick_key"])
            if entry["kind"] == "person":
                continue
            person = pending_people.setdefault(key, {"name": entry["nick"], "reputation": 0, "debt": 0, "honors": [], "_fixed_scores": []})
            if entry["fixed_score"] is not None:
                person["_fixed_scores"].append(entry["fixed_score"])
            person["honors"].append({"category": entry["category"], "title": entry["title"],
                                      "message": entry["message"] or ("Особая запись внесена в Книгу." if entry["fixed_score"] is not None and entry["fixed_score"] < 0 else self.honor_message())})
        for person in pending_people.values():
            if person["_fixed_scores"]:
                person["reputation"] = max(person["_fixed_scores"])
            people.append(person)
        count = 2 if short else 5
        worst = sorted((person for person in people if person["reputation"] < 0 and not person["honors"]), key=lambda person: (person["reputation"], -person["debt"]))[:count]
        special = sorted((person for person in people if person["honors"]), key=lambda person: (-person["reputation"], person["name"].casefold()))
        regular = sorted((person for person in people if person["reputation"] > 0 and not person["honors"]), key=lambda person: (-person["reputation"], person["name"].casefold()))[:count]
        featured = special[:2 if short else 20]
        def lines(rows, empty):
            result = []
            for index, person in enumerate(rows, 1):
                # Full username is less ambiguous than a display name containing
                # "(@username)"; never cut an @mention in half in Twitch output.
                stored_name = person["name"]
                old_username = re.search(r"\(@([A-Za-z0-9_]{4,32})\)$", stored_name)
                name = (old_username.group(1) if old_username else stored_name).replace("@", "")[:50 if short else 70]
                if person["honors"]:
                    honor = next((item for item in person["honors"] if item["category"] in ("king", "donor")), person["honors"][0])
                    titles=person.get('display_titles') or [honor['title']]
                    primary=honor['title'] if honor['title'] in titles else titles[0]
                    label = primary[:70 if short else 100] + ". " + honor["message"][:35 if short else 130]
                    extra=[title for title in titles if title!=primary]
                    if extra:label+=' · '+', '.join(extra)[:70 if short else 160]
                else:
                    label = self.rank_phrase(person)
                emblem = ('⚠️ ' if person['reputation'] < 0 else '✨ ') if person['honors'] else ''
                result.append(f"{index}. {emblem}{name} — {label}")
            return "\n".join(result) or empty
        if short:
            chunks = ["📖 Книга обид", "Особые звания:"]
            for line in lines(featured, "Пока нет").splitlines():
                if len(" | ".join(chunks + [line])) <= 450:
                    chunks.append(line)
            chunks.append("Неулаженные обиды:")
            for line in lines(worst, "Обид пока нет").splitlines():
                if len(" | ".join(chunks + [line])) <= 450:
                    chunks.append(line)
            chunks.append("Добрые страницы:")
            for line in lines(regular, "Пока пусто").splitlines():
                if len(" | ".join(chunks + [line])) <= 450:
                    chunks.append(line)
            return " | ".join(chunks).replace("@", "")
        chunks = ["📖 Книга обид", "Особые звания:"]
        for line in lines(featured, "Пока нет").splitlines():
            if len("\n".join(chunks + [line])) <= 3300:
                chunks.append(line)
        chunks.append("Неулаженные обиды:")
        for line in lines(worst, "Обид пока нет").splitlines():
            if len("\n".join(chunks + [line])) <= 3300:
                chunks.append(line)
        chunks.append("Добрые страницы:")
        for line in lines(regular, "Пока пусто").splitlines():
            if len("\n".join(chunks + [line])) <= 3300:
                chunks.append(line)
        return "\n".join(chunks).replace("@", "")  # Public rankings must not ping users.

    def book_context(self, short=False):
        if short:
            return ("📖 Как гномы FormalStyle хранят Даммаз Крон, помощник ведёт свою Книгу обид. "
                    "За запретные слова писарь заносит обиду; за дружбу и добрые дела растёт почёт. "
                    "Тяжёлый долг закрывает дверь к беседе, пока его не удастся погасить подходящей наградой канала или по решению владельца. "
                    "Владелец, модераторы, короли и топ донатеры неприкосновенны; другим можно дать почётное звание. Рейтинг: !assistant book; свой счёт: !assistant rep.")
        return ("📖 У гномов FormalStyle есть Даммаз Крон — Великая книга обид. "
                "помощник ведёт свою летопись чата. Нового путника она встречает без старых долгов: "
                "беседа и добрые дела прибавляют ему почёта, а запретные слова и обиды ложатся на страницы тяжёлой записью.\n\n"
                "При лёгкой обиде она не всегда отвечает на вопросы, но даёт понять, что помнит запись. Если строк станет больше, "
                "помощник отвечает ещё реже; при совсем тяжёлой обиде захлопнет Книгу и скажет, что сперва нужно уладить дело. "
                "Погасить обиду можно подходящей наградой канала или по решению владельца: "
                "тогда писарь отметит долг возмещённым, и разговор снова станет возможен.\n\n"
                "Имена королей сообщество и топ донатеров из особого реестра, а также владельца и модераторов с почётным званием "
                "имеют закреплённый владельцем уровень и защищены от новых записей обид. Другим людям можно дать любое особое звание и при желании закрепить для него свой уровень.\n\n"
                "Чтобы увидеть самых добрых и самых злостных, напиши /book в Telegram или !assistant book в Twitch. "
                "Свою репутацию узнай через /rep в Telegram или !assistant rep в Twitch.")

    def import_terms(self, data):
        """Add a list atomically. Each line is phrase or phrase | points | scope."""
        source = str(data.get("lines") or "")
        if len(source) > 120000:
            raise ValueError("Список слишком большой (не более 120 000 символов)")
        lines = [(number, line.strip()) for number, line in enumerate(source.splitlines(), 1) if line.strip()]
        if not lines or len(lines) > 2000:
            raise ValueError("Укажите от 1 до 2000 банвордов, по одному в строке")
        scope = str(data.get("scope", "both"))
        if scope not in ("both", "twitch", "telegram"):
            raise ValueError("Неверная площадка")
        try:
            points = int(data.get("points", 3))
        except (TypeError, ValueError):
            raise ValueError("Баллы должны быть числом от 1 до 100") from None
        if not 1 <= points <= 100:
            raise ValueError("Баллы должны быть от 1 до 100")
        response = str(data.get("response") or "").strip()
        if response:
            response = self.validate_templates(response)
        prepared = {}
        duplicate_lines = 0
        for number, line in lines:
            parts = [part.strip() for part in line.split("|")]
            if len(parts) not in (1, 2, 3):
                raise ValueError(f"Строка {number}: используйте слово | баллы | площадка")
            phrase = parts[0].casefold()
            if not 2 <= len(phrase) <= 80 or phrase.endswith("*") and len(phrase) < 4 or phrase.count("*") > 1 or ("*" in phrase and not phrase.endswith("*")):
                raise ValueError(f"Строка {number}: длина фразы должна быть от 2 до 80 символов")
            try:
                value = int(parts[1]) if len(parts) >= 2 else points
            except ValueError:
                raise ValueError(f"Строка {number}: баллы должны быть числом") from None
            if not 1 <= value <= 100:
                raise ValueError(f"Строка {number}: баллы должны быть от 1 до 100")
            item_scope = parts[2].casefold() if len(parts) == 3 else scope
            if item_scope not in ("both", "twitch", "telegram"):
                raise ValueError(f"Строка {number}: площадка должна быть both, twitch или telegram")
            if phrase in prepared:
                duplicate_lines += 1
                continue
            prepared[phrase] = (phrase, item_scope, value, response, int(bool(data.get("enabled", True))))
        with self.store.lock, self.store.db:
            before = self.store.db.total_changes
            self.store.db.executemany("INSERT OR IGNORE INTO grudge_terms(phrase,scope,points,response,enabled) VALUES (?,?,?,?,?)", prepared.values())
            added = self.store.db.total_changes - before
            if added:
                self._term_cache = None
        return {"added": added, "skipped": len(lines) - added, "duplicate_lines": duplicate_lines}

    def reward_rules(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute("SELECT * FROM twitch_reward_rules ORDER BY id")]

    def reward_title(self, reward_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT title FROM twitch_reward_rules WHERE reward_id=?", (reward_id,)).fetchone()
        return row[0] if row and row[0] else "Награда канала"

    def save_reward_rule(self, data):
        reward_id, action = str(data.get("reward_id", "")).strip(), str(data.get("action", ""))
        title = str(data.get("title") or "").strip()[:100]
        response = str(data.get("response", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", reward_id) or action not in ("forgive", "favor", "announce"):
            raise ValueError("Выберите ID награды и действие")
        if response:
            response = self.validate_templates(response)
        amount = _bounded(data.get("amount", 5), 1, 100)
        with self.store.lock, self.store.db:
            try:
                if data.get("id"):
                    cur = self.store.db.execute("UPDATE twitch_reward_rules SET reward_id=?,title=?,action=?,amount=?,response=? WHERE id=?", (reward_id, title, action, amount, response, int(data["id"])))
                    if not cur.rowcount:
                        raise ValueError("Правило награды не найдено")
                    return int(data["id"])
                return self.store.db.execute("INSERT INTO twitch_reward_rules(reward_id,title,action,amount,response) VALUES (?,?,?,?,?)", (reward_id, title, action, amount, response)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("Эта награда уже настроена") from None

    def delete_reward_rule(self, row_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM twitch_reward_rules WHERE id=?", (int(row_id),))

    def _accounts(self, platform, user_id):
        with self.store.lock:
            if platform == "twitch":
                row = self.store.db.execute("SELECT telegram_id FROM community_links WHERE twitch_id=?", (str(user_id),)).fetchone()
                return [("twitch", str(user_id))] + ([("telegram", row[0])] if row else [])
            row = self.store.db.execute("SELECT twitch_id FROM community_links WHERE telegram_id=?", (str(user_id),)).fetchone()
            return [("telegram", str(user_id))] + ([("twitch", row[0])] if row else [])

    def see_user(self, platform, user_id, name, username="", chat_id=None):
        if platform not in ("telegram", "twitch") or not str(user_id):
            return
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO community_participants(platform,user_id,name,at) VALUES (?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET name=excluded.name,at=excluded.at", (platform, str(user_id), str(name or username or user_id)[:100], int(time.time())))
            if platform == "telegram" and chat_id is not None:
                self.store.db.execute("INSERT OR IGNORE INTO community_participant_chats(user_id,chat_id) VALUES (?,?)", (str(user_id), str(chat_id)))
        self.observe_username(platform, user_id, username)
        self.bind_pending(platform, user_id, username)

    def group_coverage(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute("SELECT c.id,c.title,c.type,c.username,c.member_count,COUNT(pc.user_id) AS known FROM chats c LEFT JOIN community_participant_chats pc ON pc.chat_id=c.id WHERE c.type IN ('group','supergroup') GROUP BY c.id ORDER BY c.seen_at DESC LIMIT 30")]

    def leave_group(self, user_id, chat_id):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM community_participant_chats WHERE user_id=? AND chat_id=?", (str(user_id), str(chat_id)))

    def mtproto_status(self):
        return {"api_id": self.store.setting("tg_mtproto_api_id"), "configured": bool(self.store.setting("tg_mtproto_api_id") and self.store.setting("tg_mtproto_api_hash")), "available": importlib.util.find_spec("telethon") is not None}

    def configure_mtproto(self, data):
        api_id = str(data.get("api_id") or "").strip()
        api_hash = str(data.get("api_hash") or "").strip().casefold()
        if not api_id.isdigit() or not 1 <= int(api_id) <= 2147483647:
            raise ValueError("Укажите API ID Telegram числом")
        if api_hash and not re.fullmatch(r"[0-9a-f]{32}", api_hash):
            raise ValueError("API Hash должен содержать 32 шестнадцатеричных символа")
        if not api_hash and not self.store.setting("tg_mtproto_api_hash"):
            raise ValueError("Укажите API Hash Telegram")
        changed = api_id != self.store.setting("tg_mtproto_api_id") or bool(api_hash and api_hash != self.store.setting("tg_mtproto_api_hash"))
        values = {"tg_mtproto_api_id": api_id}
        if api_hash:
            values["tg_mtproto_api_hash"] = api_hash
        self.store.set_settings(values)
        if changed:
            self.store.set_state("tg_mtproto_session", "")
        return self.mtproto_status()

    def disconnect_mtproto(self):
        self.store.set_settings({"tg_mtproto_api_id": "", "tg_mtproto_api_hash": ""})
        self.store.set_state("tg_mtproto_session", "")

    def scan_groups(self, token="", group_id="", expected_bot_id=None):
        """Index past authors and current roster where optional MTProto is configured."""
        group_id = str(group_id or "")
        with self.store.lock, self.store.db:
            groups = [dict(row) for row in self.store.db.execute("SELECT id,title,type,username FROM chats WHERE type IN ('group','supergroup') ORDER BY seen_at DESC LIMIT 30")]
            if group_id:
                groups = [group for group in groups if group["id"] == group_id]
                if not groups:
                    raise ValueError("Группа не найдена среди известных боту")
            rows = [row for row in self.store.db.execute("SELECT m.chat,m.person,m.name,MAX(m.at) AS at FROM chat_memory m JOIN chats c ON c.id=m.chat AND c.type IN ('group','supergroup') WHERE m.person GLOB '[0-9]*' AND m.person NOT GLOB '*[^0-9]*' GROUP BY m.chat,m.person") if not group_id or row["chat"] == group_id]
            twitch_rows = [dict(row) for row in self.store.db.execute("SELECT user_id,login,display_name,at FROM (SELECT user_id,login,display_name,at,ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY at DESC,id DESC) AS rn FROM twitch_messages) WHERE rn=1 ORDER BY at DESC LIMIT 5000")]
            for row in rows:
                self.store.db.execute("INSERT INTO community_participants(platform,user_id,name,at) VALUES ('telegram',?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET name=CASE WHEN excluded.at>=at THEN excluded.name ELSE name END,at=MAX(at,excluded.at)", (row["person"], row["name"], row["at"]))
                self.store.db.execute("INSERT OR IGNORE INTO community_participant_chats(user_id,chat_id) VALUES (?,?)", (row["person"], row["chat"]))
        for row in twitch_rows:
            self.see_user("twitch", row["user_id"], row["display_name"] or row["login"], row["login"] if row["at"] >= int(time.time()) - 30 * 86400 else "")
        for row in rows:
            old_username = re.search(r"\(@([A-Za-z0-9_]{5,32})\)$", row["name"])
            if old_username and row["at"] >= int(time.time()) - 30 * 86400:
                self.observe_username("telegram", row["person"], old_username.group(1))
        admins = 0
        failures = 0
        counts = {}
        if token:
            from .core import tg_call
            def fetch(group):
                members, count, username, error = [], None, group["username"], False
                try:
                    members = tg_call(token, "getChatAdministrators", {"chat_id": group["id"]}, timeout=5)
                except (RuntimeError, OSError, TimeoutError, ValueError):
                    error = True
                try:
                    count = tg_call(token, "getChatMemberCount", {"chat_id": group["id"]}, timeout=5)
                except (RuntimeError, OSError, TimeoutError, ValueError):
                    error = True
                if not username:
                    try:
                        details = tg_call(token, "getChat", {"chat_id": group["id"]}, timeout=5)
                        username = str(details.get("username") or "") if isinstance(details, dict) else ""
                    except (RuntimeError, OSError, TimeoutError, ValueError):
                        error = True
                return group, members, count, username, error
            with ThreadPoolExecutor(max_workers=5) as pool:
                tasks = [pool.submit(fetch, group) for group in groups]
                for task in as_completed(tasks):
                    try:
                        group, members, count, username, error = task.result()
                        if error:
                            failures += 1
                        if count is not None:
                            counts[group["id"]] = int(count)
                            with self.store.lock, self.store.db:
                                self.store.db.execute("UPDATE chats SET member_count=? WHERE id=?", (int(count), group["id"]))
                        if username:
                            group["username"] = username
                            with self.store.lock, self.store.db:
                                self.store.db.execute("UPDATE chats SET username=? WHERE id=?", (username, group["id"]))
                        for member in members:
                            person = member.get("user") or {}
                            if person.get("id") and not person.get("is_bot"):
                                self.see_user("telegram", person["id"], " ".join(filter(None, (person.get("first_name", ""), person.get("last_name", "")))) or person.get("username"), person.get("username"), group["id"])
                                admins += 1
                    except (RuntimeError, OSError, TimeoutError, ValueError):
                        failures += 1
        full = {"status": "Не настроен", "added": 0, "errors": []}
        api_id, api_hash = self.store.setting("tg_mtproto_api_id"), self.store.setting("tg_mtproto_api_hash")
        if token and api_id and api_hash and groups:
            from .telegram_members import scan_members
            try:
                result = scan_members(api_id, api_hash, token, groups, self.store.state("tg_mtproto_session"), expected_bot_id)
                self.store.set_state("tg_mtproto_session", result["session"])
                for person in result["people"]:
                    self.see_user("telegram", person["id"], person["name"], person["username"], person["chat_id"])
                full = {"status": "Сканирование MTProto выполнено", "added": len(result["people"]), "errors": result["errors"]}
            except Exception as exc:
                detail = str(exc).replace(token, "[скрыто]").replace(api_hash, "[скрыто]")
                full = {"status": detail[:220], "added": 0, "errors": []}
        return {"groups": len(groups), "saved_authors": len(rows), "saved_twitch": len(twitch_rows), "administrators": admins, "failed_groups": failures, "counts": counts, "full": full, "coverage": self.group_coverage()}

    def profile(self, platform, user_id):
        if platform not in ("twitch", "telegram") or not str(user_id):
            raise ValueError("Укажите платформу и ID пользователя")
        accounts = self._accounts(platform, user_id)
        with self.store.lock:
            totals = [self.store.db.execute("SELECT COALESCE(SUM(grudge_delta),0) AS debt,COALESCE(SUM(favor_delta),0) AS favor,COALESCE(SUM(activity_delta),0) AS activity FROM grudge_ledger WHERE platform=? AND user_id=?",(p,uid)).fetchone() for p,uid in accounts]
            records = [row for p, uid in accounts for row in self.store.db.execute("SELECT * FROM grudge_ledger WHERE platform=? AND user_id=? ORDER BY at DESC,id DESC LIMIT 30", (p, uid))]
            named = [row for p,uid in accounts for row in self.store.db.execute("SELECT nick,at,id FROM grudge_ledger WHERE platform=? AND user_id=? AND nick<>'' ORDER BY at DESC,id DESC LIMIT 1",(p,uid))]
            link = self.store.db.execute("SELECT method,twitch_id,telegram_id FROM community_links WHERE " + ("twitch_id" if platform == "twitch" else "telegram_id") + "=?", (str(user_id),)).fetchone() if len(accounts) == 2 else None
            participant = self.store.db.execute("SELECT name FROM community_participants WHERE platform=? AND user_id=?", (platform, str(user_id))).fetchone()
            identities = {}
            for p, uid in accounts:
                item = self.store.db.execute("SELECT p.name,u.username FROM community_participants p LEFT JOIN community_usernames u ON u.platform=p.platform AND u.user_id=p.user_id WHERE p.platform=? AND p.user_id=?", (p, uid)).fetchone()
                identities[p] = {"id": uid, "name": item[0] if item else "", "username": item[1] if item and item[1] else ""}
            groups = [dict(row) for row in self.store.db.execute("SELECT c.id,c.title FROM community_participant_chats pc JOIN chats c ON c.id=pc.chat_id WHERE pc.user_id=? ORDER BY c.seen_at DESC LIMIT 20", (link["telegram_id"] if link else str(user_id) if platform == "telegram" else "",))]
            protected = [dict(row) for p, uid in accounts for row in self.store.db.execute("SELECT platform,user_id,nick,category,fixed_score FROM community_protected WHERE platform=? AND user_id=?", (p, uid))]
            honors = [dict(row) for p, uid in accounts for row in self.store.db.execute("SELECT * FROM community_honors WHERE platform=? AND user_id=? ORDER BY id", (p, uid))]
            telegram_id = next((uid for p, uid in accounts if p == "telegram"), None)
            warnings = [dict(row) for row in self.store.db.execute(
                "SELECT chat_id,COUNT(*) AS count,MAX(at) AS last_at FROM moderation_warnings WHERE user_id=? AND at>=? GROUP BY chat_id ORDER BY last_at DESC",
                (telegram_id, int(time.time()) - int(self.store.setting("moderation_warning_days", "7")) * 86400))] if telegram_id else []
        protected.extend({"platform": honor["platform"], "user_id": honor["user_id"], "nick": honor["nick"],
                          "category": honor["category"], "fixed_score": honor["fixed_score"]}
                         for honor in honors if honor["fixed_score"] is not None and honor["category"] not in ("king", "donor"))
        for item in protected:
            if not any(honor["category"] == item["category"] for honor in honors):
                honors.append({"id": 0, **item, "title": HONOR_TITLES[item["category"]], "message": ""})
        from .participant_roles import unique_honors
        honors = unique_honors(honors)
        for honor in honors:
            honor["message"] = honor["message"] or ("Особая запись внесена в Книгу." if honor["fixed_score"] is not None and honor["fixed_score"] < 0 else self.honor_message())
            for source in honor.get('sources',[]):
                if source.get('nick') and not identities[source['platform']]['name']:identities[source['platform']]['name']=source['nick']
            if not identities[honor["platform"]]["name"]:
                identities[honor["platform"]]["name"] = honor["nick"]
        debt = max(0, sum(row["debt"] for row in totals))
        favor = sum(row["favor"] for row in totals)
        activity = round(sum(row["activity"] for row in totals), 1)
        score = round(max(-100, min(100, activity + favor - debt)), 1)
        if protected:
            score = max(item["fixed_score"] for item in protected)
        name = (participant[0] if participant else "") or next((row["nick"] for row in sorted(named, key=lambda r: (r["at"], r["id"]), reverse=True)), "") or (honors[0]["nick"] if honors else str(user_id))
        return {"platform": platform, "user_id": str(user_id), "name": name, "reputation": score, "fixed_score": score if protected else None, "activity": activity, "favor": favor, "debt": debt,
                "linked": len(accounts) == 2, "protected": protected, "honors": honors, "warnings": warnings, "link_method": link["method"] if link else "", "twitch_id": link["twitch_id"] if link else "", "telegram_id": link["telegram_id"] if link else "", "identities": identities, "groups": groups, "events": [dict(row) for row in sorted(records, key=lambda r: (r["at"], r["id"]), reverse=True)[:30]]}

    def resolve_person(self, platform, query, twitch=None):
        if platform not in ("twitch", "telegram"):
            raise ValueError("Выберите площадку")
        value = str(query or "").strip().removeprefix("@").casefold()
        if not value:
            raise ValueError("Укажите ник или ID участника")
        if value.isdigit():
            return self.profile(platform, value)
        with self.store.lock:
            candidates = set()
            for table, field in (("community_usernames", "username"), ("community_participants", "name"), ("community_protected", "nick"), ("community_honors", "nick")):
                candidates.update(str(row[0]) for row in self.store.db.execute(
                    f"SELECT user_id,{field} FROM {table} WHERE platform=?", (platform,)) if str(row[1]).casefold() == value)
            if platform == "twitch":
                candidates.update(str(row[0]) for row in self.store.db.execute(
                    "SELECT DISTINCT user_id FROM twitch_messages WHERE lower(login)=?", (value,)))
        if len(candidates) > 1:
            raise ValueError("Такой ник есть у нескольких людей. Выберите карточку из Книги или укажите ID")
        if candidates:
            return self.profile(platform, next(iter(candidates)))
        if platform == "twitch" and twitch is not None:
            user = twitch.resolve_login(value)
            self.see_user("twitch", user["id"], user["name"], user["username"])
            return self.profile(platform, user["id"])
        raise ValueError("Telegram-пользователь пока не известен боту. Дождитесь его сообщения или выберите из Книги")

    def recent_profiles(self, limit=100):
        with self.store.lock:
            pairs = self.store.db.execute("""SELECT platform,user_id FROM (
                SELECT platform,user_id,at FROM grudge_ledger
                UNION ALL SELECT platform,user_id,at FROM community_activity
                UNION ALL SELECT 'twitch',user_id,at FROM twitch_messages
                UNION ALL SELECT platform,user_id,at FROM community_participants
                UNION ALL SELECT platform,user_id,at FROM community_protected
                UNION ALL SELECT platform,user_id,at FROM community_honors
                UNION ALL SELECT 'telegram',person,at FROM chat_memory WHERE person GLOB '[0-9]*' AND person NOT GLOB '*[^0-9]*'
            ) GROUP BY platform,user_id ORDER BY MAX(at) DESC LIMIT ?""", (min(200, int(limit)),)).fetchall()
        seen = set()
        result = []
        for platform, user_id in pairs:
            key = tuple(sorted(self._accounts(platform, user_id)))
            if key in seen:
                continue
            seen.add(key)
            result.append(self.profile(platform, user_id))
        return result

    def adjust(self, platform, user_id, nick, action, amount, reason, source_key=None):
        if platform not in ("telegram", "twitch") or not str(user_id).strip() or action not in ("forgive", "favor", "grudge", "set"):
            raise ValueError("Укажите платформу, ID и действие")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise ValueError("Укажите числовое значение") from None
        if not math.isfinite(amount) or not math.isfinite(amount * 2) or amount * 2 != int(amount * 2) or (action == "set" and not -100 <= amount <= 100) or (action != "set" and amount <= 0):
            raise ValueError("Укажите баллы с шагом 0,5; точный уровень — от −100 до +100")
        reason = str(reason or "Ручная правка")[:200]
        current = self.profile(platform, user_id)
        if current["protected"] and action in ("grudge", "set"):
            raise ValueError("Звание удерживает фиксированный уровень. Сначала измените или снимите фиксацию в панели.")
        raw = current["activity"] + current["favor"] - current["debt"]
        if action == "forgive":
            amount = min(amount, current["debt"])
            if not amount:
                return current
        if action == "set":
            # Keep the displayed debt consistent with an exact manual score.
            target = amount
            desired_debt = max(0, -target)
            debt_delta = desired_debt - current["debt"]
            favor_delta = target + desired_debt - current["activity"] - current["favor"]
        else:
            debt_delta = amount if action == "grudge" else -amount if action == "forgive" else 0
            target = max(-100, min(100, current["reputation"] + (amount if action != "grudge" else -amount)))
            favor_delta = target - (raw - debt_delta)
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,grudge_delta,favor_delta,reason,source_key,at) VALUES (?,?,?,?,?,?,?,?)",
                                  (platform, str(user_id), str(nick)[:100], debt_delta, favor_delta, reason, source_key or "manual:" + secrets.token_hex(12), int(time.time())))
        return self.profile(platform, user_id)

    def observe(self, platform, user_id, nick, message_key, text, at=None, notify_allowed=True, severe_notify_allowed=False, media_text="", assessment=None, transition_notify_allowed=None):
        """Count activity and offenses once per message; return a reply only when due."""
        if platform not in ("twitch", "telegram") or not user_id or not message_key:
            return None
        at = int(at or time.time())
        text = str(text or "")[:2000]
        media_text = str(media_text or "")[:500]
        enabled = self.store.setting("community_enabled", "0") == "1"
        accounts = self._accounts(platform, user_id)
        day = datetime.fromtimestamp(at, _GREETING_ZONE).date().isoformat()
        activity_day_key = "community_active_day:" + ":".join(p + "=" + uid for p, uid in sorted(accounts))
        prior_activity_day = self.store.state(activity_day_key, "")
        if enabled:
            self.store.set_state(activity_day_key, day)
        matched = []
        local_points = 0
        increased = False
        before_activity = 0.0
        with self.store.lock, self.store.db:
            before_score = self.profile(platform, user_id)['reputation'] if enabled else 0
            if enabled:
                cur = self.store.db.execute("INSERT OR IGNORE INTO community_activity(platform,message_key,user_id,at) VALUES (?,?,?,?)", (platform, str(message_key), str(user_id), at))
                current = self.profile(platform, user_id)
                before_activity = current["activity"]
                blocked = self.store.setting("community_activity_stop", "0") == "1" and current["debt"] >= int(self.store.setting("community_activity_stop_debt", "50"))
                cap = float(self.store.setting("community_activity_cap", "20"))
                # Fixed titles keep the visible score unchanged. Track activity up to
                # the cap so it still counts if the title is later removed.
                can_earn = current["activity"] < cap if current["protected"] else current["reputation"] < cap
                level = self.attitude(platform, user_id)
                base_rate = self.store.setting("community_activity_rate_" + level, {"normal": "0.5", "hint": "0.3", "strong": "0.2", "refuse": "0.1"}[level])
                rate = float(self.store.setting("community_activity_rate_twitch_" + level, base_rate) if platform == "twitch" else base_rate)
                room = cap - (current["activity"] if current["protected"] else current["reputation"])
                amount = round(min(rate, room), 1)
                if cur.rowcount and not blocked and can_earn and amount > 0:
                    awarded = self.store.db.execute("INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,activity_delta,reason,source_key,at) VALUES (?,?,?,?,?,?,?)", (platform, str(user_id), str(nick)[:100], amount, "Активность в чате", f"activity:{platform}:{message_key}", at))
                    increased = bool(awarded.rowcount)
            for term, caption_match in self.matching_terms(platform, text, media_text) if enabled else []:
                key = f"word:{platform}:{message_key}:{term['id']}"
                if self.profile(platform, user_id)["protected"]:
                    continue
                amount = term["points"]
                if amount == 100:
                    current = self.profile(platform, user_id)
                    amount = max(0, current["activity"] + current["favor"] - current["debt"] + 100)
                reason = ("Запретное слово" if caption_match else "Запретное слово на вложении") + " (правило №" + str(term["id"]) + ")"
                cur = self.store.db.execute("INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,grudge_delta,reason,source_key,at) VALUES (?,?,?,?,?,?,?)", (platform, str(user_id), str(nick)[:100], amount, reason, key, at))
                if cur.rowcount:
                    matched.append(dict(term))
                    local_points += term["points"]
            if enabled and assessment and not self.profile(platform, user_id)["protected"]:
                points = assessment["points"]
                # The model and a local rule must not charge twice for the same insult.
                extra = max(0, points - local_points)
                if extra:
                    if points == 100:
                        current = self.profile(platform, user_id)
                        extra = max(0, current["activity"] + current["favor"] - current["debt"] + 100)
                    kind = "Тяжёлое нарушение" if assessment["kind"] == "severe" else "Оскорбление"
                    cur = self.store.db.execute("INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,grudge_delta,reason,source_key,at) VALUES (?,?,?,?,?,?,?)",
                                                (platform, str(user_id), str(nick)[:100], extra, kind + " (оценка модели)", f"semantic:{platform}:{message_key}", at))
                    if cur.rowcount:
                        matched.append({"points": points, "response": ""})
            if enabled and self.store.setting("quest_roleplay_enabled", "0") != "1" and not self.profile(platform, user_id)["protected"]:
                roleplay = re.search(
                    r"(?:сгинь|исчезни|проваливай|заткнись).{0,35}(?:техноерес|ошибка|помощник|дух)|"
                    r"(?:забрал\w*|забир\w*|украл\w*|отобрал\w*|лишил\w*|отним\w*).{0,35}ресурс|"
                    r"ресурс.{0,35}(?:забрал\w*|забир\w*|украл\w*|отобрал\w*|лишил\w*|отним\w*)",
                    text, re.I | re.S)
                if roleplay:
                    key = f"roleplay:{platform}:{message_key}"
                    recent = self.store.db.execute(
                        "SELECT COALESCE(SUM(grudge_delta),0) FROM grudge_ledger WHERE platform=? AND user_id=? AND source_key LIKE 'roleplay:%' AND at>=?",
                        (platform, str(user_id), at - 86400)).fetchone()[0]
                    amount = min(4 if re.search(r"ресурс|сгинь|исчезни|проваливай", roleplay.group(0), re.I) else 2,
                                max(0, 20 - int(recent or 0)))
                    if amount:
                        cur = self.store.db.execute(
                            "INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,grudge_delta,reason,source_key,at) VALUES (?,?,?,?,?,?,?)",
                            (platform, str(user_id), str(nick)[:100], amount,
                             "Игровая обида: " + roleplay.group(0)[:120], key, at))
                        if cur.rowcount:
                            matched.append({"points": amount, "response": ""})
                            local_points += amount
            if enabled and not self.profile(platform, user_id)["protected"]:
                positive = re.search(
                    r"(?:помощник|помощник|ресурс).{0,35}(?:молодец|лучш|уважаю|спасибо|вернул|вернула|топ)|"
                    r"(?:молодец|уважаю|спасибо).{0,35}(?:помощник|помощник)", text, re.I | re.S)
                if positive and not re.search(r"\bне\s+(?:молодец|лучш\w*|уважаю)", positive.group(0), re.I):
                    key = f"roleplay_good:{platform}:{message_key}"
                    recent_good = self.store.db.execute(
                        "SELECT COUNT(*) FROM grudge_ledger WHERE platform=? AND user_id=? AND source_key LIKE 'roleplay_good:%' AND at>=?",
                        (platform, str(user_id), at - 86400)).fetchone()[0]
                    if int(recent_good or 0) < 3:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO grudge_ledger(platform,user_id,nick,favor_delta,reason,source_key,at) VALUES (?,?,?,?,?,?,?)",
                            (platform, str(user_id), str(nick)[:100], 1,
                             "Игровая благодарность: " + positive.group(0)[:120], key, at))
            if matched and self.quiet_penalty(max(local_points, (assessment or {}).get('points', 0))):
                self.record_transition(platform, user_id, nick, message_key, before_score, self.profile(platform, user_id)['reputation'], at)
        if not enabled:
            return None
        if (transition_notify_allowed if transition_notify_allowed is not None else notify_allowed):
            # Telegram message keys already contain the chat ID.
            notice = self.transition_notice(platform, user_id, '', message_key)
            if notice:
                return notice
        score = self.profile(platform, user_id)
        if increased and self.score_attitude(before_score) != self.score_attitude(score["reputation"]):
            order = {"normal": 0, "hint": 1, "strong": 2, "refuse": 3}
            if order[self.score_attitude(score["reputation"])] < order[self.score_attitude(before_score)]:
                return self.choose_message("improvement", platform, user_id, values={"user": str(nick)[:50], "score": score["reputation"]})
        severe = next((term for term in matched if term["points"] == 100), None)
        if severe and (notify_allowed or severe_notify_allowed) and self.notification_enabled("severe", platform):
            mentions = self.store.setting("community_severe_mentions_" + platform, "")
            offender = self.offender_mention(platform, user_id, nick)
            notice = self.choose_message("severe", platform, user_id, severe["response"], {"user": offender, "mentions": mentions})
            if offender not in notice:
                notice = offender + ", " + notice
            if mentions and mentions not in notice:
                notice += " " + mentions
            return notice[:500]
        if self.attitude(platform, user_id) in ("strong", "refuse"):
            return None
        points = max(local_points, (assessment or {}).get("points", 0))
        if matched and not self.quiet_penalty(points) and notify_allowed and self.notification_enabled("banword", platform):
            key = "community_last_reply:" + platform + ":" + str(user_id)
            if at - int(self.store.state(key, "0")) >= 120:
                self.store.set_state(key, at)
                return self.choose_message("banword", platform, user_id, matched[0]["response"], {"user": str(nick)[:50], "debt": score["debt"], "score": score["reputation"]})
        step = float(self.store.setting("community_activity_notify_every", "5"))
        if (increased and not matched and notify_allowed and self.notification_enabled("activity", platform)
                and int(round(score["activity"] * 10)) // int(round(step * 10)) > int(round(before_activity * 10)) // int(round(step * 10))):
            return StandaloneReply(self.choose_message("activity", platform, user_id, values={"user": str(nick)[:50], "score": score["reputation"], "activity": score["activity"]}))
        greeting_min = _bounded(self.store.setting("community_greeting_min", "40"), 1, 100)
        if not matched and notify_allowed and self.notification_enabled("greeting", platform) and score["reputation"] >= greeting_min:
            key = "community_last_greeting:" + ":".join(p + "=" + uid for p, uid in sorted(accounts))
            last = max([int(self.store.state(key, "0"))] + [int(self.store.state("community_last_greeting:account:" + p + ":" + uid, "0")) for p, uid in accounts] + [int(self.store.state("community_last_greeting:" + p + ":" + uid, "0")) for p, uid in accounts] + [int(self.store.state("community_last_greeting:" + p + "=" + uid, "0")) for p, uid in accounts])
            new_day = not last or datetime.fromtimestamp(at, _GREETING_ZONE).date() != datetime.fromtimestamp(last, _GREETING_ZONE).date()
            if new_day and not prior_activity_day == day and at - last >= 4 * 3600:
                self.store.set_state(key, at)
                for p, uid in accounts:
                    self.store.set_state("community_last_greeting:account:" + p + ":" + uid, at)
                return self.choose_message("greeting", platform, user_id, values={"user": str(nick)[:50], "score": score["reputation"]})
        return None

    def daily_forgiveness(self, platform, user_id, nick, at=None):
        """Give one bounded daily step toward reconciliation for an active user."""
        if not user_id or self.store.setting("community_enabled", "0") != "1":
            return None
        at = int(at or time.time())
        profile = self.profile(platform, user_id)
        if profile["protected"] or profile["debt"] <= 0 or self.attitude(platform, user_id) not in ("strong", "refuse"):
            return None
        day = datetime.fromtimestamp(at, _GREETING_ZONE).date().isoformat()
        source = f"forgive_activity:{platform}:{user_id}:{day}"
        with self.store.lock:
            exists = self.store.db.execute("SELECT 1 FROM grudge_ledger WHERE source_key=?", (source,)).fetchone()
        if exists:
            return None
        amount = min(10, float(profile["debt"]))
        if amount <= 0:
            return None
        updated = self.adjust(platform, user_id, nick, "forgive", amount, "Дневной шаг к примирению за активное общение", source_key=source)
        return {"amount": amount, "reputation": updated["reputation"], "message": f"Я вижу, что ты идёшь по пути исправления. За сегодняшнее спокойное общение обида уменьшилась на {amount:g} баллов."}

    def attitude(self, platform, user_id):
        return self.score_attitude(self.profile(platform, user_id)['reputation'])

    def score_attitude(self, score):
        strong = int(self.store.setting("community_grudge_strong_at", "-21"))
        refuse = int(self.store.setting("community_grudge_refuse_at", "-50"))
        if score <= refuse:
            return "refuse"
        if score <= strong:
            return "strong"
        if score < 0:
            return "hint"
        return "normal"

    def reply_policy(self, platform, user_id):
        level = self.attitude(platform, user_id)
        return level, self.choose_message(level, platform, user_id) if level != "normal" else ""

    def own_reputation(self, platform, user_id, chat=''):
        profile = self.profile(platform, user_id)
        score = profile["reputation"]
        level = self.attitude(platform, user_id)
        description = {"normal": "всё спокойно" if score < 40 else "добрая запись",
                       "hint": "отмечена лёгкая обида", "strong": "отмечена сильная обида",
                       "refuse": "скопилось слишком много обид"}[level]
        if profile["protected"]:
            description = "особое звание и неизменный уровень репутации"
        response = f"помощник открыла Книгу: {self.rank_phrase(profile)}. На странице {description}."
        if hasattr(self,'participant_roles'):
            merged=self.participant_roles.profile(platform,user_id,str(chat),base=profile)
            if merged['titles']:
                response += ('\n⚠️ ' if score<0 else '\n✨ ')+'Особые звания и роли: '+', '.join(merged['titles'])+'. '
                response += ' '.join(dict.fromkeys(item['message'] for item in merged['honors']))
        elif profile["honors"]:
            titles = ", ".join(dict.fromkeys(item["title"] for item in profile["honors"]))
            messages = " ".join(dict.fromkeys(item["message"] for item in profile["honors"]))
            response += f"\n{'⚠️' if score < 0 else '✨'} Особые звания: {titles}. {messages}"
        return response

    def answer_allowed(self, level):
        """Draw once per addressed question, before calling a model or trigger."""
        if level not in ("hint", "strong"):
            return True
        default = "50" if level == "hint" else "10"
        chance = max(0, min(100, int(self.store.setting("community_" + level + "_answer_chance", default))))
        return secrets.randbelow(100) < chance

    @staticmethod
    def combine_grudge_reply(remark, answer, platform):
        if not remark:
            return None
        if not answer:
            return remark
        # Twitch chat messages are sent on one line; Telegram preserves the gap.
        return GrudgeReply(remark, answer, platform)

    @staticmethod
    def reconciliation_question(question, context=False):
        text = str(question or "").casefold().replace("ё", "е")
        topic = r"(?:обид|долг|репутац|рейтинг|книг|отношени|довер|прощени|запрет\s+на\s+общен)"
        action = r"(?:погас|снят|сним|искуп|убрат|убер|улад|заглад|исправ|вернут|верни|поднят|подним|повыс|улучш|заслуж)"
        if re.search(topic + r".{0,100}" + action + "|" + action + r".{0,100}" + topic, text):
            return True
        if re.search(r"(?:как|хочу|можно|давай|помоги|что.{0,30}(?:нужно|надо)).{0,60}(?:помир|примир)|(?:помир|примир).{0,40}(?:как|что.{0,20}дела)", text):
            return True
        if re.search(r"что.{0,35}(?:делать|сделать).{0,65}(?:обид|долг|помир|примир|чтобы.{0,35}(?:общал|говор|отвеч|прост))", text):
            return True
        if re.search(r"(?:донат|donat|donation|балл.{0,12}канала|награду канала)", text):
            return True
        # Short follow-ups refer to this person's debt, not an unrelated loan.
        if re.search(r"\b(?:как|чем)\s+(?:мне\s+)?(?:ее\s+|его\s+|это\s+)?(?:погасить|искупить|загладить|уладить)(?:\s+(?:ее|его|это)\b|[?!.;,]|\s*$)", text):
            return True
        if context and re.search(r"\b(?:как|чем|можно(?:\s+ли)?)\s+(?:мне\s+)?(?:(?:ее|его|это|все)\s+)?(?:снять|убрать|исправить|уладить)(?:\s+(?:ее|его|это|все))?\s*(?=[?!.;,]|$)|\b(?:как|что.{0,20}(?:чтобы|для)).{0,25}исправиться\b|\bчто\s+(?:мне\s+)?(?:теперь\s+)?(?:(?:нужно|надо)\s+)?(?:делать|сделать)\s*(?=[?!.;,]|$)", text):
            return True
        return False

    def reconciliation_context(self, platform, user_id, chat, replied_text=""):
        if re.search(r"обид|погас|примир|книг", str(replied_text), re.I):
            return True
        if int(self.store.state("community_reconcile_context:" + str(chat) + ":" + str(user_id), "0")) > int(time.time()) - 600:
            return True
        if platform == "twitch":
            return False
        with self.store.lock:
            rows = self.store.db.execute("SELECT answer FROM bot_reply_memory WHERE chat=? AND person=? AND at>=? ORDER BY at DESC LIMIT 2", (str(chat), str(user_id), int(time.time()) - 600)).fetchall()
        return any(re.search(r"обид|погас|примир|книг", row[0], re.I) for row in rows)

    def debt_help(self, platform, user_id, question, context=False):
        """Always explain reconciliation to a debtor, even when regular replies are paused."""
        profile = self.profile(platform, user_id)
        if profile["debt"] <= 0 and profile["reputation"] >= 0:
            return None
        question = str(question or "").casefold()
        asks_reconcile = self.reconciliation_question(question, context)
        if not asks_reconcile:
            return None
        if profile["fixed_score"] is not None:
            if profile["fixed_score"] < 0:
                return "Эту запись с неизменным уровнем назначил хранитель Книги. Попроси владельца пересмотреть звание или снять его."
            return None
        link = ""
        if re.search(r"донат|donat|donation|поддержать", question):
            for fact in self.store.fixed_facts():
                if re.search(r"донат|donat|donation|поддерж", str(fact.get("name", "")) + " " + str(fact.get("triggers", "")), re.I):
                    match = re.search(r"https?://[^\s<>]+", str(fact.get("value", "")))
                    if match:
                        link = match.group(0).rstrip(".,)")
                        break
        template = self.choose_message("help", platform, user_id, values={"donation_link": link}).strip()
        answer = template
        if self.store.setting("quest_enabled", "0") == "1" and self.store.setting("quest_" + platform + "_enabled", "1" if platform == "telegram" else "0") == "1":
            answer += " Писарь может вручить личный свиток примирения; /quest покажет твой путь." if platform == "telegram" else " О личных свитках примирения спроси писаря в Telegram через /quest."
        if re.search(r"донат|donat|donation|поддержать", question):
            if link and link not in answer:
                answer += " Поддержка добровольна; ссылка: " + link
            elif not link:
                answer += " Для добровольной поддержки писарь не нашёл сохранённой ссылки; уточни её у владельца."
        return answer[:500]

    def start_link(self, telegram_id):
        code = secrets.token_urlsafe(9).replace("-", "A").replace("_", "B")[:10]
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO community_link_codes(telegram_id,code_hash,expires_at) VALUES (?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET code_hash=excluded.code_hash,expires_at=excluded.expires_at", (str(telegram_id), digest, int(time.time()) + 600))
        return code

    def observe_username(self, platform, user_id, username):
        """Link only distinct stable IDs with matching current platform usernames."""
        user_id = str(user_id)
        username = str(username or "").lstrip("@").casefold()
        if platform not in ("twitch", "telegram") or not user_id:
            return False
        other = "telegram" if platform == "twitch" else "twitch"
        now = int(time.time())
        with self.store.lock, self.store.db:
            previous = self.store.db.execute("SELECT username FROM community_usernames WHERE platform=? AND user_id=?", (platform, user_id)).fetchone()
            if not re.fullmatch(r"[a-z0-9_]{3,25}" if platform=="twitch" else r"[a-z0-9_]{5,32}", username):
                if previous:
                    self.store.db.execute("DELETE FROM community_links WHERE method='auto' AND " + ("twitch_id" if platform == "twitch" else "telegram_id") + "=?", (user_id,))
                    self.store.db.execute("DELETE FROM community_usernames WHERE platform=? AND user_id=?", (platform, user_id))
                return False
            if previous and previous[0] != username:
                self.store.db.execute("DELETE FROM community_links WHERE method='auto' AND " + ("twitch_id" if platform == "twitch" else "telegram_id") + "=?", (user_id,))
            old_owner = self.store.db.execute("SELECT user_id FROM community_usernames WHERE platform=? AND username=?", (platform, username)).fetchone()
            if old_owner and old_owner[0] != user_id:
                self.store.db.execute("DELETE FROM community_links WHERE method='auto' AND " + ("twitch_id" if platform == "twitch" else "telegram_id") + "=?", (old_owner[0],))
                self.store.db.execute("DELETE FROM community_usernames WHERE platform=? AND username=?", (platform, username))
            self.store.db.execute("INSERT INTO community_usernames(platform,user_id,username,at) VALUES (?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET username=excluded.username,at=excluded.at", (platform, user_id, username, now))
        from .account_links import reconcile
        added=reconcile(self)
        if added:self.store.event('info','Twitch и Telegram: аккаунты связаны по однозначным нормализованным логинам.')
        return any(user_id==(pair[0] if platform=='twitch' else pair[1]) for pair in added)

    def finish_link(self, twitch_id, code):
        digest = hashlib.sha256(str(code).encode()).hexdigest()
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT telegram_id FROM community_link_codes WHERE code_hash=? AND expires_at>?", (digest, int(time.time()))).fetchone()
            if not row:
                return False
            existing = self.store.db.execute("SELECT * FROM community_links WHERE twitch_id=? OR telegram_id=?", (str(twitch_id), row[0])).fetchall()
            if existing and (len(existing) != 1 or existing[0]["twitch_id"] != str(twitch_id) or existing[0]["telegram_id"] != row[0]):
                if any(item["method"] != "auto" for item in existing):
                    raise ValueError("Аккаунт уже привязан вручную. Отвяжите его в панели.")
                self.store.db.execute("DELETE FROM community_links WHERE method='auto' AND (twitch_id=? OR telegram_id=?)", (str(twitch_id), row[0]))
            self.store.db.execute("INSERT INTO community_links(twitch_id,telegram_id,at,method) VALUES (?,?,?,'manual') ON CONFLICT(twitch_id) DO UPDATE SET telegram_id=excluded.telegram_id,at=excluded.at,method='manual'", (str(twitch_id), row[0], int(time.time())))
            self.store.db.execute("DELETE FROM community_link_blocks WHERE twitch_id=? AND telegram_id=?", (str(twitch_id), row[0]))
            self.store.db.execute("DELETE FROM community_link_codes WHERE telegram_id=?", (row[0],))
            return True

    def unlink(self, twitch_id):
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT telegram_id FROM community_links WHERE twitch_id=?", (str(twitch_id),)).fetchone()
            if row:
                self.store.db.execute("INSERT OR IGNORE INTO community_link_blocks(twitch_id,telegram_id) VALUES (?,?)", (str(twitch_id), row[0]))
            self.store.db.execute("DELETE FROM community_links WHERE twitch_id=?", (str(twitch_id),))

    def stats(self, days=-1):
        days = int(days)
        if days != -1 and not 1 <= days <= 365:
            raise ValueError("Период статистики: −1 (всё время) или от 1 до 365 дней")
        since = -1 if days == -1 else int(time.time()) - days * 86400
        with self.store.lock:
            total = self.store.db.execute("SELECT COUNT(*) FROM twitch_messages WHERE at>=?", (since,)).fetchone()[0]
            users = [dict(row) for row in self.store.db.execute("SELECT user_id,MAX(display_name) AS name,COUNT(*) AS messages FROM twitch_messages WHERE at>=? GROUP BY user_id ORDER BY messages DESC LIMIT 50", (since,))]
            emotes = [dict(row) for row in self.store.db.execute("SELECT e.emote_id,e.label,SUM(e.amount) AS uses FROM twitch_emotes e JOIN twitch_messages m ON m.id=e.message_id WHERE m.at>=? GROUP BY e.emote_id ORDER BY uses DESC LIMIT 50", (since,))]
            daily = [dict(row) for row in self.store.db.execute("SELECT date(at,'unixepoch') AS day,COUNT(*) AS messages FROM twitch_messages WHERE at>=? GROUP BY day ORDER BY day", (since,))]
            rewards = [dict(row) for row in self.store.db.execute("SELECT * FROM twitch_redemptions ORDER BY at DESC LIMIT 50")]
        return {"days": days, "total": total, "users": users, "emotes": emotes, "daily": daily, "rewards": rewards}

    def record_chat(self, event):
        from .twitch_bots import ignored
        try:extra=json.loads(self.store.setting('scout_twitch_settings','{}')).get('ignored_bots','')
        except (ValueError,TypeError):extra=''
        if ignored(event.get('chatter_user_login'),extra):return False
        msg_id = str(event.get("message_id") or "")
        user_id = str(event.get("chatter_user_id") or "")
        if not msg_id or not user_id:
            return False
        msg = event.get("message") or {}
        text = str(msg.get("text") or "")[:2000]
        stamp=event.get('sent_at');at=stamp if type(stamp) is int and 0<stamp<=int(time.time())+300 else int(time.time())
        with self.store.lock, self.store.db:
            cur = self.store.db.execute("INSERT OR IGNORE INTO twitch_messages(id,user_id,login,display_name,text,at) VALUES (?,?,?,?,?,?)", (msg_id, user_id, str(event.get("chatter_user_login") or "")[:100], str(event.get("chatter_user_name") or "")[:100], text, at))
            if not cur.rowcount:
                return False
            emotes = Counter()
            for fragment in msg.get("fragments") or []:
                emote = fragment.get("emote") if isinstance(fragment, dict) else None
                if isinstance(emote, dict) and emote.get("id"):
                    emotes[(str(emote["id"]), str(fragment.get("text") or "")[:80])] += 1
            for (emote_id, label), count in emotes.items():
                self.store.db.execute("INSERT INTO twitch_emotes(message_id,emote_id,label,amount) VALUES (?,?,?,?)", (msg_id, emote_id, label, count))
        self.see_user("twitch", user_id, event.get("chatter_user_name") or event.get("chatter_user_login"), event.get("chatter_user_login"))
        return True

    def record_reward(self, event):
        redemption_id = str(event.get("id") or "")
        reward = event.get("reward") or {}
        if not redemption_id or not event.get("user_id"):
            return None
        with self.store.lock, self.store.db:
            cur = self.store.db.execute("INSERT OR IGNORE INTO twitch_redemptions(id,user_id,login,display_name,reward_id,title,cost,user_input,at) VALUES (?,?,?,?,?,?,?,?,?)", (redemption_id, str(event["user_id"]), str(event.get("user_login") or ""), str(event.get("user_name") or ""), str(reward.get("id") or ""), str(reward.get("title") or "")[:200], int(reward.get("cost") or 0), str(event.get("user_input") or "")[:500], int(time.time())))
            if not cur.rowcount:
                return None
            rule = self.store.db.execute("SELECT * FROM twitch_reward_rules WHERE reward_id=?", (str(reward.get("id") or ""),)).fetchone()
        if rule and rule["action"] != "announce":
            self.adjust("twitch", str(event["user_id"]), str(event.get("user_name") or ""), rule["action"], rule["amount"], "Награда: " + str(reward.get("title") or ""), "reward:" + redemption_id)
        score = self.profile("twitch", str(event["user_id"]))
        return {"event": dict(event), "rule": dict(rule) if rule else None, "profile": score}

    def cleanup(self):
        days = int(self.store.setting("twitch_retention_days", "-1"))
        with self.store.lock, self.store.db:
            if days != -1:
                cutoff = int(time.time()) - _bounded(days, 30, 730) * 86400
                self.store.db.execute("DELETE FROM twitch_emotes WHERE message_id IN (SELECT id FROM twitch_messages WHERE at<?)", (cutoff,))
                self.store.db.execute("DELETE FROM twitch_messages WHERE at<?", (cutoff,))
                self.store.db.execute("DELETE FROM community_activity WHERE at<?", (cutoff,))
            self.store.db.execute("DELETE FROM community_link_codes WHERE expires_at<?", (int(time.time()),))

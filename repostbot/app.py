import hashlib
import base64
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from .core import MEMORY_MESSAGES, Store, Worker, parse_sticker_line, request_json, request_text, tg_call, telegram_upload_media, vk_posts, vk_public_url, vk_source
from .community import RANK_PHRASES
from .reply_behavior import LIMIT_MESSAGE, neutral_limit_message
from .message_template import render_youtube_template, render_vk_template
from .trigger_pack import export_pack, import_pack
from .update import CURRENT_VERSION, MAX_UPLOAD, pending_version, stage_package

ROOT = Path(__file__).parent
SECRETS = ("telegram_token", "vk_token", "vk_cookie", "openai_api_key", "weather_api_key")
SETTINGS = ("telegram_token", "vk_token", "vk_cookie", "vk_interval", "vk_ssl_verify", "reply_enabled", "reply_chats", "llm_enabled", "ollama_url", "ollama_model", "system_prompt", "fallback", "ai_provider", "ai_provider_order", "ai_enabled_providers", "openai_api_key", "openai_model", "chatgpt_model", "ai_max_tokens", "memory_turns", "memory_days", "memory_chats", "image_enabled", "image_chats", "image_model", "image_quality", "reply_vision_enabled", "reply_stickers_enabled", "reply_stickers", "reply_sticker_catalog_backup", "reply_sticker_semantics", "reply_sticker_formats", "reply_sticker_thumbs", "reply_sticker_packs", "reply_sticker_disabled", "reply_sticker_learning_pending", "reply_sticker_quarantine_sources", "reply_sticker_chance", "reply_sticker_default_key", "weather_enabled", "weather_api_key", "ai_limit_message")
DEFAULTS = {"vk_interval": "120", "vk_ssl_verify": "1", "reply_enabled": "1", "reply_chats": "", "llm_enabled": "0", "ollama_url": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"), "ollama_model": os.environ.get("OLLAMA_MODEL", "qwen3:4b"), "system_prompt": "Ты вежливый помощник сообщества. Отвечай кратко по-русски. Если точного ответа нет, предложи уточнить у менеджера. Не придумывай цены, адреса, наличие и сроки.", "fallback": "Уточните, пожалуйста, вопрос — я передам его человеку."}
DEFAULTS.update({"ai_provider": "faq", "ai_provider_order": "chatgpt,openai,local", "ai_enabled_providers": "chatgpt,openai,local", "openai_model": "gpt-4.1-mini", "chatgpt_model": "", "ai_max_tokens": "300", "memory_turns": "2", "memory_days": "-1", "memory_chats": "", "image_enabled": "0", "image_chats": "", "image_model": "gpt-image-1", "image_quality": "low"})
DEFAULTS.update({"reply_vision_enabled": "0", "reply_stickers_enabled": "0", "reply_stickers": "", "reply_sticker_catalog_backup": "", "reply_sticker_semantics": "{}", "reply_sticker_formats": "{}", "reply_sticker_thumbs": "{}", "reply_sticker_packs": "{}", "reply_sticker_disabled": "[]", "reply_sticker_learning_pending": "{}", "reply_sticker_quarantine_sources": "{}", "reply_sticker_chance": "10", "reply_sticker_default_key": "", "weather_enabled": "0"})
DEFAULTS["ai_limit_message"] = LIMIT_MESSAGE
STICKER_SETTINGS = {"reply_stickers_enabled", "reply_stickers", "reply_sticker_catalog_backup", "reply_sticker_semantics", "reply_sticker_formats", "reply_sticker_thumbs", "reply_sticker_packs", "reply_sticker_disabled", "reply_sticker_learning_pending", "reply_sticker_quarantine_sources", "reply_sticker_chance", "reply_sticker_default_key"}
STICKER_CATALOG_SETTINGS = STICKER_SETTINGS - {"reply_stickers_enabled", "reply_sticker_chance", "reply_sticker_default_key"}
SETTINGS += ("ai_error_message", "admin_captcha_message", "admin_alerts", "admin_user_id", "admin_alert_cooldown")
SETTINGS += ("reply_names",)
SETTINGS += tuple(MEMORY_MESSAGES)
DEFAULTS["reply_names"] = "помощник, помощник, Assistant"
DEFAULTS.update(MEMORY_MESSAGES)
DEFAULTS.update({"ai_error_message": "В боте возникла ошибка. Проверьте журнал активности.", "admin_captcha_message": "VK просит решить капчу. Откройте страницу источника в браузере и решите её; затем повторите парсинг.", "admin_alerts": "0", "admin_user_id": "", "admin_alert_cooldown": "600"})
SETTINGS += ("route_reaction_enabled", "route_reaction_chance", "route_reaction_prompt", "route_reaction_chats", "route_reaction_repeat_hours")
DEFAULTS.update({"route_reaction_enabled": "0", "route_reaction_chance": "20", "route_reaction_prompt": "Напиши короткую добрую шутку по теме публикации.", "route_reaction_chats": "", "route_reaction_repeat_hours": "24"})
SETTINGS += ("stream_pin_enabled", "stream_pin_delete_service", "stream_pin_chats")
SETTINGS += ("route_reaction_game_facts",)
DEFAULTS["route_reaction_game_facts"] = "1"
DEFAULTS.update({"stream_pin_enabled": "1", "stream_pin_delete_service": "1", "stream_pin_chats": ""})


def admin_session_secret(store, password):
    """Keep signatures across restarts, but invalidate them on password change.

    The random seed lives outside editable/exported settings in the data DB,
    which the code-only updater preserves.
    """
    with store.lock, store.db:
        store.db.execute("CREATE TABLE IF NOT EXISTS web_auth (id INTEGER PRIMARY KEY CHECK(id=1), seed BLOB NOT NULL)")
        store.db.execute("INSERT OR IGNORE INTO web_auth(id,seed) VALUES(1,?)", (secrets.token_bytes(32),))
        seed = store.db.execute("SELECT seed FROM web_auth WHERE id=1").fetchone()[0]
    if not isinstance(seed, bytes) or len(seed) != 32:
        raise ValueError("Повреждён ключ входа в панель")
    return hmac.new(seed, b"repostbot-admin-session-v1\0" + password.encode("utf-8"), hashlib.sha256).digest()


class Handler(BaseHTTPRequestHandler):
    server_version = "RepostBot/1.0"

    def log_message(self, fmt, *args):
        # Never log a URL containing a token.
        logging.info("Web request: %s", self.command)

    def respond(self, status, data, cookies=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookies:
            self.send_header("Set-Cookie", cookies)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self, max_size=128_000):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > max_size:
            raise ValueError("Слишком большой запрос")
        return json.loads(self.rfile.read(size) or b"{}")

    def upload_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > MAX_UPLOAD:
            raise ValueError("Архив должен быть размером до 20 МБ")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Архив передан не полностью")
        return raw

    def signed(self, timestamp):
        signature = hmac.new(self.server.secret, timestamp.encode(), hashlib.sha256).hexdigest()
        return timestamp + "." + signature

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie["session"].value
            timestamp, signature = value.split(".", 1)
            return int(timestamp) > time.time() and hmac.compare_digest(self.signed(timestamp), value)
        except (KeyError, ValueError, TypeError):
            return False

    def csrf_ok(self):
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            return False
        return self.headers.get("X-Repostbot-Request") == "1"

    def do_GET(self):
        path = urlparse(self.path).path
        page_path = path in ("/", "/connections", "/routes", "/routes/new", "/answers", "/models", "/stickers", "/conversation", "/speech", "/memory", "/templates", "/triggers", "/twitch", "/moderation", "/activity", "/decision-log", "/social", "/analytics", "/updates", "/scout") or bool(re.fullmatch(r"/routes/\d+(?:/settings)?", path))
        if page_path or path in ("/static/app.js", "/static/style.css"):
            file = "index.html" if page_path else {"/static/app.js": "app.js", "/static/style.css": "style.css"}[path]
            raw = (ROOT / "static" / file).read_bytes()
            if file == "index.html":
                raw = raw.replace(b'<script src="/static/app.js"></script>', f'<script src="/static/app.js?v={CURRENT_VERSION}"></script>'.encode())
                raw = raw.replace(b'href="/static/style.css"', f'href="/static/style.css?v={CURRENT_VERSION}"'.encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if file.endswith("html") else "application/javascript; charset=utf-8" if file.endswith("js") else "text/css; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/session":
            self.respond(200, {"authenticated": self.authorized()})
            return
        if path == "/api/twitch/callback":
            query = parse_qs(urlparse(self.path).query)
            try:
                if query.get("error"):
                    raise ValueError("Вход Twitch отменён: " + query["error"][0][:100])
                self.server.worker.twitch.complete(query.get("state", [""])[0], query.get("code", [""])[0])
                location = "/twitch?connected=1"
            except (ValueError, RuntimeError, KeyError) as exc:
                location = "/twitch?error=" + quote(str(exc)[:180])
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if not self.authorized():
            self.respond(401, {"error": "Требуется вход"})
            return
        s = self.server.store
        if path == '/api/twitch/emotes':
            self.respond(200,self.server.worker.twitch_scout.emotes.panel())
        elif path == '/api/twitch/emotes/image':
            try:
                mime,raw=self.server.worker.twitch_scout.emotes.image(parse_qs(urlparse(self.path).query).get('id',['0'])[0])
                self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
            except Exception:self.respond(404,{'error':'Картинка смайла недоступна'})
        elif path == '/api/scout/twitch':
            self.respond(200,self.server.worker.twitch_scout.panel())
        elif path in ('/api/scout/twitch/log','/api/scout/twitch/export'):
            query=parse_qs(urlparse(self.path).query);ledger=self.server.worker.twitch_scout.ledger
            try:
                if path.endswith('/log'):
                    self.respond(200,ledger.panel(query.get('day',[None])[0],query.get('kind',['all'])[0],query.get('offset',['0'])[0]))
                else:
                    day=query.get('day',[''])[0];format=query.get('format',['jsonl'])[0];raw=ledger.export(day,format)
                    self.send_response(200);self.send_header('Content-Type','application/x-ndjson; charset=utf-8' if format=='jsonl' else 'text/plain; charset=utf-8')
                    self.send_header('Content-Disposition','attachment; filename="twitch-'+ledger.owner.channel+'-'+day+'.'+format+'"')
                    self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except (ValueError,TypeError) as exc:self.respond(400,{'error':str(exc)})
        elif path == '/api/repost-stats':
            query=parse_qs(urlparse(self.path).query);stats=self.server.worker.repost_stats
            chat=query.get('chat',[s.setting('repost_stats_main_chat')])[0]
            self.respond(200,{'chats':stats.chats(),'main_chat':s.setting('repost_stats_main_chat'),'genres':stats.genres(),'report':stats.report(chat) if chat else None})
        elif path == '/api/scout/twitch/frame':
            try:raw=self.server.worker.twitch_scout.browser.request('frame')
            except RuntimeError as exc:self.respond(409,{'error':str(exc)});return
            self.send_response(200);self.send_header('Content-Type','image/jpeg')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
        elif path == '/api/scout':
            self.respond(200, self.server.worker.scout.panel())
        elif path == '/api/conversation/glitch-avatar':
            from .glitch_avatar_assets import avatar,ASSETS
            style=parse_qs(urlparse(self.path).query).get('style',['custom'])[0]
            if style not in ASSETS:
                self.respond(400,{'error':'Неизвестный стиль сбоя.'});return
            raw=avatar(style)
            self.send_response(200);self.send_header('Content-Type','image/jpeg')
            self.send_header('Cache-Control','private, max-age=3600');self.end_headers();self.wfile.write(raw)
        elif path == '/api/scout/avatar':
            from .scout_avatar_assets import avatar, ASSETS
            mode = parse_qs(urlparse(self.path).query).get('mode', ['scout'])[0]
            if mode not in ASSETS:
                self.respond(400, {'error':'Неизвестный аватар.'})
                return
            raw = avatar(mode)
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'private, max-age=3600')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/config":
            settings = {k: s.setting(k, DEFAULTS.get(k, "")) for k in SETTINGS if k not in SECRETS}
            settings["ai_limit_message"] = neutral_limit_message(settings["ai_limit_message"])
            settings.update({k + "_configured": bool(s.setting(k)) for k in SECRETS})
            settings["ai_provider"] = self.server.worker.ai_provider()
            if self.server.worker.models.enabled():
                settings['ai_provider'] = s.setting('ai_provider', 'local' if s.setting('llm_enabled', '0') == '1' else 'faq')
            routes = s.routes()
            self.respond(200, {"settings": settings, "routes": routes, "route_posts": {str(route["id"]): s.route_posts(route["id"], 5) for route in routes if route["source_type"] in ("vk", "vk_public", "youtube")}, "faqs": s.faqs(), "facts": s.fixed_facts(), "profiles": s.chat_profiles(), "memory_people": s.memory_people(), "corrections": s.corrections(), "triggers": s.triggers(), "chats": s.chats(), "events": s.history(), "bot_username": self.server.worker.bot_username})
        elif path == "/api/twitch/status":
            self.respond(200, self.server.worker.twitch.status())
        elif path == "/api/stickers":
            self.respond(200, {"enabled": s.setting("reply_stickers_enabled", "0") == "1", "stickers": self.server.worker.sticker_panel()})
        elif path == "/api/conversation":
            self.respond(200, {"settings": self.server.worker.conversation.settings(), "presence": self.server.worker.presence.settings(), "events": self.server.worker.conversation.recent(), "presence_events": self.server.worker.presence.recent(), "statuses": self.server.worker.conversation.statuses(), "glitch_profile":self.server.worker.glitch_profile.panel(), "provider": self.server.worker.ai_provider(), "reply_enabled": s.setting("reply_enabled", "1") == "1", "vision_enabled": s.setting("reply_vision_enabled", "0") == "1"})
        elif path == "/api/route-reaction/journal":
            params=parse_qs(urlparse(self.path).query)
            self.respond(200,{"events":self.server.worker.series.journal(**{key:params.get(key,[""])[0] for key in ("chat","game","event","q")})})
        elif path == "/api/route-reaction/series":
            params = parse_qs(urlparse(self.path).query)
            self.respond(200, self.server.worker.series.panel(params.get("chat", [""])[0]))
        elif path == "/api/reply-performance":
            self.respond(200, {"events": self.server.worker.reply_metrics.recent(), "memory_index": s.memory_search.status()})
        elif path == "/api/decision-log":
            params = parse_qs(urlparse(self.path).query)
            self.respond(200, {"events": s.decision_log(params.get("module", [""])[0], params.get("chat", [""])[0], params.get("q", [""])[0], params.get("limit", ["200"])[0])})
        elif path == "/api/social":
            self.respond(200, self.server.worker.social.panel())
        elif path == '/api/social/participants':
            self.respond(200,self.server.worker.participants.panel())
        elif path == "/api/social/role-history":
            query = parse_qs(urlparse(self.path).query)
            try:
                self.respond(200, {"events": self.server.worker.social.role_history(
                    chat=query.get("chat", [""])[0], person=query.get("person", [""])[0],
                    day=query.get("day", [""])[0], limit=query.get("limit", ["100"])[0])})
            except ValueError:
                self.respond(400, {"error": "Проверьте дату и лимит записей журнала ролей"})
        elif path == '/api/models':
            self.respond(200, self.server.worker.models.panel())
        elif path == '/api/speech':
            self.respond(200, self.server.worker.speech.panel())
        elif path == "/api/quests":
            self.respond(200, self.server.worker.quests.panel())
        elif path == "/api/telegram/service-messages":
            self.respond(200, {"settings": self.server.worker.service_messages.settings()})
        elif path == "/api/stream-pin/settings":
            self.respond(200, {"enabled": s.setting("stream_pin_enabled", DEFAULTS["stream_pin_enabled"]) == "1",
                               "delete_service": s.setting("stream_pin_delete_service", DEFAULTS["stream_pin_delete_service"]) == "1",
                               "chats": s.setting("stream_pin_chats", DEFAULTS["stream_pin_chats"])})
        elif path == "/api/telegram/templates":
            self.respond(200, {"templates": s.telegram_templates()})
        elif path == "/api/twitch/stats":
            days = parse_qs(urlparse(self.path).query).get("days", ["-1"])[0]
            try:
                stats = self.server.worker.community.stats(days)
            except ValueError as exc:
                self.respond(400, {"error": str(exc)})
            else:
                self.respond(200, stats)
        elif path == "/api/moderation":
            moderation = self.server.worker.moderation
            self.respond(200, {"settings": moderation.settings(), "events": moderation.recent()})
        elif path == "/api/moderation/lexicon":
            params = parse_qs(urlparse(self.path).query)
            self.respond(200, self.server.worker.community.lexicon(params.get("query", [""])[0], params.get("offset", ["0"])[0], params.get("limit", ["50"])[0], kind=params.get("kind", ["both"])[0]))
        elif path == "/api/moderation/lexicon/group":
            params = parse_qs(urlparse(self.path).query)
            self.respond(200, self.server.worker.community.lexicon_group(params.get("id", [""])[0], params.get("offset", ["0"])[0]))
        elif path == "/api/community":
            community = self.server.worker.community
            self.respond(200, {"enabled": s.setting("community_enabled", "0") == "1", "media_scan_enabled": s.setting("community_media_scan_enabled", "1") == "1", "ai_scan_enabled": s.setting("community_ai_scan_enabled", "1") == "1", "retention_days": int(s.setting("twitch_retention_days", "-1")), "greeting_min": int(s.setting("community_greeting_min", "40")),
                               "notify_greeting": s.setting("community_notify_greeting", "0") == "1", "notify_banword": s.setting("community_notify_banword", "0") == "1", "notify_severe": s.setting("community_notify_severe", "0") == "1", "notify_activity": s.setting("community_notify_activity", "0") == "1", "notify_reward": s.setting("community_notify_reward", "0") == "1",
                               **{"reply_receipt_" + platform: s.setting("community_reply_receipt_" + platform, "1") == "1" for platform in ("telegram", "twitch")},
                               "quiet_penalty_max": int(s.setting("community_quiet_penalty_max", "10")),
                               "notify_transition": s.setting('community_notify_transition', '1') == '1',
                               **{'notify_transition_' + platform: s.setting('community_notify_transition_' + platform, '1') == '1' for platform in ('telegram', 'twitch')},
                               **{'transition_' + level: s.setting('community_transition_' + level, '0' if level == 'hint' else '1') == '1' for level in ('hint', 'strong', 'refuse')},
                               'transition_cooldown_minutes': int(s.setting('community_transition_cooldown_minutes', '5')),
                               **{"notify_" + kind + "_" + platform: s.setting("community_notify_" + kind + "_" + platform, "1") == "1" for kind in ("greeting", "banword", "severe", "activity", "reward") for platform in ("twitch", "telegram")},
                               "severe_mentions_twitch": s.setting("community_severe_mentions_twitch", ""), "severe_mentions_telegram": s.setting("community_severe_mentions_telegram", ""),
                               "activity_stop": s.setting("community_activity_stop", "0") == "1", "activity_stop_debt": int(s.setting("community_activity_stop_debt", "50")),
                               "activity_cap": float(s.setting("community_activity_cap", "20")), "activity_notify_every": float(s.setting("community_activity_notify_every", "5")),
                               **{"activity_rate_" + level: float(s.setting("community_activity_rate_" + level, default)) for level, default in (("normal", "0.5"), ("hint", "0.3"), ("strong", "0.2"), ("refuse", "0.1"))},
                               **{"activity_rate_twitch_" + level: float(s.setting("community_activity_rate_twitch_" + level, s.setting("community_activity_rate_" + level, default))) for level, default in (("normal", "0.5"), ("hint", "0.3"), ("strong", "0.2"), ("refuse", "0.1"))},
                               "hint_answer_chance": int(s.setting("community_hint_answer_chance", "50")), "strong_answer_chance": int(s.setting("community_strong_answer_chance", "10")),
                               "honor_message": community.honor_message(),
                               **{"rank_" + level: s.setting("community_rank_" + level, default) for level, default in RANK_PHRASES.items()},
                               **{"ai_" + level: s.setting("community_ai_" + level, "0") == "1" for level in ("hint", "strong", "refuse", "help")},
                               **{"ai_" + level + "_fallback": level in ("help", "refuse") or s.setting("community_ai_" + level + "_fallback", "1") == "1" for level in ("hint", "strong", "refuse", "help")},
                               "grudge_strong_at": int(s.setting("community_grudge_strong_at", "-21")), "grudge_refuse_at": int(s.setting("community_grudge_refuse_at", "-50")),
                               **{"grudge_" + level: community.template_text(level) for level in ("hint", "strong", "refuse", "help")},
                               **{"message_" + kind: community.template_text(kind) for kind in ("greeting", "banword", "severe", "activity", "reward", "reply_grudge", "reply_roleplay", 'transition_hint', 'transition_strong', 'transition_refuse')},
                               "terms_count": community.term_count(), "reward_rules": community.reward_rules(), "profiles": community.recent_profiles(), "protected": community.protected_people(), "honors": community.honors(), "pending": community.pending_people(), "groups": community.group_coverage(), "mtproto": community.mtproto_status()})
        elif path == "/api/triggers/export":
            self.respond(200, export_pack(s))
        elif path == "/api/community/terms/export":
            self.respond(200, self.server.worker.community.export_terms())
        elif re.fullmatch(r"/api/route/\d+/public-html", path):
            route_id = int(path.split("/")[3])
            route = next((item for item in s.routes() if item["id"] == route_id and item["source_type"] == "vk_public"), None)
            if not route:
                self.respond(404, {"error": "Маршрут VK не найден"})
                return
            try:
                page = request_text(vk_public_url(route["source"]), timeout=25, cookie="", verify_tls=s.setting("vk_ssl_verify", "1") != "0")
            except (RuntimeError, OSError, ValueError) as exc:
                self.respond(502, {"error": str(exc)[:300]})
                return
            raw = page[:512 * 1024].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif re.fullmatch(r"/api/route/\d+/diagnostic-html", path):
            route_id = int(path.split("/")[3])
            sample = s.data_dir / "vk-diagnostics" / (str(route_id) + ".html.txt")
            if not sample.is_file() or not any(route["id"] == route_id for route in s.routes()):
                self.respond(404, {"error": "Диагностический HTML недоступен"})
                return
            raw = sample.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="vk-route-{route_id}.html.txt"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path.startswith("/api/route/") and path.endswith("/posts"):
            try:
                route_id = int(path.split("/")[3])
            except (ValueError, IndexError):
                self.respond(400, {"error": "Неверный маршрут"})
                return
            route = next((item for item in s.routes() if item["id"] == route_id), None)
            if route and route["source_type"] == "youtube":
                diagnostic = {"channel": json.loads(s.state("youtube_channel:" + str(route_id), "{}")),
                              "last_poll_at": route["last_poll_at"], "last_success_at": route["last_success_at"],
                              "last_post_count": route["last_post_count"], "last_error": route["last_error"]}
            else:
                diagnostic = json.loads(s.state("vk_diagnostic:" + str(route_id), "{}"))
                diagnostic["html_available"] = (s.data_dir / "vk-diagnostics" / (str(route_id) + ".html.txt")).is_file()
            self.respond(200, {"posts": s.route_posts(route_id, 5), "history": s.route_post_history(route_id), "events": s.history(route_id), "route": route, "diagnostic": diagnostic})
        elif path == "/api/ai/status":
            self.respond(200, {"chatgpt": self.server.worker.chatgpt.status(), "chain": self.server.worker.model_health_status(), "usage": s.ai_usage_summary()})
        elif path == "/api/ai/usage":
            days = parse_qs(urlparse(self.path).query).get("days", ["7"])[0]
            try:
                report = s.ai_usage_report(days)
            except ValueError as exc:
                self.respond(400, {"error": str(exc)})
            else:
                self.respond(200, report)
        elif path.startswith("/api/media/"):
            try:
                media = s.media_file(int(path.rsplit("/", 1)[1]))
            except (ValueError, IndexError):
                media = None
            if not media:
                self.respond(404, {"error": "Изображение уже удалено из кэша"})
                return
            file_path, mime = media
            try:
                raw = Path(file_path).read_bytes()
            except OSError:
                self.respond(404, {"error": "Изображение уже удалено из кэша"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/model/status":
            model = s.setting("ollama_model", DEFAULTS["ollama_model"])
            base = s.setting("ollama_url", DEFAULTS["ollama_url"]).rstrip("/")
            setup = getattr(self.server, "local_setup", {})
            try:
                models = request_json(base + "/api/tags", timeout=4).get("models", [])
                names = [str(m.get("name") or m.get("model")) for m in models]
                self.respond(200, {"connected": True, "ready": model.casefold() in [n.casefold() for n in names], "enabled": s.setting("llm_enabled", "0") == "1", "model": model, "available": names[:30], "installing": setup.get("state") == "pulling", "install_error": setup.get("error", ""), "install_progress": setup.get("progress", "")})
            except Exception as exc:
                detail = str(exc)[:260]
                if "Name or service not known" in detail or "nodename nor servname" in detail:
                    detail = "Не найден сервис Ollama по адресу " + base + ". В Compose проверьте, что сервисы bot и ollama запущены в одном проекте; для Ollama на хосте используйте host.docker.internal:11434."
                self.respond(200, {"connected": False, "ready": False, "enabled": s.setting("llm_enabled", "0") == "1", "model": model, "available": [], "installing": setup.get("state") == "pulling", "install_error": setup.get("error", "") or detail, "install_progress": setup.get("progress", "")})
        elif path == "/api/update/status":
            self.respond(200, {"version": CURRENT_VERSION, "supported": self.server.windows_launcher or getattr(self.server, "linux_launcher", False) or getattr(self.server, "container_launcher", False), "pending_version": pending_version(self.server.data_dir)})
        elif path == "/api/memory/search":
            query = parse_qs(urlparse(self.path).query)
            person = query.get("person", [""])[0][:100]
            topic = query.get("q", [""])[0][:150]
            if not person or not topic or not any(item["person"] == person for item in s.memory_people()):
                self.respond(400, {"error": "Укажите пользователя из списка и тему поиска"})
            else:
                self.respond(200, {"messages": s.relevant_chat_memory("", person, topic)})
        elif path == "/api/health":
            self.respond(200, {"telegram_configured": bool(s.setting("telegram_token")), "vk_configured": bool(s.setting("vk_token")), "bot_username": self.server.worker.bot_username})
        else:
            self.respond(404, {"error": "Не найдено"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.csrf_ok():
            self.respond(403, {"error": "Неверный источник запроса"})
            return
        if path != "/api/login" and not self.authorized():
            self.respond(401, {"error": "Требуется вход"})
            return
        try:
            if path == "/api/update/upload":
                if not (self.server.windows_launcher or getattr(self.server, "linux_launcher", False) or getattr(self.server, "container_launcher", False)):
                    raise ValueError("Обновление через панель доступно при запуске через файл запуска или контейнерный Compose")
                version = stage_package(self.upload_body(), self.server.data_dir)
                self.respond(200, {"version": version})
                return
            if path == "/api/telegram/media/upload":
                query = parse_qs(urlparse(self.path).query)
                kind = query.get("type", [""])[0]
                name = query.get("name", ["upload.bin"])[0]
                if not self.server.store.setting("admin_user_id") or not self.server.store.setting("telegram_token"):
                    raise ValueError("Укажите Telegram ID администратора и токен бота в панели, затем откройте личный чат с ботом")
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 8_000_000:
                    raise ValueError("Файл должен быть размером до 8 МБ")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Файл передан не полностью")
                file_id = telegram_upload_media(self.server.store.setting("telegram_token"), self.server.store.setting("admin_user_id"), kind, name, raw)
                self.respond(200, {"type": kind, "file_id": file_id})
                return
            data = self.body(2_000_000 if path in ("/api/triggers/import", "/api/community/terms/import-pack", "/api/twitch/emotes/save") else 512_000 if path == "/api/settings" else 256_000 if path == "/api/community/terms/import" else 128_000)
            s = self.server.store
            if path == "/api/login":
                if not hmac.compare_digest(str(data.get("password", "")), self.server.password):
                    time.sleep(0.4)
                    self.respond(401, {"error": "Неверный пароль"})
                    return
                cookie = "session=" + self.signed(str(int(time.time() + 86400))) + "; Max-Age=86400; HttpOnly; SameSite=Strict; Path=/"
                self.respond(200, {"ok": True}, cookie)
            elif path == "/api/logout":
                self.respond(200, {"ok": True}, "session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
            elif path == "/api/shutdown":
                self.respond(202, {"ok": True})
                # HTTPServer.shutdown() must run outside the serving thread.
                # Respond first so the panel can show the stopped state.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == '/api/twitch/emotes/save':
                self.respond(200,self.server.worker.twitch_scout.emotes.save(data))
            elif path == '/api/twitch/emotes/change':
                self.respond(200,self.server.worker.twitch_scout.emotes.change(data.get('id'),data.get('enabled')))
            elif path == '/api/twitch/emotes/analyze':
                self.respond(200,self.server.worker.twitch_scout.emotes.learn(data.get('ids') or []))
            elif path == '/api/twitch/emotes/import':
                scout=self.server.worker.twitch_scout
                result=scout.emotes.import_channel(data.get('channel') or 'example_channel',True)
                if scout.browser.running and scout.authenticated:scout.browser.request('emotes',{'learn':True},wait=False)
                self.respond(200,result)
            elif path == '/api/scout/twitch/settings':
                self.respond(200,self.server.worker.twitch_scout.configure(data))
            elif path == '/api/scout/twitch/open':
                self.respond(200,self.server.worker.twitch_scout.open_browser(data.get('destination','login')))
            elif path == '/api/scout/twitch/input':
                self.respond(200,self.server.worker.twitch_scout.browser.request('input',data))
            elif path == '/api/scout/twitch/session':
                self.respond(200,self.server.worker.twitch_scout.import_session(data.get('token')))
            elif path == '/api/scout/twitch/disconnect':
                self.respond(200,self.server.worker.twitch_scout.disconnect())
            elif path == '/api/scout/settings':
                self.respond(200, self.server.worker.scout.configure(data))
            elif path == '/api/scout/disguise':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.disguise.request(data.get('mode',''))))
            elif path == '/api/scout/code':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.request_code(data.get('phone', ''))))
            elif path == '/api/scout/verify':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.verify(data.get('code', ''), data.get('password', ''))))
            elif path == '/api/scout/disconnect':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout._disconnect(True)))
            elif path == '/api/scout/connect':
                scout = self.server.worker.scout
                scout.call(scout.connect(refresh=True))
                self.respond(200, scout.panel())
            elif path == '/api/scout/target':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.resolve(data.get('reference'), bool(data.get('join')), data.get('destination', ''))))
            elif path == '/api/scout/target-change':
                self.respond(200, self.server.worker.scout.change_target(data.get('id'), data.get('enabled')))
            elif path == '/api/scout/joined-change':
                self.respond(200,self.server.worker.scout.joined.change(data.get('chat'),data.get('enabled')))
            elif path == '/api/scout/history-clear':
                scout=self.server.worker.scout
                self.respond(200,scout.call(scout.history.clear_now(),timeout=150,busy=False))
            elif path == '/api/scout/test-dialog':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.preview_dialog(data.get('text', '')), timeout=150))
            elif path == '/api/scout/report-preview':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.preview_report(), timeout=150))
            elif path == '/api/scout/scene-preview':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.preview_scene(data.get('scenario','')), timeout=150))
            elif path == '/api/scout/scene-test':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.testing.scene(data.get('scenario', '')), timeout=150))
            elif path == '/api/scout/dialog-test':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.testing.dialog(data.get('text', '')), timeout=150))
            elif path == '/api/scout/report-test':
                scout = self.server.worker.scout
                self.respond(200, scout.call(scout.test_send(data.get('draft_id')), timeout=150))
            elif path == "/api/settings":
                values = {k: str(v).strip() for k, v in data.items() if k in SETTINGS and not k.startswith("route_reaction_") and (k not in SECRETS or str(v).strip())}
                if data.get("_scope") == "answers":
                    for key in STICKER_SETTINGS:
                        values.pop(key, None)
                # Legacy clients may submit an unchanged catalog with a prompt.
                if "reply_stickers" in values and values["reply_stickers"] == s.setting("reply_stickers").strip():
                    for key in STICKER_CATALOG_SETTINGS:
                        if key in values and values[key] == s.setting(key).strip():
                            values.pop(key)
                # The legacy answers form still contains hidden sticker
                # fields. An empty stale copy must never erase the dedicated
                # sticker catalog; a non-empty value remains supported for
                # compatibility with older installations.
                if not values.get("reply_stickers", "") and s.setting("reply_stickers", ""):
                    for key in STICKER_CATALOG_SETTINGS:
                        values.pop(key, None)
                if "vk_interval" in values:
                    values["vk_interval"] = str(max(30, min(3600, int(values["vk_interval"]))))
                if "ollama_url" in values:
                    parsed = urlparse(values["ollama_url"])
                    if parsed.scheme not in ("http", "https") or not parsed.netloc:
                        raise ValueError("Неверный адрес Ollama")
                if "ai_provider" in values:
                    if values["ai_provider"] not in ("faq", "local", "chatgpt", "openai", "chain"):
                        raise ValueError("Неизвестный поставщик ответов")
                    values["llm_enabled"] = "1" if values["ai_provider"] == "local" else "0"
                for key in ("ai_provider_order", "ai_enabled_providers"):
                    if key in values:
                        items = [item.casefold() for item in re.split(r"[,;\s]+", values[key]) if item.strip()]
                        if len(items) > 3 or len(set(items)) != len(items) or any(item not in ("chatgpt", "openai", "local") for item in items):
                            raise ValueError("Порядок и список моделей могут содержать только ChatGPT, OpenAI API и Ollama")
                        values[key] = ",".join(items)
                if "chatgpt_model" in values:
                    values["chatgpt_model_account"] = self.server.worker.chatgpt.status()["active"]
                if "image_chats" in values:
                    chats = [item.strip() for item in re.split(r"[,;\n\s]+", values["image_chats"]) if item.strip()]
                    if len(chats) > 100 or any(not re.fullmatch(r"-?[1-9]\d{0,19}", item) for item in chats):
                        raise ValueError("Укажите до 100 числовых ID для /image, по одному в строке")
                    values["image_chats"] = "\n".join(dict.fromkeys(str(int(item)) for item in chats))
                for key, low, high in (("ai_max_tokens", 64, 4096), ("memory_turns", 0, 6)):
                    if key in values:
                        values[key] = str(max(low, min(high, int(values[key]))))
                if "memory_days" in values:
                    days = int(values["memory_days"])
                    if days != -1 and not 1 <= days <= 365:
                        raise ValueError("Срок хранения памяти: −1 (бессрочно) или от 1 до 365 дней")
                    values["memory_days"] = str(days)
                if "memory_chats" in values:
                    chats = [item.strip() for item in re.split(r"[,\n]", values["memory_chats"]) if item.strip()]
                    if len(chats) > 20 or any(not re.fullmatch(r"-?[1-9]\d{0,19}", item) for item in chats):
                        raise ValueError("Укажите до 20 числовых ID чатов, по одному в строке")
                    values["memory_chats"] = "\n".join(dict.fromkeys(str(int(item)) for item in chats))
                if "admin_alert_cooldown" in values:
                    values["admin_alert_cooldown"] = str(max(60, min(86400, int(values["admin_alert_cooldown"]))))
                if values.get("admin_user_id") and not re.fullmatch(r"[1-9]\d{0,19}", values["admin_user_id"]):
                    raise ValueError("ID администратора — положительное число")
                if "admin_alerts" in values and values["admin_alerts"] not in ("0", "1"):
                    raise ValueError("Неверная настройка уведомлений")
                if values.get("admin_alerts", s.setting("admin_alerts", "0")) == "1" and not values.get("admin_user_id", s.setting("admin_user_id")):
                    raise ValueError("Укажите личный Telegram ID администратора")
                for key in ("ai_limit_message", "ai_error_message", "admin_captcha_message"):
                    if key in values and (not values[key] or len(values[key]) > 3500):
                        raise ValueError("Сообщение должно содержать от 1 до 3500 символов")
                if "ai_limit_message" in values:
                    values["ai_limit_message"] = neutral_limit_message(values["ai_limit_message"])
                for key in MEMORY_MESSAGES:
                    if key in values and (not values[key] or len(values[key]) > 500):
                        raise ValueError("Ответ памяти должен содержать от 1 до 500 символов")
                if "image_quality" in values and values["image_quality"] not in ("low", "medium", "high"):
                    raise ValueError("Неизвестное качество изображения")
                if any(values.get(key, "0") not in ("0", "1") for key in ("image_enabled", "reply_vision_enabled", "reply_stickers_enabled", "weather_enabled")):
                    raise ValueError("Неверная настройка изображений")
                if "weather_api_key" in values and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", values["weather_api_key"]):
                    raise ValueError("Неверный ключ прогноза")
                if "reply_stickers" in values:
                    lines = [line.strip() for line in values["reply_stickers"].splitlines() if line.strip()]
                    if len(values["reply_stickers"]) > 100_000:
                        raise ValueError("Список стикеров слишком большой (до 100 000 символов)")
                    keys = set()
                    normalized = []
                    for line in lines:
                        key, file_id, description = parse_sticker_line(line)
                        keys.add(key)
                        normalized.append("|".join((key, file_id, description)) if description else key + "|" + file_id)
                    values["reply_stickers"] = "\n".join(normalized)
                    origins = self.server.worker._sticker_json_map("reply_sticker_origins")
                    origins.update({parse_sticker_line(line)[0]: "manual" for line in normalized})
                    values["reply_sticker_origins"] = json.dumps(origins, ensure_ascii=False)
                if "reply_sticker_chance" in values:
                    chance = int(values["reply_sticker_chance"])
                    if not 0 <= chance <= 100:
                        raise ValueError("Шанс стикера: от 0 до 100%")
                    values["reply_sticker_chance"] = str(chance)
                if "reply_sticker_default_key" in values:
                    key = values["reply_sticker_default_key"].casefold()
                    if key and not re.fullmatch(r"[a-zа-яё0-9_-]{1,24}", key):
                        raise ValueError("Укажите короткий ключ стикера по умолчанию")
                    if key and key not in {parse_sticker_line(line)[0] for line in values.get("reply_stickers", s.setting("reply_stickers")).splitlines() if line.strip()}:
                        raise ValueError("Стикер по умолчанию должен быть в сохранённом списке")
                    values["reply_sticker_default_key"] = key
                if "vk_cookie" in values:
                    cookie = values["vk_cookie"].removeprefix("Cookie:").strip()
                    if "\n" in cookie or "\r" in cookie or "=" not in cookie:
                        raise ValueError("Вставьте полный Cookie: имя=значение; имя=значение")
                    values["vk_cookie"] = cookie
                s.set_settings(values)
                # A failed provider is cooled down only for the previous
                # configuration.  Clear that state when credentials, model,
                # order, or endpoint settings change so a repaired provider
                # can be used immediately instead of waiting 15 minutes.
                if any(key in values for key in (
                        "ai_provider", "ai_provider_order", "ai_enabled_providers",
                        "chatgpt_model", "openai_model", "openai_api_key",
                        "ollama_url", "ollama_model")):
                    for provider in ("chatgpt", "openai", "local"):
                        self.server.worker.model_mark_ready(provider)
                    # The semantic Book of Offenses also has a short local
                    # backoff after a provider error.  A saved replacement
                    # model is an explicit recovery signal, so retry it now.
                    self.server.worker.semantic_retry_at = 0
                self.respond(200, {"ok": True})
            elif path == "/api/weather/test":
                from .weather import CITY, answer as weather_answer
                city = str(data.get("city") or "Кинешма").strip()
                if not CITY.fullmatch(city) or len(city.split()) > 4:
                    raise ValueError("Укажите название города словами")
                if s.setting("weather_enabled", DEFAULTS["weather_enabled"]) != "1":
                    self.respond(200, {"enabled": False, "message": "Погода выключена. Включите её в настройках ответов и сохраните настройки."})
                else:
                    forecast = weather_answer(s, request_json, "weather-test", "weather-test", "Какая погода в городе " + city + "?", diagnostic=True)
                    self.respond(200, {"enabled": True, "message": forecast})
            elif path == "/api/telegram/template":
                self.respond(200, {"id": s.save_telegram_template(data)})
            elif path == "/api/telegram/template/delete":
                s.delete_telegram_template(data.get("id"))
                self.respond(200, {"ok": True})
            elif path == "/api/telegram/sticker-set":
                source = str(data.get("name") or "").strip()
                name = source.rsplit("/", 1)[-1].split("?", 1)[0]
                if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", name):
                    raise ValueError("Укажите ссылку t.me/addstickers/название или имя набора")
                if not s.setting("telegram_token"):
                    raise ValueError("Сохраните токен Telegram")
                result = tg_call(s.setting("telegram_token"), "getStickerSet", {"name": name})
                self.respond(200, {"title": result.get("title"), "stickers": [{"file_id": item["file_id"], "emoji": item.get("emoji", ""), "format": "video" if item.get("is_video") else "animated" if item.get("is_animated") else "static"} for item in result.get("stickers", [])[:200]]})
            elif path == "/api/clear-secret":
                if data.get("key") not in SECRETS:
                    raise ValueError("Неизвестный ключ")
                s.set_settings({data["key"]: ""})
                self.respond(200, {"ok": True})
            elif path == "/api/chatgpt/connect":
                self.respond(200, self.server.worker.chatgpt.begin(str(data.get("account_id", ""))))
            elif path == "/api/chatgpt/complete-url":
                self.respond(200, self.server.worker.chatgpt.complete_from_url(data.get("url")))
            elif path == "/api/telegram/diagnose":
                self.respond(200, self.server.worker.telegram_diagnostics(str(data.get("chat_id", "")).strip()))
            elif path == "/api/chatgpt/cancel":
                self.server.worker.chatgpt.cancel()
                self.respond(200, {"ok": True})
            elif path == "/api/chatgpt/select":
                self.server.worker.chatgpt.select(str(data.get("account_id", "")))
                s.set_settings({"chatgpt_model": ""})
                self.respond(200, {"ok": True})
            elif path == "/api/chatgpt/models":
                self.respond(200, {"models": self.server.worker.chatgpt.models()})
            elif path == "/api/ai/models":
                from .ai import openai_json, API
                token = s.setting("openai_api_key")
                if not token:
                    raise ValueError("Сначала сохраните API-ключ OpenAI")
                listing = openai_json(API + "/models", token=token, timeout=20)
                models = sorted([{"id": str(item.get("id"))} for item in listing.get("data", [])
                                 if re.search(r"^(?:gpt|o[134]|chatgpt)", str(item.get("id", "")), re.I)], key=lambda item: item["id"])
                self.respond(200, {"models": models[:200]})
            elif path == "/api/ai/test":
                from .ai import response_text
                model = str(data.get("model") or s.setting("openai_model", DEFAULTS["openai_model"])).strip()
                token = s.setting("openai_api_key")
                if not token:
                    raise ValueError("Сначала сохраните API-ключ OpenAI")
                result = response_text(token, model, "Ответь одним словом: ок", [{"role": "user", "content": "Проверка подключения."}], max_tokens=8)
                self.respond(200, {"ok": True, "model": model, "message": "OpenAI API доступен для модели " + model})
            elif path == "/api/image/test":
                if s.setting("image_enabled", "0") != "1":
                    raise ValueError("Сначала включите генерацию изображений и сохраните настройки")
                token, key, admin = s.setting("telegram_token"), s.setting("openai_api_key"), s.setting("admin_user_id")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                if not key:
                    raise ValueError("Сначала сохраните API-ключ OpenAI")
                if not re.fullmatch(r"[1-9]\d{0,19}", admin):
                    raise ValueError("Укажите личный Telegram ID администратора и напишите боту /start")
                prompt = str(data.get("prompt") or "Тестовое изображение для проверки доставки RepostBot").strip()[:1200]
                if not prompt:
                    raise ValueError("Укажите описание тестового изображения")
                # Validate the destination before spending on image
                # generation. Telegram usually returns a useful chat
                # diagnostic from getChat even when a multipart upload gives
                # an empty HTTP 400 response.
                tg_call(token, "getChat", {"chat_id": int(admin)}, timeout=15)
                tg_call(token, "sendMessage", {"chat_id": int(admin), "text": "RepostBot: проверка отправки изображения"}, timeout=15)
                message = {"chat": {"id": int(admin), "type": "private"}, "message_id": None}
                self.server.worker.reply_image(token, message, prompt, persist=False, raise_errors=True)
                self.respond(200, {"ok": True, "message": "Тестовое изображение отправлено в личный чат администратора"})
            elif path == "/api/image/delivery-test":
                token, admin = s.setting("telegram_token"), s.setting("admin_user_id")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                if not re.fullmatch(r"[1-9]\d{0,19}", admin):
                    raise ValueError("Укажите личный Telegram ID администратора")
                tg_call(token, "getChat", {"chat_id": int(admin)}, timeout=15)
                tg_call(token, "sendMessage", {"chat_id": int(admin), "text": "RepostBot: проверка отправки изображения"}, timeout=15)
                image_dir = s.data_dir / "generated-images"
                image_dir.mkdir(exist_ok=True)
                image_path = image_dir / "telegram-delivery-test.png"
                image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
                try:
                    self.server.worker.send_generated_image(token, int(admin), None, image_path)
                finally:
                    image_path.unlink(missing_ok=True)
                self.respond(200, {"ok": True, "message": "Тестовый PNG доставлен в личный чат администратора"})
            elif path == "/api/chatgpt/logout":
                self.respond(200, self.server.worker.chatgpt.logout(str(data.get("account_id", ""))))
            elif path == "/api/fact":
                self.respond(200, {"id": s.save_fact(data)})
            elif path == "/api/trigger":
                self.respond(200, {"id": s.save_trigger(data)})
            elif path == "/api/twitch/config":
                self.respond(200, self.server.worker.twitch.configure(data))
            elif path == "/api/twitch/begin":
                self.respond(200, {"url": self.server.worker.twitch.begin(str(data.get("role") or ""))})
            elif path == "/api/twitch/complete-url":
                self.respond(200, self.server.worker.twitch.complete_from_url(data.get("url")))
            elif path == "/api/twitch/disconnect":
                self.server.worker.twitch.disconnect(str(data.get("role") or ""))
                self.respond(200, {"ok": True})
            elif path == "/api/twitch/send":
                self.respond(200, {"message_id": self.server.worker.twitch.send(str(data.get("message") or "")[:450])})
            elif path == "/api/moderation/settings":
                self.respond(200, {"settings": self.server.worker.moderation.save_settings(data)})
            elif path == "/api/moderation/scan-chat":
                token = s.setting("telegram_token")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                self.respond(200, self.server.worker.moderation.scan_chat(token, data.get("chat"), data.get("limit", 500)))
            elif path == "/api/conversation/settings":
                presence_data = {str(key)[len("presence_"):]: value for key, value in data.items() if str(key).startswith("presence_")}
                self.respond(200, {"settings": self.server.worker.conversation.configure(data), "presence": self.server.worker.presence.configure(presence_data) if presence_data else self.server.worker.presence.settings()})
            elif path == '/api/social/title':
                self.respond(200,self.server.worker.participants.save_title(data,data.get('kind','honor')))
            elif path == '/api/social/link':
                from .account_links import manual
                self.respond(200,manual(self.server.worker.community,data.get('twitch_id'),data.get('telegram_id')))
            elif path == '/api/repost-stats/genre':
                self.respond(200,self.server.worker.repost_stats.save(data))
            elif path == '/api/repost-stats/main':
                chat=str(data.get('chat',''))
                if chat and chat not in self.server.worker.repost_stats.chats():raise ValueError('Выберите канал из журнала.')
                s.set_settings({'repost_stats_main_chat':chat});self.respond(200,{'ok':True})
            elif path == '/api/social/title/delete':
                self.respond(200,self.server.worker.participants.delete_title(data))
            elif path == "/api/social/settings":
                self.respond(200, {"settings": self.server.worker.social.configure(data)})
            elif path == "/api/social/role":
                self.respond(200, self.server.worker.social.save_custom_role(data))
            elif path == "/api/social/role/delete":
                self.respond(200, self.server.worker.social.delete_custom_role(data.get("key")))
            elif path == '/api/models/settings':
                self.respond(200, {'settings': self.server.worker.models.configure(data)})
            elif path == '/api/models/provider':
                self.respond(200, self.server.worker.models.save(data))
            elif path == '/api/models/delete':
                self.respond(200, self.server.worker.models.delete(str(data.get('id') or '')))
            elif path == '/api/models/list':
                self.respond(200, {'models': self.server.worker.models.model_list(str(data.get('id') or ''))})
            elif path == '/api/models/check':
                self.respond(202, {'started': self.server.worker.models.check(str(data.get('id') or ''))})
            elif path == '/api/models/evaluate':
                self.respond(202, self.server.worker.models.evaluate(data))
            elif path == '/api/models/grade':
                self.respond(200, self.server.worker.models.grade(data))
            elif path == "/api/social/permanent-role":
                self.respond(200, self.server.worker.social.save_permanent_role(data))
            elif path == "/api/social/permanent-role/delete":
                self.respond(200, self.server.worker.social.delete_permanent_role(data.get("chat"), data.get("person"), data.get("role_key")))
            elif path == "/api/stickers/add":
                key, file_id, description = parse_sticker_line("|".join(str(data.get(k) or "").strip() for k in ("key", "file_id", "description")))
                with s.lock:
                    if len(s.setting("reply_stickers")) + len(file_id) + len(description) + 26 > 100_000:
                        raise ValueError("Список стикеров слишком длинный")
                    keys, _ = s.stickers.upsert_many([{"file_id": file_id, "description": description}], preferred_keys=[key], origin="manual")
                    s.set_settings({"reply_stickers_enabled": "1"})
                    self.server.worker._sticker_catalog_source = None
                self.respond(200, {"key": keys[0]})
            elif path == "/api/stickers/add-lines":
                lines = str(data.get("lines") or "")
                if not lines.strip() or len(lines) > 100_000:
                    raise ValueError("Введите строки стикеров, до 100 000 символов")
                parsed = [parse_sticker_line(line.strip()) for line in lines.splitlines() if line.strip()]
                with s.lock:
                    if len(s.setting("reply_stickers")) + len(lines) > 100_000:
                        raise ValueError("Список стикеров слишком длинный")
                    keys, added = s.stickers.upsert_many([{"file_id": file_id, "description": description} for key, file_id, description in parsed], preferred_keys=[key for key, _, _ in parsed], origin="manual")
                    s.set_settings({"reply_stickers_enabled": "1"})
                    self.server.worker._sticker_catalog_source = None
                self.respond(200, {"added": added, "keys": keys})
            elif path == "/api/stickers/bot-permission":
                if type(data.get("blocked")) is not bool:
                    raise ValueError("Укажите параметр blocked: true или false")
                self.respond(200, self.server.worker.set_sticker_bot_blocked(data.get("key"), data["blocked"]))
            elif path == "/api/stickers/settings":
                values = {key: data[key] for key in ("reply_stickers_enabled", "reply_stickers", "reply_sticker_chance", "reply_sticker_default_key") if key in data}
                if "reply_stickers" in values:
                    lines = [line.strip() for line in str(values["reply_stickers"]).splitlines() if line.strip()]
                    normalized = []
                    if len("\n".join(lines)) > 100_000:
                        raise ValueError("Список стикеров слишком длинный")
                    for line in lines:
                        key, file_id, description = parse_sticker_line(line)
                        normalized.append("|".join(item for item in (key, file_id, description) if item))
                    values["reply_stickers"] = "\n".join(normalized)
                    origins = self.server.worker._sticker_json_map("reply_sticker_origins")
                    origins.update({parse_sticker_line(line)[0]: "manual" for line in normalized})
                    values["reply_sticker_origins"] = json.dumps(origins, ensure_ascii=False)
                    if values["reply_stickers"]:
                        values["reply_sticker_catalog_backup"] = values["reply_stickers"]
                if "reply_sticker_chance" in values and not 0 <= int(values["reply_sticker_chance"]) <= 100:
                    raise ValueError("Шанс стикера должен быть от 0 до 100")
                if values:
                    self.server.worker.store.set_settings(values)
                    self.server.worker._sticker_catalog_source = None
                self.respond(200, {"stickers": self.server.worker.sticker_panel()})
            elif path == "/api/stickers/import-pack":
                token = s.setting("telegram_token")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                self.respond(200, self.server.worker.import_sticker_pack(token, data.get("pack")))
            elif path == "/api/stickers/analyze":
                token = s.setting("telegram_token")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                self.respond(200, self.server.worker.analyze_sticker(token, data.get("key")))
            elif path == "/api/stickers/analyze-bulk":
                token = s.setting("telegram_token")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                self.respond(200, self.server.worker.analyze_sticker_batch(token, data.get("keys") or []))
            elif path == "/api/stickers/analyze-status":
                self.respond(200, self.server.worker.sticker_analysis_status(data.get("job_id")))
            elif path == "/api/stickers/analyze-cancel":
                self.respond(200, self.server.worker.cancel_sticker_analysis(data.get("job_id")))
            elif path == "/api/stickers/preview":
                token = s.setting("telegram_token")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                key = str(data.get("key") or "").strip().casefold()
                entry = self.server.worker.sticker_entries(include_disabled=True).get(key)
                if not entry:
                    raise ValueError("Стикер с таким ключом не найден")
                self.respond(200, {"key": key, "image": self.server.worker.sticker_preview(token, key, refresh=data.get("refresh") is True)})
            elif path == "/api/stickers/semantic":
                self.respond(200, {"stickers": self.server.worker.save_sticker_semantics(data.get("key"), data.get("contexts"))})
            elif path == "/api/stickers/toggle":
                self.respond(200, {"stickers": self.server.worker.set_sticker_enabled(data.get("key"), data.get("enabled") is True)})
            elif path == "/api/stickers/delete":
                self.respond(200, {"stickers": self.server.worker.delete_sticker(data.get("key"))})
            elif path == "/api/conversation/glitch-test":
                token = s.setting("telegram_token")
                if not token:
                    raise ValueError("Сначала сохраните токен Telegram")
                conversation = self.server.worker.conversation
                config = conversation.settings()
                chat = str(data.get("chat") or "").strip()
                if data.get('destination')=='scout_test':
                    chat=str(self.server.worker.scout.destination('test_chat').get('chat',''))
                    if not chat:raise ValueError('Сначала укажите тестовый чат.')
                style=str(data.get('style') or '')
                from .glitch_profile import NAMES
                if style and style not in NAMES:raise ValueError('Выберите известный стиль сбоя.')
                allowed_chats = [item.strip() for item in config["glitch_chats"].splitlines() if item.strip()]
                allowed_chats+= [str(row['id']) for row in s.chats() if row.get('type') in ('group','supergroup')]
                allowed_chats+= [str(target['chat']) for target in (self.server.worker.scout.destination(),self.server.worker.scout.destination('test_chat')) if target.get('chat')]
                allowed_chats=list(dict.fromkeys(allowed_chats))
                if not chat:
                    chat = allowed_chats[0] if len(allowed_chats) == 1 else ""
                if not re.fullmatch(r"-\d{1,20}", chat):
                    raise ValueError("Укажите отрицательный ID группы Telegram для теста")
                if chat not in allowed_chats:
                    raise ValueError("Сначала подключите группу к боту или укажите её как тестовую группу")
                if self.server.worker.ai_provider() == "faq":
                    raise ValueError("Для проверки сбоя подключите модель ответов, а не режим без модели")
                question = str(data.get("text") or "Проверка сбоя роли: ответь в выбранном стиле.").strip()[:1500]
                if not question:
                    raise ValueError("Введите тестовую реплику")
                person = str(data.get("person") or "").strip()
                if person and not re.fullmatch(r"[1-9]\d{0,19}", person):
                    raise ValueError("ID участника должен быть положительным числом или пустым")
                source = int(time.time() * 1000)
                if data.get('replace') is True:conversation.cancel_all_glitches(cooldown=False)
                session = conversation.glitch_prepare("telegram", chat, person, source, force=True, target_name="Панель",style_override=style,manual=True)
                if not session:
                    raise ValueError("Не удалось запустить тестовый сбой в этой группе")
                if session.get("phase") in ("active", "sending"):
                    raise ValueError("В этой группе уже идёт сбой; дождитесь сообщения восстановления")
                try:
                    answer = self.server.worker.answer(question, chat_id=chat, person=person, username="Панель", panel_test=True,
                                                       force_glitch=True, source_message_id=source)
                    if not answer:
                        raise ValueError("Модель не вернула тестовый ответ")
                    pending = conversation.glitch_active(chat)
                    if not pending or pending.get("id") != session.get("id"):
                        raise ValueError("Тестовая реплика не получила режим сбоя; проверьте подключённую модель")
                    sent = self.server.worker.telegram_delivery(token, "sendMessage", {"chat_id": chat, "text": str(answer)[:3500]})
                    if not isinstance(sent, dict) or not sent.get("message_id"):
                        raise ValueError("Telegram не вернул ID тестового сообщения")
                    conversation.glitch_commit(token, chat, source, sent["message_id"])
                    self.server.worker.send_glitch_quest(token, chat)
                except Exception:
                    conversation.glitch_cancel(chat, source)
                    raise
                self.server.store.event("info", "Панель: тест режима сбоя отправлен в " + chat)
                self.respond(200, {"ok": True, "chat": chat, "style": session.get("style", ""),
                                   "quest": bool(session.get("quest")),
                                   "message": "Тест сбоя отправлен в группу. Скрытый квест также поставлен в очередь." if session.get("quest") else "Тест сбоя отправлен в группу. Сообщение восстановления придёт по заданной задержке."})
            elif path == '/api/conversation/glitch-profile-sync':
                self.respond(200,self.server.worker.glitch_profile.tick())
            elif path == '/api/conversation/glitch-profile-restore':
                self.server.worker.conversation.configure({'glitch_profile_enabled':False})
                self.respond(200,self.server.worker.glitch_profile.tick())
            elif path == "/api/conversation/glitch-cancel":
                self.server.worker.conversation.cancel_all_glitches()
                self.respond(200, {"message": "Все активные сбои отключены; базовый промпт восстановлен."})
            elif path == '/api/speech/settings':
                self.respond(200, {'settings': self.server.worker.speech.configure(data)})
            elif path == '/api/speech/prepare':
                self.respond(200, self.server.worker.speech.prepare())
            elif path in ('/api/speech/test', '/api/telegram/voice-preview'):
                self.respond(200, self.server.worker.speech.test(data))
            elif path == "/api/quests/settings":
                self.respond(200, {"settings": self.server.worker.quests.configure(data)})
            elif path == "/api/telegram/service-messages/settings":
                self.respond(200, {"settings": self.server.worker.service_messages.configure(data)})
            elif path == "/api/telegram/service-messages/commands":
                self.respond(200, {"message": self.server.worker.service_messages.publish_commands(s.setting("telegram_token"))})
            elif path == "/api/quests/rule":
                self.respond(200, {"rules": self.server.worker.quests.save_rule(data)})
            elif path == "/api/quests/auto-rule":
                self.respond(200, {"auto_rules": self.server.worker.quests.auto.save_rule(data)})
            elif path == "/api/quests/cancel":
                self.respond(200, self.server.worker.quests.cancel(data))
            elif path == "/api/quests/assign":
                platform = str(data.get("platform") or "telegram")
                profile = self.server.worker.community.resolve_person(platform, data.get("nick"), self.server.worker.twitch)
                run = self.server.worker.quests.assign_manual(platform, str(data.get("chat") or ""), profile["user_id"], profile["name"], data.get("rule_id"), s.setting("telegram_token"))
                self.respond(200, {"run": run})
            elif path == "/api/conversation/preview":
                platform = str(data.get("platform") or "telegram")
                chat = str(data.get("chat") or "").strip()
                text = str(data.get("text") or "").strip()
                name = str(data.get("name") or "Участник").strip()[:100]
                if platform not in ("telegram", "twitch") or not 1 <= len(text) <= 1500:
                    raise ValueError("Выберите площадку и введите сообщение до 1500 символов")
                if platform == "twitch":
                    chat = "twitch:" + s.setting("twitch_channel", "preview")
                elif chat and not re.fullmatch(r"-?\d{1,20}", chat):
                    raise ValueError("ID чата должен быть числом или пустым")
                with s.lock:
                    row = s.db.execute("SELECT p.user_id FROM community_participants p LEFT JOIN community_usernames u ON u.platform=p.platform AND u.user_id=p.user_id WHERE p.platform=? AND (LOWER(u.username)=? OR LOWER(p.name)=?) LIMIT 1", (platform, name.removeprefix("@").casefold(), name.casefold())).fetchone()
                decision = self.server.worker.conversation.generate(platform, chat, row[0] if row else "", name, text, preview=True)
                self.respond(200, {"decision": decision})
            elif path == "/api/moderation/variant":
                self.respond(200, self.server.worker.community.add_variant(data.get("group_id"), data.get("phrase")))
            elif path == "/api/moderation/variant/delete":
                self.server.worker.community.delete_variant(data.get("id"))
                self.respond(200, {"ok": True})
            elif path == "/api/moderation/white":
                self.respond(200, self.server.worker.community.set_white(data.get("word"), data.get("enabled") is True))
            elif path == "/api/community/settings":
                enabled = data.get("enabled") is True
                days = int(data.get("retention_days", s.setting("twitch_retention_days", "-1")))
                if days != -1 and not 30 <= days <= 730:
                    raise ValueError("Срок хранения Twitch: −1 (бессрочно) или от 30 до 730 дней")
                greeting = int(data.get("greeting_min", 40))
                if not 1 <= greeting <= 100:
                    raise ValueError("Порог приветствия должен быть от 1 до 100")
                strong, refuse = int(data.get("grudge_strong_at", -21)), int(data.get("grudge_refuse_at", -50))
                if not -99 <= strong <= -2 or not -100 <= refuse < strong:
                    raise ValueError("Порог сильной обиды: −99…−2; порог отказа должен быть ниже")
                hint_chance, strong_chance = int(data.get("hint_answer_chance", 50)), int(data.get("strong_answer_chance", 10))
                if not 0 <= hint_chance <= 100 or not 0 <= strong_chance <= 100:
                    raise ValueError("Шанс ответа при лёгкой и сильной обиде должен быть от 0 до 100%")
                quiet_max = int(data.get("quiet_penalty_max", s.setting("community_quiet_penalty_max", "10")))
                if not 0 <= quiet_max <= 20:
                    raise ValueError("Порог тихих нарушений должен быть от 0 до 20; 0 отключает тихий режим")
                transition_pause = int(data.get('transition_cooldown_minutes', s.setting('community_transition_cooldown_minutes', '5')))
                if not 0 <= transition_pause <= 1440:
                    raise ValueError('Пауза уведомлений о ступени обиды: от 0 до 1440 минут')
                honor_message = str(data.get("honor_message", s.setting("community_honor_message", "Твой вклад в историю сообщества неизмерим."))).strip()
                if not 1 <= len(honor_message) <= 300 or "\n" in honor_message or "\r" in honor_message:
                    raise ValueError("Общая фраза для почётных участников должна быть одной строкой до 300 символов")
                rank_phrases = {"community_rank_" + level: str(data.get("rank_" + level, s.setting("community_rank_" + level, default))).strip()
                                for level, default in RANK_PHRASES.items()}
                if any(not 1 <= len(value) <= 100 or "\n" in value or "\r" in value for value in rank_phrases.values()):
                    raise ValueError("Фраза ступени Книги должна быть одной строкой до 100 символов")
                stop_debt = int(data.get("activity_stop_debt", 50))
                if not 1 <= stop_debt <= 10000:
                    raise ValueError("Порог остановки активности должен быть от 1 до 10000")
                cap, notify_every = float(data.get("activity_cap", 20)), float(data.get("activity_notify_every", 5))
                if (not all(math.isfinite(x) and x * 2 == int(x * 2) for x in (cap, notify_every))
                        or not 0 <= cap <= 100 or not 0.5 <= notify_every <= 100):
                    raise ValueError("Лимит активности: 0–100; шаг уведомлений: 0,5–100, оба с шагом 0,5")
                rates = {level: float(data.get("activity_rate_" + level, default)) for level, default in (("normal", 0.5), ("hint", 0.3), ("strong", 0.2), ("refuse", 0.1))}
                twitch_rates = {level: float(data.get("activity_rate_twitch_" + level, rate)) for level, rate in rates.items()}
                if any(not math.isfinite(value) or not 0 <= value <= 10 or round(value * 10) != value * 10 for value in (*rates.values(), *twitch_rates.values())):
                    raise ValueError("Начисление за сообщение должно быть от 0 до 10 с шагом 0,1")
                community = self.server.worker.community
                texts = {community.template_key(kind): community.validate_templates(data.get(("grudge_" if kind in ("hint", "strong", "refuse", "help") else "message_") + kind, community.template_text(kind)))
                         for kind in ("hint", "strong", "refuse", "help", "greeting", "banword", "severe", "activity", "reward", "reply_grudge", "reply_roleplay", 'transition_hint', 'transition_strong', 'transition_refuse')}
                mentions = {"community_severe_mentions_" + platform: community.validate_mentions(data.get("severe_mentions_" + platform, s.setting("community_severe_mentions_" + platform, ""))) for platform in ("twitch", "telegram")}
                legacy_scan = {"community_" + name: "1" if data[name] is True else "0"
                               for name in ("media_scan_enabled", "ai_scan_enabled") if name in data}
                s.set_settings({"community_enabled": "1" if enabled else "0", **legacy_scan, "twitch_retention_days": str(days), "community_greeting_min": str(greeting),
                                "community_grudge_strong_at": str(strong), "community_grudge_refuse_at": str(refuse),
                                "community_hint_answer_chance": str(hint_chance), "community_strong_answer_chance": str(strong_chance),
                                "community_quiet_penalty_max": str(quiet_max),
                                'community_transition_cooldown_minutes': str(transition_pause),
                                'community_notify_transition': '1' if data.get('notify_transition', s.setting('community_notify_transition', '1') == '1') is True else '0',
                                **{'community_notify_transition_' + platform: '1' if data.get('notify_transition_' + platform, s.setting('community_notify_transition_' + platform, '1') == '1') is True else '0' for platform in ('telegram', 'twitch')},
                                **{'community_transition_' + level: '1' if data.get('transition_' + level, s.setting('community_transition_' + level, '0' if level == 'hint' else '1') == '1') is True else '0' for level in ('hint', 'strong', 'refuse')},
                                **{"community_reply_receipt_" + platform: "1" if data.get("reply_receipt_" + platform, s.setting("community_reply_receipt_" + platform, "1") == "1") is True else "0" for platform in ("telegram", "twitch")},
                                "community_honor_message": honor_message, **rank_phrases,
                                "community_activity_stop": "1" if data.get("activity_stop") is True else "0", "community_activity_stop_debt": str(stop_debt),
                                "community_activity_cap": str(cap), "community_activity_notify_every": str(notify_every),
                                **{"community_activity_rate_" + level: str(value) for level, value in rates.items()},
                                **{"community_activity_rate_twitch_" + level: str(value) for level, value in twitch_rates.items()},
                                **{"community_" + key: "1" if data.get(key) is True else "0" for key in ("notify_greeting", "notify_banword", "notify_severe", "notify_activity", "notify_reward")},
                                **{"community_notify_" + kind + "_" + platform: "1" if data.get("notify_" + kind + "_" + platform, True) is True else "0" for kind in ("greeting", "banword", "severe", "activity", "reward") for platform in ("twitch", "telegram")},
                                **{"community_ai_" + level: "1" if data.get("ai_" + level) is True else "0" for level in ("hint", "strong", "refuse", "help")},
                                **{"community_ai_" + level + "_fallback": "1" if level in ("refuse", "help") or data.get("ai_" + level + "_fallback", True) is True else "0" for level in ("hint", "strong", "refuse", "help")}, **texts, **mentions})
                self.respond(200, {"ok": True})
            elif path == "/api/community/term":
                self.respond(200, {"id": self.server.worker.community.save_term(data)})
            elif path == "/api/community/term/toggle":
                self.respond(200, self.server.worker.community.set_term_enabled(data.get("id"), data.get("enabled")))
            elif path == "/api/community/terms/import":
                self.respond(200, self.server.worker.community.import_terms(data))
            elif path == "/api/community/terms/import-pack":
                preview = data.get("preview") is True
                result = self.server.worker.community.import_terms_pack(data.get("pack"), preview, data.get("overwrite") is True)
                if not preview and data.get("enable_book") is True:
                    s.set_settings({"community_enabled": "1"})
                result["book_enabled"] = s.setting("community_enabled", "0") == "1"
                self.respond(200, result)
            elif path == "/api/community/terms/check":
                self.respond(200, self.server.worker.community.check_terms(str(data.get("platform") or ""), data.get("message")))
            elif path == "/api/community/scan-groups":
                self.respond(200, self.server.worker.community.scan_groups(s.setting("telegram_token"), data.get("group_id"), self.server.worker.bot_id))
            elif path == "/api/community/mtproto":
                self.respond(200, self.server.worker.community.configure_mtproto(data))
            elif path == "/api/community/mtproto/disconnect":
                self.server.worker.community.disconnect_mtproto()
                self.respond(200, {"ok": True})
            elif path == "/api/community/term/delete":
                self.server.worker.community.delete_term(data["id"])
                self.respond(200, {"ok": True})
            elif path == "/api/community/reward-rule":
                self.respond(200, {"id": self.server.worker.community.save_reward_rule(data)})
            elif path == "/api/community/reward-rule/delete":
                self.server.worker.community.delete_reward_rule(data["id"])
                self.respond(200, {"ok": True})
            elif path == "/api/community/adjust":
                if data.get("action") not in ("forgive", "grudge", "set"):
                    raise ValueError("В ручной правке можно добавить, уменьшить обиду или установить её уровень")
                if not str(data.get("user_id") or "").isdigit():
                    person = self.server.worker.community.resolve_person(str(data.get("platform") or ""), data.get("user_id") or data.get("nick"), self.server.worker.twitch)
                    data["user_id"], data["nick"] = person["user_id"], data.get("nick") or person["name"]
                self.respond(200, self.server.worker.community.adjust(str(data.get("platform") or ""), str(data.get("user_id") or ""), str(data.get("nick") or ""), str(data.get("action") or ""), data.get("amount", 1), str(data.get("reason") or "Ручная правка")))
            elif path == "/api/community/protected":
                if not str(data.get("user_id") or "").isdigit():
                    self.respond(200, self.server.worker.community.save_by_nickname("protected", data))
                else:
                    self.respond(200, self.server.worker.community.set_protected(str(data.get("platform") or ""), str(data.get("user_id") or ""), str(data.get("nick") or ""), str(data.get("category") or ""),
                                                                                     data.get("title"), data.get("message"), data.get("fixed_score", 100)))
            elif path == "/api/community/protected/delete":
                self.server.worker.community.remove_protected(data.get("platform"), data.get("user_id"))
                self.respond(200, {"ok": True})
            elif path == "/api/community/honor":
                if not str(data.get("user_id") or "").isdigit():
                    self.respond(200, self.server.worker.community.save_by_nickname("honor", data))
                else:
                    self.respond(200, {"id": self.server.worker.community.save_honor(data)})
            elif path == "/api/community/person":
                entry = {"platform": data.get("platform"), "user_id": data.get("nick"), "category": "member", "pending_id": data.get("pending_id")}
                self.respond(200, self.server.worker.community.save_by_nickname("person", entry))
            elif path == "/api/community/pending/delete":
                self.server.worker.community.delete_pending(data.get("id"))
                self.respond(200, {"ok": True})
            elif path == "/api/community/resolve":
                person = self.server.worker.community.resolve_person(str(data.get("platform") or ""), str(data.get("query") or ""), self.server.worker.twitch)
                self.respond(200, {"platform": person["platform"], "user_id": person["user_id"], "name": person["name"], "identities": person["identities"], "linked": person["linked"]})
            elif path == "/api/community/honor/delete":
                self.server.worker.community.delete_honor(data.get("id"))
                self.respond(200, {"ok": True})
            elif path == "/api/community/unlink":
                self.server.worker.community.unlink(str(data.get("twitch_id") or ""))
                self.respond(200, {"ok": True})
            elif path == "/api/triggers/import":
                self.respond(200, import_pack(s, data.get("pack"), overwrite=data.get("overwrite") is True, preview=data.get("preview") is True))
            elif path == "/api/trigger/delete":
                s.delete_trigger(data["id"])
                self.respond(200, {"ok": True})
            elif path == "/api/fact/delete":
                s.delete_fact(data["id"])
                self.respond(200, {"ok": True})
            elif path == "/api/profile":
                s.save_profile(data)
                self.respond(200, {"ok": True})
            elif path == "/api/profile/delete":
                s.delete_profile(data["chat"])
                self.respond(200, {"ok": True})
            elif path == "/api/memory/clear":
                s.clear_ai_context()
                self.respond(200, {"ok": True})
            elif path == "/api/memory/correction/delete":
                s.delete_correction(data["id"])
                self.respond(200, {"ok": True})
            elif path == "/api/local/setup":
                if self.server.windows_launcher:
                    import subprocess
                    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT.parent / "start_windows.ps1"), "-SetupOllamaOnly"], cwd=ROOT.parent)
                    self.respond(202, {"ok": True, "state": "pulling"})
                elif getattr(self.server, "container_launcher", False):
                    model = str(data.get("model") or s.setting("ollama_model", DEFAULTS["ollama_model"])).strip()
                    base = str(data.get("url") or s.setting("ollama_url", DEFAULTS["ollama_url"])).strip().rstrip("/")
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model) or ".." in model:
                        raise ValueError("Некорректное имя модели Ollama")
                    parsed = urlparse(base)
                    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                        raise ValueError("Неверный адрес Ollama")
                    s.set_settings({"ollama_url": base, "ollama_model": model})
                    setup = getattr(self.server, "local_setup", None)
                    if setup is None:
                        setup = self.server.local_setup = {"state": "idle", "error": "", "progress": "", "model": "", "lock": threading.Lock()}
                    lock = setup.setdefault("lock", threading.Lock())
                    with lock:
                        if setup.get("state") == "pulling":
                            self.respond(202, {"ok": True, "state": "pulling", "model": setup.get("model") or model})
                            return
                        setup.update({"state": "pulling", "error": "", "progress": "Запрашиваю модель…", "model": model})
                    def pull():
                        try:
                            request_json(base + "/api/pull", {"model": model, "stream": False}, timeout=3600)
                            setup.update({"state": "ready", "error": "", "progress": "Модель загружена", "model": model})
                        except Exception as exc:
                            detail = str(exc)[:260]
                            if "Name or service not known" in detail or "nodename nor servname" in detail:
                                detail = "Не найден сервис Ollama по адресу " + base + ". Запустите Compose целиком или укажите адрес доступного сервиса Ollama."
                            setup.update({"state": "error", "error": detail, "progress": "", "model": model})
                    threading.Thread(target=pull, daemon=True, name="ollama-model-pull").start()
                    self.respond(202, {"ok": True, "state": "pulling", "model": model})
                else:
                    raise ValueError("Для контейнера добавьте сервис Ollama в Compose или установите Ollama на компьютере")
            elif path == "/api/route":
                self.server.worker.check_telegram_targets(data.get("targets", []))
                self.respond(200, {"id": s.save_route(data)})
            elif path == "/api/route-reaction/series/rebuild":
                self.respond(202, self.server.worker.series.request_rebuild())
            elif path == "/api/route-reaction/game":
                self.respond(200, self.server.worker.series.save_game(data))
            elif path == "/api/route-reaction/game/check":
                self.respond(202, self.server.worker.series.check_facts(str(data.get("game_key") or "")))
            elif path == "/api/route-reaction/settings":
                enabled = data.get("enabled") is True
                chance = int(data.get("chance", 20))
                hours = int(data.get("repeat_hours", 24))
                prompt = str(data.get("prompt", "")).strip()
                chats = list(dict.fromkeys(part.strip() for part in re.split(r"[,\n]", str(data.get("chats", ""))) if part.strip()))
                if not 0 <= chance <= 100 or not 0 <= hours <= 168 or len(prompt) > 2000:
                    raise ValueError("Проверьте шанс, время повтора и длину промпта")
                if any(not re.fullmatch(r"-\d{1,20}", chat) for chat in chats) or enabled and not chats:
                    raise ValueError("Для автоматических реакций нужны отрицательные ID групп Telegram, например -1001234567890. Положительный личный ID можно указать только в предпросмотре.")
                s.set_settings({"route_reaction_" + key: str(value) for key, value in {"enabled": "1" if enabled else "0", "chance": chance, "repeat_hours": hours, "prompt": prompt, "chats": "\n".join(chats)}.items()})
                if "game_facts" in data:
                    s.set_settings({"route_reaction_game_facts":"1" if data["game_facts"] is True else "0"})
                self.server.worker.series.event("settings_changed",detail=f"Реакции: {enabled}; шанс: {chance}%; окно повторов: {hours} ч; справка: {s.setting('route_reaction_game_facts','1')}")
                self.respond(200, {"ok": True})
            elif path == "/api/stream-pin/settings":
                chats = list(dict.fromkeys(part.strip() for part in re.split(r"[,\n\s]+", str(data.get("chats", ""))) if part.strip()))
                if any(not re.fullmatch(r"-\d{1,20}", chat) for chat in chats):
                    raise ValueError("Группы для закрепления указываются отрицательными Telegram ID, например -1001234567890")
                s.set_settings({"stream_pin_enabled": "1" if data.get("enabled") is True else "0",
                                "stream_pin_delete_service": "1" if data.get("delete_service") is True else "0",
                                "stream_pin_chats": "\n".join(chats)})
                self.respond(200, {"ok": True})
            elif path == "/api/stream-pin/check-today":
                if s.setting("stream_pin_enabled", DEFAULTS["stream_pin_enabled"]) != "1":
                    raise ValueError("Сначала включите автоматическое закрепление стримов")
                results = []
                for route in s.routes(enabled=True):
                    if route["source_type"] not in ("vk", "vk_public"):
                        continue
                    try:
                        results.append({"route": route["source"], **self.server.worker.test_vk_stream_pin(route["id"])})
                    except Exception as exc:
                        results.append({"route": route["source"], "sent": [], "pinned": [], "failed": [{"target": "", "error": str(exc)[:300]}]})
                pinned = sum(len(item.get("pinned", [])) for item in results)
                failed = sum(len(item.get("failed", [])) for item in results)
                self.respond(200, {"results": results, "message": f"Проверка завершена: закреплено {pinned}, ошибок {failed}"})
            elif path == "/api/route-reaction/preview":
                route = next((item for item in s.routes() if item["id"] == int(data["route_id"])), None) if data.get("route_id") else None
                sample = str(data.get("sample_text", "")).strip()[:1600]
                target = str(data.get("target", "")).strip()
                prompt = str(data.get("prompt", "")).strip()
                if target and not re.fullmatch(r"-?\d{1,20}", target):
                    raise ValueError("ID чата для контекста должен состоять из цифр; его можно оставить пустым")
                if len(prompt) > 2000:
                    raise ValueError("Промпт слишком длинный")
                # A common panel mistake is pasting the style prompt into the
                # optional post sample. Prefer the selected route's actual post.
                if route and sample and sample == prompt:
                    sample = ""
                context = None
                if route and not sample and route["source_type"] == "youtube":
                    rows = s.youtube_route_posts(route["id"], 1)
                    if rows:
                        video = rows[0]
                        context = {"type": "youtube", "title": video["title"], "text": video.get("channel_title", ""), "url": video["source_url"], "published_at": video.get("published_at", 0)}
                elif route and not sample and route["source_type"] in ("vk", "vk_public"):
                    rows = s.route_posts(route["id"], 1)
                    if rows:
                        post = rows[0]
                        context = {"type": "vk", "title": "", "text": post.get("text", ""), "url": post.get("source_url", ""), "published_at": post.get("published_at", 0)}
                if sample:
                    context = {"type": route["source_type"] if route else "telegram", "title": "", "text": sample, "url": "", "published_at": 0, "_preview": True}
                if not context:
                    raise ValueError("Введите пример публикации или выберите маршрут с сохранёнными постами")
                if route:
                    context = {**context, "_series_route_id": route['id']}
                answer = self.server.worker.generate_route_reaction(context, target, prompt=prompt or None)
                self.respond(200, {"message": answer if answer != "[[SKIP_REACTION]]" else "Похожую тему бот уже комментировал: реакция пропущена.", "skipped": answer == "[[SKIP_REACTION]]", "post": (context.get("title") or context.get("text") or "")[:160], "source": context.get("type", "")})
            elif path == "/api/route/format-preview":
                video = None
                if data.get("id"):
                    route = next((r for r in s.routes() if r["id"] == int(data["id"])), None)
                    if route and route["source_type"] == "youtube":
                        saved = s.youtube_route_posts(route["id"], 1)
                        video = saved[0] if saved else None
                if video is None:
                    video = {"video_id": "aaaaaaaaaaa", "title": "Пример названия видео", "channel_title": "Пример канала", "published_at": 1791504000, "source_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
                message, length = render_youtube_template(str(data.get("youtube_template") or ""), video)
                self.respond(200, {"html": message, "length": length, "photo_caption": length <= 1024})
            elif path == "/api/route/vk-format-preview":
                post = None
                if data.get("id"):
                    route = next((r for r in s.routes() if r["id"] == int(data["id"])), None)
                    if route and route["source_type"] in ("vk", "vk_public"):
                        saved = s.route_posts(route["id"], 1)
                        post = saved[0] if saved else None
                if post is None:
                    post = {"owner_id": -123, "id": 42, "text": "Пример записи VK", "date": 1791504000}
                message, length = render_vk_template(str(data.get("vk_template") or ""), post)
                self.respond(200, {"html": message, "length": length, "photo_caption": length <= 1024})
            elif path == "/api/route/parse":
                route_id = int(data.get("id"))
                route = next((item for item in s.routes() if item["id"] == route_id), None)
                if not route or route["source_type"] not in ("vk", "vk_public", "youtube"):
                    raise ValueError("Маршрут публикаций не найден")
                if route["source_type"] == "youtube":
                    videos = self.server.worker.poll_youtube_route(route, preview_only=True)
                    self.respond(200, {"posts": s.route_posts(route_id, 5), "found": len(videos)})
                    return
                if route["source_type"] == "vk" and not s.setting("vk_token"):
                    raise ValueError("Для режима VK API укажите токен VK")
                posts = self.server.worker.poll_vk_route(s.setting("vk_token"), route, preview_only=True)
                self.respond(200, {"posts": s.route_posts(route_id, 5), "found": len(posts)})
            elif path == "/api/route/test-latest":
                route_id = int(data.get("id"))
                route = next((item for item in s.routes() if item["id"] == route_id), None)
                self.respond(200, self.server.worker.test_youtube_route(route_id) if route and route["source_type"] == "youtube" else self.server.worker.test_vk_route(route_id))
            elif path == "/api/route/test-stream-pin":
                route_id = int(data.get("id"))
                target = str(data.get("target") or "").strip() or None
                result = self.server.worker.test_vk_stream_pin(route_id, target)
                result["message"] = f"Найден сегодняшний стрим-пост; закреплено без уведомления: {len(result['pinned'])}/{len(result['pinned']) + len(result['failed'])}"
                self.respond(200, result)
            elif path == "/api/route/resend":
                route_id = int(data.get("route_id"))
                source_key = str(data.get("source_key", ""))[:200]
                if not source_key:
                    raise ValueError("Не указан пост")
                target = data.get("target")
                if target is not None and not isinstance(target, str):
                    raise ValueError("Некорректный получатель")
                route = next((item for item in s.routes() if item["id"] == route_id), None)
                sender = self.server.worker.resend_youtube_video if route and route["source_type"] == "youtube" else self.server.worker.resend_vk_post
                self.respond(200, sender(route_id, source_key, target) if target is not None else sender(route_id, source_key))
            elif path == "/api/route/mark-sent":
                route_id = int(data.get("route_id"))
                source_key = str(data.get("source_key", ""))[:200]
                target = str(data.get("target", ""))[:100]
                if not source_key or not target or not isinstance(data.get("marked"), bool):
                    raise ValueError("Укажите пост, получателя и отметку")
                route = next((item for item in s.routes() if item["id"] == route_id), None)
                if route and route["source_type"] == "youtube":
                    self.server.worker.mark_youtube_video_sent(route_id, source_key, target, data["marked"])
                else:
                    self.server.worker.mark_vk_post_sent(route_id, source_key, target, data["marked"])
                self.respond(200, {"ok": True, "history": s.route_post_history(route_id)})
            elif path == "/api/route/delete":
                s.delete_route(int(data["id"]))
                self.respond(200, {"ok": True})
            elif path == "/api/route/test":
                route_id = None
                if "id" in data:
                    route_id = int(data["id"])
                    route = next((r for r in s.routes() if r["id"] == route_id), None)
                    if route is None:
                        raise ValueError("Маршрут не найден")
                else:
                    route = data
                if route["source_type"] == "youtube":
                    route = {**route, "youtube_template": data.get("youtube_template", route.get("youtube_template", ""))}
                elif route["source_type"] in ("vk", "vk_public"):
                    route = {**route, "vk_template": data.get("vk_template", route.get("vk_template", ""))}
                source, targets = s.validate_route(route)
                self.server.worker.check_telegram_targets(targets)
                if route["source_type"] == "youtube":
                    if len(targets) > 20:
                        raise ValueError("Для теста укажите не более 20 получателей")
                    result = self.server.worker.test_youtube_source(route, targets)
                    result["message"] = f"Видео {result['post']} отправлено: {len(result['sent'])}/{len(targets)}"
                    self.respond(200, result)
                    return
                if route["source_type"] not in ("vk", "vk_public"):
                    raise ValueError("Тестовая отправка доступна для групп VK")
                vk_source(source)
                if len(targets) > 20:
                    raise ValueError("Для теста укажите не более 20 получателей")
                result = self.server.worker.test_vk_post(source, targets, route_id, route["source_type"], bool(route.get("use_vk_cookie", False)))
                result["message"] = f"Пост {result['post']} отправлен: {len(result['sent'])}/{len(targets)}"
                self.respond(200, result)
            elif path == "/api/faq":
                self.respond(200, {"id": s.save_faq(data)})
            elif path == "/api/faq/delete":
                s.delete_faq(int(data["id"]))
                self.respond(200, {"ok": True})
            elif path == "/api/test":
                kind = data.get("kind")
                if kind == "telegram":
                    token = s.setting("telegram_token")
                    if not token:
                        raise ValueError("Сначала сохраните токен Telegram")
                    me = self.server.worker.refresh_telegram_identity(token)
                    self.respond(200, {"message": "Telegram: @" + me["username"]})
                elif kind == "vk":
                    source = str(data.get("source", "")).strip()
                    source_type = str(data.get("source_type", ""))
                    if not source_type:
                        source_type = next((r["source_type"] for r in s.routes(enabled=True) if r["source"] == source and r["source_type"] in ("vk", "vk_public")), "vk_public" if not s.setting("vk_token") else "vk")
                    if source_type == "vk" and not s.setting("vk_token"):
                        raise ValueError("Укажите VK токен или выберите режим «Публичная страница VK»")
                    use_cookie = bool(data.get("use_vk_cookie", False))
                    posts = vk_posts(s.setting("vk_token"), source, source_type, s.setting("vk_cookie") if use_cookie else "", s.setting("vk_ssl_verify", "1") != "0")
                    self.respond(200, {"message": f"VK: получено {len(posts)} последних записей ({'без API' if source_type == 'vk_public' else 'API'})"})
                elif kind == "llm":
                    self.respond(200, {"message": self.server.worker.answer(str(data.get("question", "Привет"))[:1500], chat_id=str(data.get("chat", "")), person="panel", panel_test=True)})
                else:
                    raise ValueError("Неизвестная проверка")
            elif path == "/api/update/apply":
                version = pending_version(self.server.data_dir)
                if not (self.server.windows_launcher or getattr(self.server, "linux_launcher", False) or getattr(self.server, "container_launcher", False)) or not version:
                    raise ValueError("Нет подготовленной версии или требуется запуск через файл запуска либо контейнерный Compose")
                marker = self.server.data_dir / "updates" / "apply-requested.txt"
                marker.write_text(version, encoding="ascii")
                self.server.update_requested = True
                self.respond(202, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.respond(404, {"error": "Не найдено"})
        except (ValueError, KeyError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
        except Exception as exc:
            self.server.store.event("error", f"Проверка: {exc}")
            self.respond(502, {"error": str(exc)[:300]})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("Задайте ADMIN_PASSWORD длиной не менее 12 символов")
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        data_dir.chmod(0o700)
    except OSError:
        pass
    store = Store(data_dir / "bot.sqlite3")
    worker = Worker(store)
    server = ThreadingHTTPServer((os.environ.get("WEB_HOST", "127.0.0.1"), int(os.environ.get("WEB_PORT", "8787"))), Handler)
    server.store, server.worker = store, worker
    server.password, server.secret = password, admin_session_secret(store, password)
    server.data_dir = data_dir
    server.windows_launcher = os.environ.get("REPOSTBOT_WINDOWS_LAUNCHER") == "1"
    server.linux_launcher = os.environ.get("REPOSTBOT_LINUX_LAUNCHER") == "1"
    server.container_launcher = os.environ.get("REPOSTBOT_CONTAINER_LAUNCHER") == "1"
    if server.container_launcher:
        # Older Docker data used loopback and the tiny bootstrap model. Point
        # those untouched defaults at the Compose Ollama service after upgrade.
        old_url = store.setting("ollama_url", "").rstrip("/")
        old_model = store.setting("ollama_model", "")
        migrated = {}
        if old_url in ("http://127.0.0.1:11434", "http://localhost:11434"):
            migrated["ollama_url"] = os.environ.get("OLLAMA_URL", "http://ollama:11434")
        if old_model in ("qwen3:0.6b", "qwen3:0.6b-q4_K_M"):
            migrated["ollama_model"] = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
        if migrated:
            store.set_settings(migrated)
    if store.setting("image_model", "") == "gpt-image-2.5-flare":
        store.set_settings({"image_model": "gpt-image-1"})
    server.local_setup = {"state": "idle", "error": "", "progress": "", "model": "", "lock": threading.Lock()}
    server.update_requested = False
    import signal
    def terminate(_signal,_frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,terminate)
    worker.start()
    print(f"Web UI: http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop.set()
        worker.scout.close()
        worker.twitch_scout.close()
        worker.speech.close()
        worker.presence.close()
        worker.models.pool.shutdown(wait=False, cancel_futures=True)
        worker.chatgpt.cancel()
        server.server_close()
    if server.update_requested:
        return 42
    return 0


if __name__ == "__main__":
    sys.exit(main())

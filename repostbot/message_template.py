"""Small, safe HTML templates for YouTube notifications in Telegram."""

import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser


DEFAULT_YOUTUBE_TEMPLATE = (
    "{test}<b>🎬 Новое видео: {title}</b>\n"
    "Канал: {channel}\n"
    "Дата выхода: {date}\n\n"
    '<a href="{url}">Смотреть на YouTube</a>'
)
FIELDS = {"test", "title", "channel", "date", "date_time", "url", "video_id"}
TAGS = {"b", "i", "u", "s", "code", "blockquote", "a"}
VARIABLE = re.compile(r"\{([a-z_]+)\}")


class _TemplateParser(HTMLParser):
    def __init__(self, fields):
        super().__init__(convert_charrefs=False)
        self.fields = fields
        self.parts = []
        self.stack = []
        self.visible = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            raise ValueError("Доступные теги: b, i, u, s, code, blockquote и a")
        if "code" in self.stack or tag == "code" and self.stack or tag in ("a", "blockquote") and any(t in ("a", "blockquote") for t in self.stack):
            raise ValueError("Ссылки и цитаты не вкладываются друг в друга; код не совмещается с оформлением")
        if tag == "a":
            if attrs != [("href", "{url}")]:
                raise ValueError('Ссылка должна иметь вид <a href="{url}">текст</a>')
        elif attrs:
            raise ValueError("Теги оформления не принимают атрибуты")
        self.parts.append('<a href="' + html.escape(self.fields["url"], quote=True) + '">' if tag == "a" else '<' + tag + '>')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise ValueError("Проверьте закрывающие теги оформления")
        self.parts.append('</' + tag + '>')
        self.stack.pop()

    def handle_data(self, data):
        if '<' in data or '>' in data:
            raise ValueError("Символы < и > в обычном тексте замените на &lt; и &gt;")
        position = 0
        for match in VARIABLE.finditer(data):
            self._literal(data[position:match.start()])
            field = match.group(1)
            if field not in self.fields:
                raise ValueError("Неизвестный параметр: {" + field + "}")
            value = str(self.fields[field])
            self.parts.append(html.escape(value, quote=False))
            self.visible.append(value)
            position = match.end()
        self._literal(data[position:])

    def _literal(self, data):
        if data:
            self.parts.append(html.escape(data, quote=False))
            self.visible.append(data)

    def handle_startendtag(self, tag, attrs):
        raise ValueError("Самозакрывающиеся теги не поддерживаются")

    def handle_comment(self, data):
        raise ValueError("Комментарии HTML в шаблоне не поддерживаются")

    def handle_entityref(self, name):
        self._literal(html.unescape('&' + name + ';'))

    def handle_charref(self, name):
        self._literal(html.unescape('&#' + name + ';'))

    def handle_decl(self, decl):
        raise ValueError("HTML-декларации в шаблоне не поддерживаются")

    def handle_pi(self, data):
        raise ValueError("HTML-инструкции в шаблоне не поддерживаются")


def _render(template, fields):
    if len(template) > 3000:
        raise ValueError("Шаблон не может быть длиннее 3000 символов")
    parser = _TemplateParser(fields)
    parser.feed(template)
    parser.close()
    if parser.stack:
        raise ValueError("Закройте все теги оформления")
    body = ''.join(parser.parts).strip()
    visible_length = len(''.join(parser.visible).strip().encode('utf-16-le')) // 2
    if not visible_length:
        raise ValueError("Шаблон сообщения не может быть пустым")
    if visible_length > 4096:
        raise ValueError("Сообщение длиннее лимита Telegram (4096 символов)")
    return body, visible_length


def render_youtube_template(template, video, test=False):
    template = template or DEFAULT_YOUTUBE_TEMPLATE
    published = video.get("published_at") or 0
    date = datetime.fromtimestamp(published, timezone.utc) if published else None
    fields = {
        "test": "[ТЕСТ] " if test else "",
        "title": video.get("title") or "",
        "channel": video.get("channel_title") or "",
        "date": date.strftime("%d.%m.%Y") if date else "не указана",
        "date_time": date.strftime("%d.%m.%Y %H:%M UTC") if date else "не указана",
        "url": video.get("source_url") or "",
        "video_id": video.get("video_id") or "",
    }
    return _render(template, fields)


DEFAULT_VK_TEMPLATE = '{test}{text}\n\n<a href="{url}">Открыть запись VK</a>{videos}'
VK_FIELDS = {"test", "text", "url", "date", "date_time", "post_id", "owner_id", "videos"}


def render_vk_template(template, post, test=False):
    from .core import vk_post_text, vk_post_link
    published = int(post.get("date") or post.get("published_at") or 0)
    date = datetime.fromtimestamp(published, timezone.utc) if published else None
    videos = post.get("video_urls") or []
    fields = {
        "test": "[ТЕСТ] " if test else "", "text": vk_post_text(post),
        "url": post.get("source_url") or vk_post_link(post),
        "date": date.strftime("%d.%m.%Y") if date else "не указана",
        "date_time": date.strftime("%d.%m.%Y %H:%M UTC") if date else "не указана",
        "post_id": post.get("post_id") or post.get("id") or "",
        "owner_id": post.get("owner_id") or "",
        "videos": "\n\nВидео:\n" + "\n".join(videos) if videos else "",
    }
    return _render(template or DEFAULT_VK_TEMPLATE, fields)


def validate_vk_template(template):
    if template:
        render_vk_template(template, {"owner_id": -1, "id": 2, "text": "Пример записи", "date": 1791504000})
    return template


def validate_youtube_template(template):
    if template:
        render_youtube_template(template, {
            "title": "Пример названия", "channel_title": "Пример канала",
            "published_at": 1791504000, "source_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "video_id": "aaaaaaaaaaa",
        })
    return template

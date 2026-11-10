"""Public YouTube channel upload feed; no account or API key is needed."""

import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree


CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def _youtube_host(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in {"youtube.com", "www.youtube.com", "m.youtube.com"}


class _YouTubeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not _youtube_host(newurl):
            host = (urlparse(newurl).hostname or "неизвестный сайт").lower()
            raise RuntimeError(f"YouTube перенаправил запрос на {host}; проверьте доступность канала с сервера")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def channel_source(value):
    """Return a safe canonical channel URL or direct UC... channel ID."""
    raw = str(value or "").strip()
    if CHANNEL_ID.fullmatch(raw):
        return "https://www.youtube.com/channel/" + raw, raw
    if raw.startswith("@"):
        raw = "https://www.youtube.com/" + raw
    if not _youtube_host(raw):
        raise ValueError("Укажите https://www.youtube.com/@канал/videos или ID канала UC…")
    path = unquote(urlparse(raw).path).strip("/")
    parts = path.split("/")
    if len(parts) == 2 and parts[1] in {"videos", "featured"}:
        parts = parts[:1]
    if len(parts) == 2 and parts[0] == "channel" and CHANNEL_ID.fullmatch(parts[1]):
        return "https://www.youtube.com/channel/" + parts[1], parts[1]
    if len(parts) == 1 and re.fullmatch(r"@[\w.-]{3,40}", parts[0], re.UNICODE):
        return "https://www.youtube.com/" + parts[0] + "/videos", ""
    raise ValueError("Укажите адрес канала YouTube вида https://www.youtube.com/@канал/videos")


def _fetch(url):
    opener = build_opener(_YouTubeRedirect())
    # A fresh server session in some regions is redirected to YouTube's
    # consent page before the public channel HTML can be read.
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; RepostBot/1.0)",
                                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                                    "Cookie": "SOCS=CAI"})
    try:
        with opener.open(request, timeout=25) as response:
            if not _youtube_host(response.geturl()):
                host = (urlparse(response.geturl()).hostname or "неизвестный сайт").lower()
                raise RuntimeError(f"YouTube перенаправил запрос на {host}; проверьте доступность канала с сервера")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Ответ YouTube слишком большой")
            return raw.decode(response.headers.get_content_charset() or "utf-8", "replace")
    except HTTPError as exc:
        raise RuntimeError(f"YouTube вернул HTTP {exc.code}") from None
    except URLError as exc:
        raise RuntimeError(f"YouTube недоступен: {exc.reason}") from None


def resolve_channel_id(source):
    url, direct_id = channel_source(source)
    if direct_id:
        return direct_id
    page = _fetch(url)
    channel_id = page_channel_id(page)
    if channel_id:
        return channel_id
    raise RuntimeError("YouTube не указал ID канала на странице. Попробуйте адрес /channel/UC…")


def _initial_data(page):
    marker = re.search(r'(?:var\s+ytInitialData|window\[["\']ytInitialData["\']\]|ytInitialData)\s*=\s*(\{)', page)
    if not marker:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(page[marker.start(1):])
        return state
    except ValueError:
        raise RuntimeError("Не удалось прочитать список видео YouTube") from None


def page_channel_id(page, state=None):
    """Use the channel's own metadata, never an ID in recommendations."""
    state = state if state is not None else _initial_data(page)
    metadata = (state or {}).get("metadata", {}).get("channelMetadataRenderer", {})
    value = metadata.get("externalId", "")
    if CHANNEL_ID.fullmatch(value):
        return value
    patterns = (
        r'<meta\s+[^>]*itemprop=["\']channelId["\'][^>]*content=["\'](UC[A-Za-z0-9_-]{22})',
        r'<link\s+[^>]*rel=["\']alternate["\'][^>]*href=["\']https://www\.youtube\.com/feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            return match.group(1)
    return ""


def parse_feed(xml, expected_channel_id):
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise RuntimeError("YouTube вернул некорректную ленту видео") from None
    if root.tag != ATOM + "feed":
        raise RuntimeError("YouTube вернул ответ без ленты видео")
    channel_id = root.findtext(YT + "channelId")
    if expected_channel_id and channel_id and channel_id != expected_channel_id:
        raise RuntimeError("Лента YouTube принадлежит другому каналу")
    channel_title = html.unescape(root.findtext(ATOM + "title") or "Канал YouTube")
    videos = []
    seen = set()
    for entry in root.findall(ATOM + "entry"):
        video_id = entry.findtext(YT + "videoId") or ""
        if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        published = entry.findtext(ATOM + "published") or ""
        try:
            published_at = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        seen.add(video_id)
        videos.append({"video_id": video_id, "title": html.unescape(entry.findtext(ATOM + "title") or "Без названия"),
                       "channel_title": channel_title, "published_at": published_at,
                       "source_url": "https://www.youtube.com/watch?v=" + video_id})
    return videos


def video_publish_time(video_id, expected_channel_id=""):
    """Get the public publication date from the video's own watch page."""
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Некорректный ID видео YouTube")
    page = _fetch("https://www.youtube.com/watch?v=" + video_id)
    marker = re.search(r'\bytInitialPlayerResponse\s*=\s*(\{)', page)
    if not marker:
        raise RuntimeError("YouTube не показал метаданные видео")
    try:
        player, _ = json.JSONDecoder().raw_decode(page[marker.start(1):])
    except ValueError:
        raise RuntimeError("Не удалось прочитать дату видео YouTube") from None
    details = player.get("videoDetails", {})
    microformat = player.get("microformat", {}).get("playerMicroformatRenderer", {})
    if details.get("videoId") != video_id:
        raise RuntimeError("YouTube показал сведения другого видео")
    owner = microformat.get("externalChannelId", "")
    if expected_channel_id and owner and owner != expected_channel_id:
        raise RuntimeError("Видео принадлежит другому каналу")
    published = microformat.get("publishDate") or microformat.get("uploadDate") or ""
    if not published:
        match = re.search(r'<meta\s+[^>]*itemprop=["\'](?:datePublished|uploadDate)["\'][^>]*content=["\']([^"\']+)', page, re.I)
        published = match.group(1) if match else ""
    try:
        value = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError("YouTube не указал дату публикации видео") from None
    if value.tzinfo is None:
        # The page may only supply a calendar date. Noon UTC keeps that date
        # unchanged when displayed in the panel's local timezone.
        value = value.replace(tzinfo=timezone.utc) + timedelta(hours=12)
    return int(value.timestamp())


def channel_videos(channel_id, channel_url=None):
    if not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Некорректный ID канала YouTube")
    feed_xml = ""
    try:
        feed_xml = _fetch("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id)
        return parse_feed(feed_xml, channel_id)
    except RuntimeError as feed_error:
        try:
            videos = parse_videos_page(_fetch(channel_url or "https://www.youtube.com/channel/" + channel_id + "/videos"), channel_id)
            if feed_xml and "другому каналу" in str(feed_error):
                # The feed sometimes identifies another channel. Only use
                # dates for video IDs independently confirmed on this page.
                feed_by_id = {video["video_id"]: video for video in parse_feed(feed_xml, "")}
                for video in videos:
                    if video["video_id"] in feed_by_id:
                        video["published_at"] = feed_by_id[video["video_id"]]["published_at"]
            return videos
        except RuntimeError as page_error:
            raise RuntimeError(f"лента YouTube недоступна ({feed_error}); страница видео: {page_error}") from None


def parse_videos_page(page, expected_channel_id):
    """Read only the channel's Videos grid when its Atom feed is unavailable."""
    state = _initial_data(page)
    if state is None:
        raise RuntimeError("YouTube не показал список видео")
    actual_id = page_channel_id(page, state)
    if actual_id and actual_id != expected_channel_id:
        raise RuntimeError("Страница YouTube принадлежит другому каналу")

    def title_text(value):
        if not isinstance(value, dict):
            return ""
        return str(value.get("simpleText") or "".join(run.get("text", "") for run in value.get("runs", []) if isinstance(run, dict)))

    def grids(value):
        if isinstance(value, dict):
            if isinstance(value.get("richGridRenderer"), dict):
                yield value["richGridRenderer"]
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from grids(child)
        elif isinstance(value, list):
            for child in value:
                yield from grids(child)

    channel_title = state.get("metadata", {}).get("channelMetadataRenderer", {}).get("title") or "Канал YouTube"
    meta = re.search(r'<meta\s+[^>]*(?:property|name)=["\']og:title["\'][^>]*content=["\']([^"\']+)', page, re.I)
    if meta:
        if channel_title == "Канал YouTube":
            channel_title = html.unescape(meta.group(1))
    tabs = state.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
    selected = next((tab.get("tabRenderer", tab.get("exassistantbleTabRenderer", {})) for tab in tabs
                     if tab.get("tabRenderer", tab.get("exassistantbleTabRenderer", {})).get("selected")), None)
    content_root = selected.get("content", {}) if selected else state
    videos = []
    seen = set()
    for grid in grids(content_root):
        for item in grid.get("contents", []):
            if not isinstance(item, dict):
                continue
            content = item.get("richItemRenderer", {}).get("content", {})
            video = content.get("videoRenderer") or content.get("gridVideoRenderer")
            lockup = content.get("lockupViewModel", {})
            if isinstance(video, dict):
                video_id = video.get("videoId", "")
                title = title_text(video.get("title"))
            elif isinstance(lockup, dict) and lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
                video_id = lockup.get("contentId", "")
                title = lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {}).get("content", "")
            else:
                continue
            if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
                continue
            seen.add(video_id)
            videos.append({"video_id": video_id, "title": html.unescape(title or "Без названия"),
                           "channel_title": channel_title, "published_at": 0,
                           "source_url": "https://www.youtube.com/watch?v=" + video_id})
            if len(videos) >= 15:
                return videos
    if not videos:
        raise RuntimeError("YouTube не показал видео на вкладке канала")
    return videos

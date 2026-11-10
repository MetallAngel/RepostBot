"""Resolve a public community from page identity, never an arbitrary post link."""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


def numeric_owner(source):
    path = str(source or '').strip().rstrip('/').split('/')[-1].split('?')[0].lstrip('@')
    match = re.fullmatch(r'(?:club|public|event)(\d+)|wall(-\d+)|(-?\d+)', path, re.I)
    if not match:
        return 0
    return -abs(int(next(value for value in match.groups() if value)))


class PageIdentity(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.header = True
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            self.header = False
        if not self.header:
            return
        data = dict(attrs)
        if tag == 'link' and 'canonical' in data.get('rel', '').casefold().split():
            self.urls.append(data.get('href', ''))
        elif tag == 'meta' and data.get('property', '').casefold() == 'og:url':
            self.urls.append(data.get('content', ''))


def objects(page):
    decoder = json.JSONDecoder()
    # Primary VK bootstrap can follow hundreds of unrelated script objects.
    primary = r'window\.cur\s*=\s*Object\.assign\(window\.cur\s*\|\|\s*\{\s*\}\s*,\s*(\{)'
    starts = [match.start(1) for match in re.finditer(primary, page)][:10]
    starts += [match.start() for match in re.finditer(r'(?<![A-Za-z0-9_])\{', page)][:300]
    for start in dict.fromkeys(starts):
        try:
            value, _ = decoder.raw_decode(page[start:])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def walk(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child, depth + 1)


def public_owner(page, domain):
    """Require canonical identity, a matching group record or scoped wall.get."""
    raw = html.unescape(str(page or ''))
    expected = domain.casefold()
    header = PageIdentity()
    try:
        header.feed(raw)
    except Exception:
        pass
    candidates = set()
    canonical_matches = False
    for url in header.urls:
        parsed = urlsplit(url if '://' in url else 'https://vk.com/' + url.lstrip('/'))
        if (parsed.hostname or '').casefold() not in ('vk.com', 'vk.ru', 'm.vk.com', 'm.vk.ru'):
            continue
        path = parsed.path.strip('/')
        owner = numeric_owner(path)
        if owner:
            candidates.add(owner)
        elif path.casefold() == expected:
            canonical_matches = True
    for root in objects(raw):
        for value in walk(root):
            label = value.get('screen_name') or value.get('domain') or value.get('short_name')
            if isinstance(label, str) and label.casefold() == expected:
                group = value.get('group_id') or value.get('public_id') or value.get('club_id')
                if not group and value.get('type') in ('group', 'page', 'event', 'community'):
                    group = value.get('id')
                if not group and value.get('owner_id', 0) and str(value.get('owner_id')).startswith('-'):
                    group = value.get('owner_id')
                try:
                    if group:
                        candidates.add(-abs(int(group)))
                except (TypeError, ValueError):
                    pass
            if value.get('method') == 'wall.get':
                params = value.get('params') or value.get('parameters') or {}
                if isinstance(params, list):
                    params = dict(item for item in params if isinstance(item, (list, tuple)) and len(item) == 2)
                if not isinstance(params, dict) or str(params.get('domain', '')).casefold() != expected:
                    continue
                response = value.get('response') or {}
                if not isinstance(response, dict):
                    continue
                owners = {int(item.get('owner_id', 0)) for item in response.get('items', [])
                          if isinstance(item, dict) and str(item.get('owner_id', '')).lstrip('-').isdigit()}
                if len(owners) == 1 and next(iter(owners)) < 0:
                    candidates.update(owners)
        if canonical_matches:
            # Only the primary bootstrap's owner, not arbitrary nested objects.
            try:
                owner = int(root.get('oid') or root.get('wall_owner_id') or 0)
                if owner < 0:
                    candidates.add(owner)
            except (ValueError, TypeError):
                pass
    if canonical_matches:
        for match in re.finditer(r'\bcur\.(?:oid|owner_id)\s*=\s*(-\d+)', raw):
            candidates.add(int(match[1]))
    # Conflicting identities must not select the first recommendation.
    return next(iter(candidates)) if len(candidates) == 1 else 0

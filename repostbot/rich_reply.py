"""Native Telegram articles and a readable fallback for older Bot API servers.

sendRichMessage accepts a rich_message with a Markdown source, including tables:
https://github.com/tdlib/telegram-bot-api/blob/master/telegram-bot-api/Client.cpp
No third-party publication service or extra model request is involved.
"""

import re


def article_requested(question):
    return bool(re.search(r"\bстать(?:ю|ёй|ей)\b|\b(?:as an? |write an? )article\b", str(question), re.I))


def structured_requested(question):
    return article_requested(question) or bool(re.search(r"таблиц|таблич|\btable\b", str(question), re.I))


def _cells(line):
    """Split table cells without treating escaped/code-span pipes as columns."""
    cells, current, code_ticks = [], [], 0
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            current.extend(line[i:i + 2])
            i += 2
            continue
        if char == "`":
            end = i + 1
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - i
            code_ticks = 0 if code_ticks == run else run if not code_ticks else code_ticks
            current.append(line[i:end])
            i = end
            continue
        if char == "|" and not code_ticks:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    cells.append("".join(current).strip())
    if line.lstrip().startswith("|"):
        cells = cells[1:]
    if line.rstrip().endswith("|") and not line.rstrip().endswith("\\|"):
        cells = cells[:-1]
    return cells


def blocks(markdown):
    """Yield only validated GFM tables; leave ordinary pipes and code alone."""
    lines = str(markdown).splitlines()
    i, fence = 0, ""
    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = ""
            yield "text", line
            i += 1
            continue
        if not fence and "|" in line and i + 1 < len(lines):
            header, divider = _cells(line), _cells(lines[i + 1])
            if (len(header) >= 2 and len(header) == len(divider)
                    and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in divider)):
                rows = []
                j = i + 2
                while j < len(lines) and "|" in lines[j]:
                    row = _cells(lines[j])
                    if len(row) != len(header):
                        break
                    rows.append(row)
                    j += 1
                if rows:
                    yield "table", (header, rows)
                    i = j
                    continue
        yield "text", line
        i += 1


def has_table(markdown):
    return any(kind == "table" for kind, _ in blocks(markdown))


def wants_article(answer, question=""):
    return article_requested(question) or has_table(answer) or len(str(answer).encode("utf-16-le")) // 2 > 3500


def _plain(text):
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", str(text))
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m[1] or m[2], text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.replace(r"\|", "|")


def readable_fallback(markdown):
    """Cards keep wide comparisons legible on phones without monospaced grids."""
    result = []
    for kind, content in blocks(markdown):
        if kind != "table":
            result.append(_plain(re.sub(r"^\s*#{1,6}\s+", "", content)))
            continue
        header, rows = content
        for row in rows:
            result.append("")
            result.append(_plain(row[0]))
            result.extend(_plain(f"{label}: {value}") for label, value in zip(header[1:], row[1:]))
        result.append("")
    return "\n".join(result).strip()


def split_text(text, limit=3500):
    """Keep every character and respect Telegram's UTF-16 message limit."""
    text = str(text)
    while text:
        units = 0
        end = 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end < len(text):
            boundary = text.rfind("\n", 0, end)
            if boundary >= end // 2:
                end = boundary + 1
        yield text[:end]
        text = text[end:]


def rich_message_text(message):
    """Read received articles as context; never execute any embedded content."""
    def rich(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(rich(item) for item in value)
        return rich(value.get("text", "")) if isinstance(value, dict) else ""

    def walk(items, depth=0):
        if depth > 8 or not isinstance(items, list):
            return []
        result = []
        for block in items[:200]:
            if not isinstance(block, dict):
                continue
            result.append(rich(block.get("text", "")))
            if block.get("type") == "table":
                for row in block.get("cells", [])[:100]:
                    if isinstance(row, list):
                        result.append(" | ".join(rich(cell.get("text", "")) for cell in row if isinstance(cell, dict)))
            result.extend(walk(block.get("blocks"), depth + 1))
            for item in (block.get("items") or [])[:100]:
                if isinstance(item, dict):
                    result.extend(walk(item.get("blocks"), depth + 1))
        return result

    article = message.get("rich_message") or {}
    return "\n".join(filter(None, walk(article.get("blocks")))) if isinstance(article, dict) else ""

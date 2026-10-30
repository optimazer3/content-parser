"""Работа с текстом: нормализация, хэши для дедупа, Telegram HTML, разбивка длинных сообщений."""

import hashlib
import html
import re

TG_LIMIT = 4096

_URL_RE = re.compile(r"https?://\S+")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t ]+")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    text = _URL_RE.sub(" ", text.lower())
    return _NON_WORD_RE.sub(" ", text).strip()


def text_hash(text: str) -> str:
    """Хэш по началу нормализованного текста: ловит одинаковые репосты из разных каналов."""
    return hashlib.sha1(normalize(text)[:500].encode()).hexdigest()


def strip_html(raw: str) -> str:
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", raw)
    text = html.unescape(_TAG_RE.sub("", raw))
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _MANY_NEWLINES_RE.sub("\n\n", text).strip()


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "blockquote", "a", "tg-spoiler"}
# обычный HTML, который модель иногда вставляет: убираем тег, оставляя текст
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "section", "article"}
_DROP_TAGS = _BLOCK_TAGS | {"span", "small", "sup", "sub", "mark", "font", "center", "header", "footer"}
_TOKEN_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^<>]*)>")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""")


def sanitize_html(text: str) -> str:
    """Оставляет только теги, которые понимает Telegram; остальное экранирует.

    Модель иногда отвечает Markdown'ом — **жирный** и *курсив* переводим в теги.
    """
    text = text.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<i>\1</i>", text)
    text = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", text)

    out = []
    pos = 0
    stack: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        out.append(_escape_keep_entities(text[pos:m.start()]))
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name == "br":
            out.append("\n")
            continue
        if name == "li":
            out.append("" if closing else "\n• ")
            continue
        if name in _DROP_TAGS:
            if closing and name in _BLOCK_TAGS:
                out.append("\n\n")
            continue
        if name not in _ALLOWED_TAGS:
            out.append(esc(m.group(0)))
            continue
        if closing:
            if name in stack:
                # закрываем всё, что открыто внутри, чтобы теги не перекрещивались
                while stack:
                    top = stack.pop()
                    out.append(f"</{top}>")
                    if top == name:
                        break
            continue
        if name == "a":
            href = _HREF_RE.search(attrs)
            if not href:
                continue
            out.append(f'<a href="{html.escape(href.group(1), quote=True)}">')
        else:
            out.append(f"<{name}>")
        stack.append(name)
    out.append(_escape_keep_entities(text[pos:]))
    out.extend(f"</{tag}>" for tag in reversed(stack))
    return _MANY_NEWLINES_RE.sub("\n\n", "".join(out)).strip()


def _escape_keep_entities(chunk: str) -> str:
    return esc(html.unescape(chunk))


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет по абзацам, потом по строкам, в крайнем случае — жёстко по лимиту."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        parts.append(current)
    return parts


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

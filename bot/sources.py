"""Разбор того, что пользователь прислал в /add: канал, приватная ссылка или сайт."""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog", "www.t.me"}


@dataclass
class ParsedRef:
    kind: str  # 'tg' | 'tg_invite' | 'web' | 'bad'
    value: str


def classify_ref(raw: str) -> ParsedRef:
    original = raw.strip().strip("<>")
    ref = original.rstrip("/")
    if not ref:
        return ParsedRef("bad", raw)
    if ref.startswith("@"):
        name = ref[1:]
        return ParsedRef("tg", name.lower()) if _USERNAME_RE.match(name) else ParsedRef("bad", raw)

    url = ref if re.match(r"^[a-z]+://", ref, re.I) else f"https://{ref}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in _TG_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return ParsedRef("bad", raw)
        if parts[0].startswith("+") or parts[0] == "joinchat":
            return ParsedRef("tg_invite", ref)
        if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
            return ParsedRef("tg", f"id:-100{parts[1]}")
        if parts[0] == "s" and len(parts) > 1:
            parts = parts[1:]
        name = parts[0]
        return ParsedRef("tg", name.lower()) if _USERNAME_RE.match(name) else ParsedRef("bad", raw)

    if "." not in host:
        # «durov» без @ и без точки — считаем юзернеймом канала
        if _USERNAME_RE.match(ref):
            return ParsedRef("tg", ref.lower())
        return ParsedRef("bad", raw)
    # для сайта оставляем адрес как прислали: часть серверов отвечает 404 без последнего «/»
    url = original if re.match(r"^[a-z]+://", original, re.I) else f"https://{original}"
    if not urlparse(url).path:
        url += "/"
    return ParsedRef("web", url)


def split_refs(text: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", text) if p]

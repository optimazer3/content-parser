"""Сайты: RSS, если он есть (ищем сами), иначе — новые ссылки со страницы + текст статьи."""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import httpx
import trafilatura

from . import RawItem
from ..db import Source
from ..text import strip_html

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}
COMMON_FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/feed/rss"]
FIRST_FETCH = 10
MAX_FETCH = 25
MIN_ARTICLE = 300
SHORT_SUMMARY = 400  # если в RSS только анонс — дочитываем статью целиком

_SKIP_PATH_RE = re.compile(
    r"/(tag|tags|category|categories|author|authors|page|login|signin|signup|register|search|about|contacts?|"
    r"privacy|terms|feed|rss|cart|account|user|users|hubs?|companies)(/|$)",
    re.I,
)
_ASSET_RE = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js|xml)$", re.I)


def make_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "link" and "alternate" in a.get("rel", "").lower():
            if any(t in a.get("type", "").lower() for t in ("rss", "atom", "feed+json")) and a.get("href"):
                self.feeds.append(a["href"])
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse_page(html: str, base_url: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception:  # битый HTML — берём, что успели разобрать
        pass
    parser.feeds = [urljoin(base_url, f) for f in parser.feeds]
    parser.title = parser.title.strip()
    return parser


def _same_site(a: str, b: str) -> bool:
    ha = (urlparse(a).hostname or "").removeprefix("www.")
    hb = (urlparse(b).hostname or "").removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def article_links(html: str, base_url: str) -> list[str]:
    """Ссылки, похожие на статьи: тот же сайт, «глубокий» путь, не теги/категории/картинки."""
    base_path = urlparse(base_url).path.rstrip("/")
    seen, result = set(), []
    for href in parse_page(html, base_url).links:
        url = urljoin(base_url, href.strip())
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not _same_site(url, base_url):
            continue
        path = p.path.rstrip("/")
        if not path or path == base_path or _SKIP_PATH_RE.search(path) or _ASSET_RE.search(path):
            continue
        segments = [s for s in path.split("/") if s]
        last = segments[-1]
        looks_like_article = (
            len(segments) >= 2 and (re.search(r"\d", path) or "-" in last or "_" in last or len(last) > 15)
        ) or last.endswith((".html", ".htm")) or ("-" in last and len(last) > 20)
        if not looks_like_article:
            continue
        clean = urlunparse(p._replace(fragment="", query=""))
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def parse_feed(content: bytes):
    feed = feedparser.parse(content)
    return feed if feed.entries else None


def _entry_date(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc).isoformat(timespec="seconds")


def _entry_text(entry) -> str:
    if entry.get("content"):
        raw = max((c.get("value", "") for c in entry.content), key=len)
    else:
        raw = entry.get("summary", "")
    return strip_html(raw)


def extract_article(html: str, url: str) -> RawItem | None:
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False,
                                      include_tables=False, favor_precision=True)
    if not doc or not doc.text or len(doc.text) < MIN_ARTICLE:
        return None
    return RawItem(url=url, text=doc.text.strip(), title=doc.title, published_at=doc.date)


class WebCollector:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._sem = asyncio.Semaphore(3)

    async def _get(self, url: str) -> httpx.Response:
        async with self._sem:
            r = await self.http.get(url)
        r.raise_for_status()
        return r

    async def discover(self, url: str) -> tuple[str, str | None]:
        """(название сайта, адрес RSS или None — тогда будем разбирать страницу)."""
        try:
            r = await self._get(url)
        except httpx.HTTPError as e:
            raise LookupError(f"сайт не открылся ({e.__class__.__name__})")

        feed = parse_feed(r.content)
        if feed:
            return feed.feed.get("title") or urlparse(url).hostname, str(r.url)

        page = parse_page(r.text, str(r.url))
        title = page.title or urlparse(url).hostname
        root = f"{r.url.scheme}://{r.url.host}"
        for candidate in page.feeds + [root + p for p in COMMON_FEED_PATHS]:
            try:
                fr = await self._get(candidate)
            except httpx.HTTPError:
                continue
            if parse_feed(fr.content):
                return title, str(fr.url)

        if not article_links(r.text, str(r.url)):
            raise LookupError("не нашёл ни RSS, ни ссылок на статьи — возможно, сайт рисуется через JavaScript")
        return title, None

    async def fetch_article(self, url: str) -> RawItem | None:
        try:
            r = await self._get(url)
        except httpx.HTTPError as e:
            log.info("Не открылась статья %s: %s", url, e)
            return None
        return await asyncio.to_thread(extract_article, r.text, str(r.url))

    async def fetch_new(self, source: Source, known_urls) -> tuple[list[RawItem], list[str]]:
        """Новые статьи и все ссылки, которые пробовали открыть (их больше не трогаем).

        known_urls: async-функция, которая из списка ссылок возвращает уже виденные.
        """
        limit = FIRST_FETCH if not source.last_seen else MAX_FETCH
        if source.feed_url:
            return await self._from_feed(source.feed_url, limit, known_urls)
        return await self._from_page(source.ref, limit, known_urls)

    async def _from_feed(self, feed_url: str, limit: int, known_urls) -> tuple[list[RawItem], list[str]]:
        r = await self._get(feed_url)
        feed = parse_feed(r.content)
        if not feed:
            return [], []
        entries = [e for e in feed.entries[:limit] if e.get("link")]
        known = await known_urls([e.link for e in entries])
        fresh = [e for e in entries if e.link not in known]

        async def build(entry) -> RawItem | None:
            text = _entry_text(entry)
            if len(text) < SHORT_SUMMARY:
                full = await self.fetch_article(entry.link)
                if full and len(full.text) > len(text):
                    text = full.text
            if len(text) < 40:
                return None
            return RawItem(url=entry.link, text=text, title=entry.get("title"), published_at=_entry_date(entry))

        items = await asyncio.gather(*(build(e) for e in fresh))
        return [i for i in reversed(items) if i], [e.link for e in fresh]

    async def _from_page(self, page_url: str, limit: int, known_urls) -> tuple[list[RawItem], list[str]]:
        r = await self._get(page_url)
        links = article_links(r.text, str(r.url))[:40]
        known = await known_urls(links)
        fresh = [u for u in links if u not in known][:limit]
        items = await asyncio.gather(*(self.fetch_article(u) for u in fresh))
        return [i for i in reversed(items) if i], fresh

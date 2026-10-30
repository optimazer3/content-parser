import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'tg' | 'web'
    ref TEXT NOT NULL UNIQUE,        -- tg: username или id:<число>; web: адрес сайта
    title TEXT,
    feed_url TEXT,                   -- web: найденный RSS, NULL → разбираем HTML
    last_seen INTEGER,               -- tg: id последнего прочитанного сообщения
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER,
    url TEXT NOT NULL UNIQUE,
    text_hash TEXT NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    score INTEGER,                   -- NULL → ещё не оценён
    summary TEXT,
    topic_key TEXT,
    digested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_hash ON items(text_hash);
CREATE INDEX IF NOT EXISTS items_pending ON items(digested, score);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    item_ids TEXT NOT NULL,          -- JSON-список id из items
    msg_id INTEGER,
    feedback INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER,
    text TEXT NOT NULL,
    msg_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_urls (   -- ссылки с сайтов, которые уже открывали
    url TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


@dataclass
class Source:
    id: int
    kind: str
    ref: str
    title: str | None
    feed_url: str | None
    last_seen: int | None

    @property
    def name(self) -> str:
        return self.title or self.ref


@dataclass
class Item:
    id: int
    source_id: int | None
    url: str
    title: str | None
    text: str
    score: int | None
    summary: str | None
    topic_key: str | None
    source_name: str = ""


@dataclass
class Topic:
    id: int
    title: str
    summary: str | None
    item_ids: list[int]
    msg_id: int | None
    feedback: int


@dataclass
class Draft:
    id: int
    topic_id: int | None
    text: str
    msg_id: int | None


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "Database":
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # --- настройки ---

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # --- источники ---

    async def add_source(self, kind: str, ref: str, title: str | None, feed_url: str | None = None) -> bool:
        """False, если такой источник уже есть."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO sources(kind, ref, title, feed_url, added_at) VALUES(?, ?, ?, ?, ?)",
            (kind, ref, title, feed_url, now()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_sources(self) -> list[Source]:
        async with self.conn.execute("SELECT * FROM sources ORDER BY kind, id") as cur:
            rows = await cur.fetchall()
        return [Source(r["id"], r["kind"], r["ref"], r["title"], r["feed_url"], r["last_seen"]) for r in rows]

    async def delete_source(self, source_id: int) -> Source | None:
        sources = {s.id: s for s in await self.list_sources()}
        await self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await self.conn.commit()
        return sources.get(source_id)

    async def set_last_seen(self, source_id: int, last_seen: int) -> None:
        await self.conn.execute("UPDATE sources SET last_seen = ? WHERE id = ?", (last_seen, source_id))
        await self.conn.commit()

    # --- посты ---

    async def known_urls(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        marks = ",".join("?" * len(urls))
        sql = f"SELECT url FROM items WHERE url IN ({marks}) UNION SELECT url FROM seen_urls WHERE url IN ({marks})"
        async with self.conn.execute(sql, urls + urls) as cur:
            return {r["url"] for r in await cur.fetchall()}

    async def mark_seen(self, urls: list[str]) -> None:
        await self.conn.executemany("INSERT OR IGNORE INTO seen_urls(url) VALUES(?)", [(u,) for u in urls])
        await self.conn.commit()

    async def add_item(
        self,
        source_id: int | None,
        url: str,
        text_hash: str,
        title: str | None,
        text: str,
        published_at: str | None,
    ) -> int | None:
        """Сохраняет пост. None — дубль по ссылке или по тексту."""
        async with self.conn.execute("SELECT 1 FROM items WHERE text_hash = ? OR url = ?", (text_hash, url)) as cur:
            if await cur.fetchone():
                return None
        cur = await self.conn.execute(
            "INSERT INTO items(source_id, url, text_hash, title, text, published_at, collected_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (source_id, url, text_hash, title, text, published_at, now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def item_id_by_hash(self, text_hash: str) -> int | None:
        async with self.conn.execute("SELECT id FROM items WHERE text_hash = ? LIMIT 1", (text_hash,)) as cur:
            row = await cur.fetchone()
        return row["id"] if row else None

    async def _items(self, where: str, params: tuple = (), tail: str = "") -> list[Item]:
        sql = (
            "SELECT i.*, COALESCE(s.title, s.ref, '') AS source_name FROM items i "
            f"LEFT JOIN sources s ON s.id = i.source_id WHERE {where} {tail}"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            Item(r["id"], r["source_id"], r["url"], r["title"], r["text"], r["score"], r["summary"],
                 r["topic_key"], r["source_name"])
            for r in rows
        ]

    async def unscored_items(self, limit: int = 200) -> list[Item]:
        return await self._items("i.score IS NULL AND i.source_id IS NOT NULL", (), f"ORDER BY i.id LIMIT {int(limit)}")

    async def save_score(self, item_id: int, score: int, title: str, summary: str, topic_key: str) -> None:
        await self.conn.execute(
            "UPDATE items SET score = ?, title = COALESCE(NULLIF(?, ''), title), summary = ?, topic_key = ? WHERE id = ?",
            (score, title, summary, topic_key, item_id),
        )
        await self.conn.commit()

    async def digest_candidates(self, min_score: int, limit: int = 60) -> list[Item]:
        return await self._items(
            "i.digested = 0 AND i.score >= ? AND i.collected_at >= ?",
            (min_score, ago(hours=48)),
            f"ORDER BY i.score DESC, i.id DESC LIMIT {int(limit)}",
        )

    async def mark_digested(self, item_ids: list[int]) -> None:
        await self.conn.executemany("UPDATE items SET digested = 1 WHERE id = ?", [(i,) for i in item_ids])
        await self.conn.commit()

    async def get_items(self, item_ids: list[int]) -> list[Item]:
        if not item_ids:
            return []
        marks = ",".join("?" * len(item_ids))
        return await self._items(f"i.id IN ({marks})", tuple(item_ids))

    async def recent_items(self, days: int = 14, limit: int = 2000) -> list[Item]:
        return await self._items("i.collected_at >= ? AND i.source_id IS NOT NULL", (ago(days=days),),
                                 f"ORDER BY i.id DESC LIMIT {int(limit)}")

    # --- темы подборок ---

    async def add_topic(self, title: str, summary: str | None, item_ids: list[int]) -> int:
        cur = await self.conn.execute(
            "INSERT INTO topics(title, summary, item_ids, created_at) VALUES(?, ?, ?, ?)",
            (title, summary, json.dumps(item_ids), now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def _topics(self, where: str, params: tuple = (), tail: str = "") -> list[Topic]:
        async with self.conn.execute(f"SELECT * FROM topics WHERE {where} {tail}", params) as cur:
            rows = await cur.fetchall()
        return [Topic(r["id"], r["title"], r["summary"], json.loads(r["item_ids"]), r["msg_id"], r["feedback"])
                for r in rows]

    async def get_topic(self, topic_id: int) -> Topic | None:
        found = await self._topics("id = ?", (topic_id,))
        return found[0] if found else None

    async def topic_by_msg(self, msg_id: int) -> Topic | None:
        found = await self._topics("msg_id = ?", (msg_id,), "ORDER BY id DESC LIMIT 1")
        return found[0] if found else None

    async def set_topic_msg(self, topic_id: int, msg_id: int) -> None:
        await self.conn.execute("UPDATE topics SET msg_id = ? WHERE id = ?", (msg_id, topic_id))
        await self.conn.commit()

    async def set_feedback(self, topic_id: int, value: int) -> None:
        await self.conn.execute("UPDATE topics SET feedback = ? WHERE id = ?", (value, topic_id))
        await self.conn.commit()

    async def recent_topic_titles(self, hours: int = 36) -> list[str]:
        topics = await self._topics("created_at >= ? AND msg_id IS NOT NULL", (ago(hours=hours),), "ORDER BY id DESC LIMIT 40")
        return [t.title for t in topics]

    async def disliked_titles(self, limit: int = 30) -> list[str]:
        topics = await self._topics("feedback < 0", (), f"ORDER BY id DESC LIMIT {int(limit)}")
        return [t.title for t in topics]

    # --- черновики постов ---

    async def add_draft(self, topic_id: int | None, text: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO drafts(topic_id, text, created_at) VALUES(?, ?, ?)", (topic_id, text, now())
        )
        await self.conn.commit()
        return cur.lastrowid

    async def set_draft_msg(self, draft_id: int, msg_id: int) -> None:
        await self.conn.execute("UPDATE drafts SET msg_id = ? WHERE id = ?", (msg_id, draft_id))
        await self.conn.commit()

    async def _drafts(self, where: str, params: tuple) -> Draft | None:
        async with self.conn.execute(f"SELECT * FROM drafts WHERE {where} ORDER BY id DESC LIMIT 1", params) as cur:
            r = await cur.fetchone()
        return Draft(r["id"], r["topic_id"], r["text"], r["msg_id"]) if r else None

    async def get_draft(self, draft_id: int) -> Draft | None:
        return await self._drafts("id = ?", (draft_id,))

    async def draft_by_msg(self, msg_id: int) -> Draft | None:
        return await self._drafts("msg_id = ?", (msg_id,))

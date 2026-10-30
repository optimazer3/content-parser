import json

import aiosqlite

from bot.db import Database


async def test_migration_from_first_version(tmp_path):
    path = str(tmp_path / "old.db")
    # схема и данные, как их оставляла первая версия бота
    async with aiosqlite.connect(path) as conn:
        await conn.executescript("""
            CREATE TABLE items (id INTEGER PRIMARY KEY, source_id INTEGER, url TEXT NOT NULL UNIQUE,
                text_hash TEXT NOT NULL, title TEXT, text TEXT NOT NULL, published_at TEXT,
                collected_at TEXT NOT NULL, score INTEGER, summary TEXT, topic_key TEXT,
                digested INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE topics (id INTEGER PRIMARY KEY, title TEXT NOT NULL, summary TEXT, item_ids TEXT NOT NULL,
                msg_id INTEGER, feedback INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        now = "2999-01-01T00:00:00+00:00"
        later = "2999-01-02T00:00:00+00:00"  # собран после последней подборки
        for n, collected, digested in ((1, now, 1), (2, now, 1), (3, later, 0)):
            await conn.execute("INSERT INTO items(id, source_id, url, text_hash, text, collected_at, score, digested) "
                               "VALUES(?, 1, ?, ?, 'текст', ?, 7, ?)", (n, f"https://x/{n}", f"h{n}", collected, digested))
        await conn.execute("INSERT INTO topics(title, item_ids, msg_id, created_at) VALUES('Тема', ?, 10, ?)",
                           (json.dumps([1]), now))
        await conn.execute("INSERT INTO settings VALUES('interests', '/setting')")
        await conn.commit()

    db = await Database.open(path)
    try:
        topic = (await db._topics("1"))[0]
        assert await db.recent_topic_titles(hours=10**7) == [topic.title]  # отправленная тема помечена sent
        items = {i.id: i for i in await db._items("1")}
        # отработанные посты остаются отработанными, новый переоценим по новым правилам
        assert [i.score for i in items.values()] == [7, 7, None]
        async with db.conn.execute("SELECT id, digested FROM items ORDER BY id") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [(1, 1), (2, 1), (3, 0)]
        assert await db.get_setting("interests") is None
    finally:
        await db.close()
    # повторное открытие ничего не ломает
    db = await Database.open(path)
    await db.close()


async def test_migration_after_intermediate_version(tmp_path):
    """Промежуточная версия вернула отработанные посты в игру — миграция снова помечает их отработанными."""
    db = await Database.open(str(tmp_path / "v1.db"))
    for n, collected in ((1, "2999-01-01T00:00:00+00:00"), (2, "2999-01-03T00:00:00+00:00")):
        await db.conn.execute("INSERT INTO items(id, source_id, url, text_hash, text, collected_at, score, digested) "
                              "VALUES(?, 1, ?, ?, 'текст', ?, 7, 0)", (n, f"https://x/{n}", f"h{n}", collected))
    await db.conn.execute("INSERT INTO topics(title, item_ids, sent, created_at) VALUES('Тема', '[]', 1, ?)",
                          ("2999-01-02T00:00:00+00:00",))
    await db.conn.execute("PRAGMA user_version = 1")
    await db.conn.commit()
    await db.close()

    db = await Database.open(str(tmp_path / "v1.db"))
    try:
        async with db.conn.execute("SELECT id, digested FROM items ORDER BY id") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [(1, 1), (2, 0)]
    finally:
        await db.close()

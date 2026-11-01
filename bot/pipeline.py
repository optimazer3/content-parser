"""Сбор → дедуп → оценка → подборка, плюс генерация постов по темам."""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from . import prompts
from .collectors import RawItem
from .collectors.telegram import TelegramCollector
from .collectors.web import WebCollector, feed_fits_page
from .config import Settings
from .db import Database, Draft, Item, Source, Topic
from .llm import fallback_digest
from .sources import classify_ref
from .text import normalize, shorten, text_hash

log = logging.getLogger(__name__)

SCORE_BATCH = 15
MAX_SCORE_BATCHES = 30


def is_stale(published_at: str | None, max_age_days: int) -> bool:
    """Пост старше max_age_days дней — не новость. Без даты считаем свежим."""
    if not published_at:
        return False
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published < datetime.now(timezone.utc) - timedelta(days=max_age_days)


class Pipeline:
    def __init__(self, db: Database, llm, web: WebCollector, tg: TelegramCollector | None, settings: Settings):
        self.db = db
        self.llm = llm
        self.web = web
        self.tg = tg
        self.settings = settings
        self._lock = asyncio.Lock()

    # --- настройки, которые меняются из бота ---

    async def interests(self) -> str:
        return await self.db.get_setting("interests") or prompts.DEFAULT_INTERESTS

    async def set_interests(self, text: str) -> None:
        await self.db.set_setting("interests", text)
        await self.db.reset_recent_scores(hours=36)

    async def style(self) -> str:
        return await self.db.get_setting("style") or ""

    async def int_setting(self, key: str) -> int:
        value = await self.db.get_setting(key)
        return int(value) if value else int(getattr(self.settings, key))

    async def digest_times(self) -> str:
        return await self.db.get_setting("digest_times") or self.settings.digest_times

    async def owner_ids(self) -> list[int]:
        """Аккаунты, которым бот отвечает: владелец (из .env или первый нажавший /start) и добавленные им."""
        owners = self.settings.owner_ids or [int(v) for v in [await self.db.get_setting("owner_id")] if v]
        extra = [int(v) for v in (await self.db.get_setting("extra_users") or "").split(",") if v]
        return list(dict.fromkeys(owners + extra))

    async def add_user(self, user_id: int) -> bool:
        """False — у аккаунта уже есть доступ."""
        if user_id in await self.owner_ids():
            return False
        extra = [v for v in (await self.db.get_setting("extra_users") or "").split(",") if v]
        await self.db.set_setting("extra_users", ",".join(extra + [str(user_id)]))
        return True

    async def remove_user(self, user_id: int) -> bool:
        """Убирает добавленный аккаунт. Главного владельца так не убрать — False."""
        extra = [v for v in (await self.db.get_setting("extra_users") or "").split(",") if v]
        if str(user_id) not in extra:
            return False
        await self.db.set_setting("extra_users", ",".join(v for v in extra if v != str(user_id)))
        return True

    # --- источники ---

    async def add_source(self, raw: str) -> str:
        ref = classify_ref(raw)
        if ref.kind == "bad":
            return f"❌ {raw} — не понял, что это. Пришли @канал, ссылку t.me/… или адрес сайта"
        if ref.kind == "tg_invite":
            return (f"⚠️ {raw} — по ссылке-приглашению добавить нельзя. Подпишись на канал аккаунтом, "
                    "который читает каналы, и перешли мне любой пост оттуда")
        if ref.kind == "tg":
            if not self.tg:
                return f"❌ {raw} — чтение каналов не настроено (TG_API_ID/TG_API_HASH в .env и python -m bot.login)"
            try:
                canonical, title = await self.tg.resolve(ref.value)
            except LookupError as e:
                return f"❌ {raw} — {e}"
            added = await self.db.add_source("tg", canonical, title)
            return f"✅ Канал «{title}»" if added else f"ℹ️ Канал «{title}» уже есть"
        try:
            title, feed_url = await self.web.discover(ref.value)
        except LookupError as e:
            return f"❌ {raw} — {e}"
        added = await self.db.add_source("web", ref.value, title, feed_url)
        mode = "RSS" if feed_url else "без RSS, буду читать страницу"
        return f"✅ Сайт «{title}» ({mode})" if added else f"ℹ️ Сайт «{title}» уже есть"

    # --- сбор ---

    async def collect(self) -> int:
        """Собирает новое со всех источников и оценивает. Возвращает число новых постов."""
        async with self._lock:
            await self._recheck_site_feeds()
            sources = await self.db.list_sources()
            new = 0

            async def from_site(source: Source) -> int:
                try:
                    items, attempted = await self.web.fetch_new(source, self.db.known_urls)
                except Exception as e:
                    log.warning("Сайт %s не прочитался: %s", source.ref, e)
                    return 0
                saved = await self._save(source, items)
                await self.db.mark_seen(attempted)
                if not source.last_seen:
                    await self.db.set_last_seen(source.id, 1)
                return saved

            sites = [s for s in sources if s.kind == "web"]
            new += sum(await asyncio.gather(*(from_site(s) for s in sites)))

            channels = [s for s in sources if s.kind == "tg"]
            if self.tg and channels:
                async for source, items, last_seen in self.tg.fetch_all(channels):
                    new += await self._save(source, items)
                    if last_seen and last_seen != source.last_seen:
                        await self.db.set_last_seen(source.id, last_seen)

            log.info("Собрано новых постов: %s", new)
            await self.score_pending()
            return new

    async def _save(self, source: Source, items: list[RawItem]) -> int:
        max_age = self.settings.max_age_days
        saved = 0
        for item in items:
            if is_stale(item.published_at, max_age):
                continue
            if await self.db.add_item(source.id, item.url, text_hash(item.text), item.title, item.text, item.published_at):
                saved += 1
        return saved

    async def _recheck_site_feeds(self) -> None:
        """Раньше для страницы раздела/тега бот мог взять общий RSS всего сайта. Перепроверяем такие источники один раз."""
        if await self.db.get_setting("site_feeds_checked") == "1":
            return
        for source in await self.db.list_sources():
            if source.kind != "web" or not source.feed_url or feed_fits_page(source.ref, source.feed_url):
                continue
            try:
                _, feed_url = await self.web.discover(source.ref)
            except LookupError as e:
                log.warning("Не удалось перепроверить сайт %s: %s", source.ref, e)
                continue
            log.info("Сайт %s: вместо общего RSS %s читаю %s", source.ref, source.feed_url, feed_url or "саму страницу")
            await self.db.set_feed_url(source.id, feed_url)
        await self.db.set_setting("site_feeds_checked", "1")

    async def score_pending(self) -> None:
        interests = await self.interests()
        disliked = await self.db.disliked_titles()
        for _ in range(MAX_SCORE_BATCHES):
            batch = await self.db.unscored_items(SCORE_BATCH)
            if not batch:
                return
            try:
                scores = await self.llm.score_batch(batch, interests, disliked)
            except Exception as e:
                log.warning("Оценка не удалась, повторю в следующий раз: %s", e)
                return
            for item in batch:
                s = scores.get(item.id)
                if s:
                    await self.db.save_score(item.id, s["score"], s["title"], s["summary"], s["topic_key"])
                else:
                    # модель пропустила пост — не застреваем на нём навсегда
                    await self.db.save_score(item.id, 0, "", "", "")

    # --- подборка ---

    async def make_digest(self) -> list[Topic]:
        size = await self.int_setting("digest_size")
        candidates = await self.db.digest_candidates(await self.int_setting("min_score"))
        if not candidates:
            return []
        try:
            picks = await self.llm.select_digest(candidates, size, await self.interests(),
                                                 await self.db.recent_topic_titles())
        except Exception as e:
            log.warning("Подборка через нейросеть не удалась, беру лучшие по оценке: %s", e)
            picks = []
        if not picks:
            picks = fallback_digest(candidates, size)
        topics = []
        for pick in picks:
            topic_id = await self.db.add_topic(pick["title"], pick["summary"], pick["item_ids"])
            topics.append(await self.db.get_topic(topic_id))
        # всё, что рассматривали, свой шанс использовало — в следующей подборке только новое
        await self.db.mark_digested([i.id for i in candidates])
        await self.db.mark_topics_sent([t.id for t in topics])
        return topics

    # --- материалы ---

    async def materials(self, topic: Topic) -> list[Item]:
        items = {i.id: i for i in await self.db.get_items(topic.item_ids)}
        return [items[i] for i in topic.item_ids if i in items]

    async def search(self, query: str, limit: int = 5) -> list[Item]:
        """Грубый поиск по свежим постам: совпадения по основам слов."""
        stems = {w[:5] for w in normalize(query).split() if len(w) >= 4}
        if not stems:
            return []
        ranked = []
        for item in await self.db.recent_items():
            haystack = normalize(f"{item.title or ''} {item.summary or ''} {item.text[:1500]}")
            hits = sum(1 for s in stems if s in haystack)
            if hits:
                ranked.append((hits, item.score or 0, item))
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        need = 1 if len(stems) == 1 else 2
        return [item for hits, _, item in ranked if hits >= min(need, len(stems))][:limit]

    async def manual_topic(self, text: str, url: str | None = None, title: str | None = None) -> Topic:
        """Тема из присланного вручную текста или статьи."""
        h = text_hash(text)
        item_id = await self.db.item_id_by_hash(h)
        if item_id is None:
            item_id = await self.db.add_item(None, url or f"manual:{h}", h, title, text, None)
        if item_id is None:  # такая ссылка уже собрана, но текст вытащился иначе
            item_id = await self.db.add_item(None, f"manual:{h}", h, title, text, None)
        if not title:
            title = re.sub(r"<[^>]+>", "", next((line for line in text.splitlines() if line.strip()), text))
        topic_id = await self.db.add_topic(shorten(title, 90), None, [item_id])
        return await self.db.get_topic(topic_id)

    async def fetch_article(self, url: str) -> RawItem | None:
        return await self.web.fetch_article(url)

    # --- генерация ---

    async def rewrite(self, topic: Topic, instruction: str | None = None) -> str:
        materials = await self.materials(topic)
        if not materials:
            raise LookupError("исходный текст не найден")
        return await self.llm.rewrite(materials[0].text, await self.style(), instruction)

    async def write_post(self, topic: Topic, instruction: str | None = None) -> str:
        subject = topic.title + (f". {topic.summary}" if topic.summary else "")
        return await self.llm.write_post(subject, await self.materials(topic), await self.style(), instruction)

    async def free_topic(self, query: str) -> Topic:
        """Тема для «напиши пост про …»: подбираем материалы из базы."""
        found = await self.search(query)
        topic_id = await self.db.add_topic(shorten(query, 120), None, [i.id for i in found])
        return await self.db.get_topic(topic_id)

    async def explain(self, topic: Topic) -> str:
        return await self.llm.explain(topic.title, await self.materials(topic))

    async def refine(self, draft: Draft, instruction: str) -> str:
        topic = await self.db.get_topic(draft.topic_id) if draft.topic_id else None
        materials = await self.materials(topic) if topic else []
        return await self.llm.refine(draft.text, instruction, await self.style(), materials)

"""Чтение каналов через userbot (Telethon) — от имени аккаунта пользователя."""

import asyncio
import logging

from telethon import TelegramClient, errors
from telethon.tl.types import Channel, Chat, Message

from . import RawItem
from ..db import Source

log = logging.getLogger(__name__)

FIRST_FETCH = 15  # сколько последних постов брать при добавлении канала
MAX_FETCH = 100
MIN_TEXT = 40  # совсем короткие посты («🔥🔥🔥», «Анонс завтра») не интересны


class TelegramCollector:
    def __init__(self, client: TelegramClient):
        self.client = client
        self._dialogs_loaded = False

    async def _entity(self, ref: str):
        target = int(ref[3:]) if ref.startswith("id:") else ref
        try:
            return await self.client.get_entity(target)
        except ValueError:
            # приватный канал по id: Telethon знает его только после загрузки списка диалогов
            if isinstance(target, int) and not self._dialogs_loaded:
                await self.client.get_dialogs()
                self._dialogs_loaded = True
                return await self.client.get_entity(target)
            raise

    async def resolve(self, ref: str) -> tuple[str, str]:
        """Проверяет, что канал доступен. Возвращает (ref для базы, название)."""
        try:
            entity = await self._entity(ref)
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
            raise LookupError("канал не найден")
        except errors.ChannelPrivateError:
            raise LookupError("канал закрытый — подпишись на него аккаунтом, который читает каналы")
        if not isinstance(entity, (Channel, Chat)):
            raise LookupError("это не канал и не группа")
        username = getattr(entity, "username", None)
        canonical = username.lower() if username else f"id:-100{entity.id}"
        return canonical, entity.title

    async def fetch_new(self, source: Source) -> tuple[list[RawItem], int | None]:
        """Новые посты с прошлого раза и id последнего сообщения."""
        entity = await self._entity(source.ref)
        if source.last_seen:
            messages = await self.client.get_messages(entity, limit=MAX_FETCH, min_id=source.last_seen)
        else:
            messages = await self.client.get_messages(entity, limit=FIRST_FETCH)

        last_seen = source.last_seen
        items = []
        username = getattr(entity, "username", None)
        for msg in messages:
            if not isinstance(msg, Message):
                continue
            last_seen = max(last_seen or 0, msg.id)
            text = (msg.message or "").strip()
            if len(text) < MIN_TEXT:
                continue
            url = f"https://t.me/{username}/{msg.id}" if username else f"https://t.me/c/{entity.id}/{msg.id}"
            items.append(RawItem(url=url, text=text, published_at=msg.date.isoformat() if msg.date else None))
        items.reverse()  # от старых к новым
        return items, last_seen

    async def fetch_all(self, sources: list[Source]):
        """Обходит каналы по очереди с паузой, чтобы не словить FloodWait."""
        for source in sources:
            try:
                items, last_seen = await self.fetch_new(source)
                yield source, items, last_seen
            except errors.FloodWaitError as e:
                log.warning("FloodWait %s сек на %s", e.seconds, source.ref)
                if e.seconds > 120:
                    return
                await asyncio.sleep(e.seconds)
            except Exception:
                log.exception("Не удалось прочитать канал %s", source.ref)
            await asyncio.sleep(1)

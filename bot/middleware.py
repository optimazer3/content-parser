from aiogram import BaseMiddleware
from aiogram.types import Update


class OwnerOnly(BaseMiddleware):
    """Бот личный: отвечает только владельцу. Если владелец не задан — им становится первый, кто нажал /start."""

    async def __call__(self, handler, event: Update, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return None
        pipeline = data["pipeline"]
        owner = await pipeline.owner_id()
        is_start = bool(event.message and (event.message.text or "").startswith("/start"))
        if not owner and is_start:
            await pipeline.db.set_setting("owner_id", str(user.id))
            owner = user.id
        if user.id != owner:
            if is_start:
                await event.message.answer("Это личный бот, он отвечает только своему владельцу.")
            return None
        return await handler(event, data)


class CommandResetsState(BaseMiddleware):
    """Любая команда отменяет ожидание ввода: иначе после /topics команда «/setting» сохранилась бы как интересы."""

    async def __call__(self, handler, event: Update, data: dict):
        state = data.get("state")
        if state and event.message and (event.message.text or "").startswith("/"):
            await state.clear()
            data["raw_state"] = None  # фильтры состояний смотрят сюда, а не в хранилище
        return await handler(event, data)

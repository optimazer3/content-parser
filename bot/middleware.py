from aiogram import BaseMiddleware
from aiogram.types import Update


class OwnerOnly(BaseMiddleware):
    """Бот личный: отвечает только аккаунтам из OWNER_ID. Если там пусто — владельцем станет первый, кто нажал /start."""

    async def __call__(self, handler, event: Update, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return None
        pipeline = data["pipeline"]
        allowed = await pipeline.owner_ids()
        is_start = bool(event.message and (event.message.text or "").startswith("/start"))
        if not allowed and is_start:
            await pipeline.db.set_setting("owner_id", str(user.id))
            allowed = [user.id]
        if user.id not in allowed:
            if is_start:
                # ID показываем, чтобы второй свой аккаунт было легко вписать в OWNER_ID
                await event.message.answer(f"Это личный бот. Твой Telegram ID: <code>{user.id}</code>")
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

import logging

from aiogram import BaseMiddleware
from aiogram.types import Update
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .text import esc
from .ui import AccessCb

log = logging.getLogger(__name__)


class OwnerOnly(BaseMiddleware):
    """Бот личный: отвечает только аккаунтам владельца. Если владелец не задан — им становится первый, кто нажал /start.

    Чужой /start получает свой ID, а владельцу приходит запрос с кнопкой «Пустить» — так добавляется второй аккаунт.
    """

    def __init__(self):
        self._asked: set[int] = set()  # кто уже просил доступ с момента запуска — не дёргаем владельца повторно

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
                await event.message.answer(f"Это личный бот. Твой Telegram ID: <code>{user.id}</code>\n"
                                           "Владельцу отправлен запрос на доступ.")
                if user.id not in self._asked:
                    self._asked.add(user.id)
                    await self._ask_owners(data["bot"], allowed, user)
            return None
        return await handler(event, data)

    @staticmethod
    async def _ask_owners(bot, owners: list[int], user) -> None:
        name = esc(user.full_name) + (f" (@{esc(user.username)})" if user.username else "")
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Пустить", callback_data=AccessCb(action="allow", user_id=user.id))
        kb.button(text="🚫 Нет", callback_data=AccessCb(action="deny", user_id=user.id))
        for owner in owners:
            try:
                await bot.send_message(owner, f"🔑 {name}, ID <code>{user.id}</code>, хочет пользоваться ботом.\n"
                                              "Если это твой второй аккаунт — нажми «Пустить».",
                                       reply_markup=kb.as_markup())
            except Exception as e:
                log.warning("Не смог отправить запрос на доступ аккаунту %s: %s", owner, e)


class CommandResetsState(BaseMiddleware):
    """Любая команда отменяет ожидание ввода: иначе после /topics команда «/setting» сохранилась бы как интересы."""

    async def __call__(self, handler, event: Update, data: dict):
        state = data.get("state")
        if state and event.message and (event.message.text or "").startswith("/"):
            await state.clear()
            data["raw_state"] = None  # фильтры состояний смотрят сюда, а не в хранилище
        return await handler(event, data)

"""Запуск: python -m bot"""

import asyncio
import logging
import sys
from contextlib import AsyncExitStack

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, ErrorEvent
from .collectors.telegram import CONNECT_ERRORS, TelegramCollector, connection_hint, make_client
from .collectors.web import WebCollector, make_http
from .config import Settings
from .db import Database
from .handlers import actions, chat, commands
from .llm import LLM
from .middleware import CommandResetsState, OwnerOnly
from .pipeline import Pipeline
from .scheduler import make_scheduler, schedule_digests

log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="digest", description="Подборка прямо сейчас"),
    BotCommand(command="add", description="Добавить канал или сайт"),
    BotCommand(command="sources", description="Список источников"),
    BotCommand(command="topics", description="Какие темы интересны"),
    BotCommand(command="style", description="Образцы моих постов"),
    BotCommand(command="time", description="Время подборок"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="users", description="Мои аккаунты с доступом"),
    BotCommand(command="help", description="Что умеет бот"),
]


async def on_error(event: ErrorEvent, bot: Bot, pipeline) -> bool:
    log.error("Ошибка при обработке апдейта", exc_info=event.exception)
    source = event.update.message or event.update.callback_query
    user = source.from_user if source else None
    if user:
        try:
            await bot.send_message(user.id, f"⚠️ Внутренняя ошибка: {event.exception.__class__.__name__}. "
                                            "Подробности — в окне, где запущен бот.", parse_mode=None)
        except Exception:
            pass
    return True


def make_dispatcher() -> Dispatcher:
    """pipeline, scheduler и settings кладутся в dp.workflow_data — их получают обработчики."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(OwnerOnly())
    dp.update.outer_middleware(CommandResetsState())
    dp.include_routers(commands.router, actions.router, chat.router)
    dp.errors.register(on_error)
    return dp


async def main() -> None:
    settings = Settings()
    if not settings.bot_token:
        sys.exit("Впиши BOT_TOKEN в файл .env (токен выдаёт @BotFather)")
    if not settings.polza_api_key:
        sys.exit("Впиши POLZA_API_KEY в файл .env (ключ из личного кабинета polza.ai)")

    async with AsyncExitStack() as stack:
        db = await Database.open(settings.db_path)
        stack.push_async_callback(db.close)
        http = await stack.enter_async_context(make_http())

        tg_client = None
        if settings.telethon_enabled:
            tg_client = make_client(settings)
            log.info("Подключаю аккаунт для чтения каналов%s…",
                     f" через прокси {settings.proxy_url}" if settings.proxy_url else "")
            try:
                await tg_client.connect()
            except CONNECT_ERRORS:
                sys.exit(connection_hint(settings))
            stack.push_async_callback(tg_client.disconnect)
            if not await tg_client.is_user_authorized():
                sys.exit("Аккаунт для чтения каналов не авторизован. Сначала выполни: python -m bot.login")
        else:
            log.warning("TG_API_ID/TG_API_HASH не заданы — читаю только сайты, без Telegram-каналов")

        llm = LLM(settings.polza_api_key, settings.llm_base_url, settings.llm_model_fast, settings.llm_model_smart)
        pipeline = Pipeline(db, llm, WebCollector(http), TelegramCollector(tg_client) if tg_client else None, settings)

        bot = Bot(
            settings.bot_token,
            session=AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None,
            default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True),
        )
        stack.push_async_callback(bot.session.close)
        scheduler = make_scheduler(settings.timezone, settings.collect_interval_min, bot, pipeline)
        schedule_digests(scheduler, await pipeline.digest_times(), bot, pipeline)

        dp = make_dispatcher()
        dp.workflow_data.update(pipeline=pipeline, scheduler=scheduler, settings=settings)

        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            sys.exit("Telegram не принял BOT_TOKEN — проверь токен от @BotFather в .env")
        except TelegramNetworkError:
            sys.exit(connection_hint(settings))
        await bot.set_my_commands(COMMANDS)
        scheduler.start()
        stack.callback(scheduler.shutdown, wait=False)
        log.info("Бот @%s запущен. Подборки в %s. Остановить — Ctrl+C", me.username, await pipeline.digest_times())
        await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

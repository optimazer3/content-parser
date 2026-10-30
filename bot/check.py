"""Проверка связи без запуска бота: python -m bot.check

Проверяет по очереди: доступ к Telegram для чтения каналов, Bot API (токен бота) и Polza.ai.
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from .collectors.telegram import CONNECT_ERRORS, connection_hint, make_client
from .config import Settings
from .llm import LLM
from .ui import error_text


async def check_userbot(settings: Settings) -> bool:
    if not settings.telethon_enabled:
        print("— Чтение каналов: TG_API_ID / TG_API_HASH не заданы, пропускаю")
        return True
    client = make_client(settings, retries=1)
    try:
        await asyncio.wait_for(client.connect(), 60)
    except CONNECT_ERRORS:
        print("❌ Чтение каналов: Telegram не отвечает\n")
        print(connection_hint(settings))
        return False
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"✅ Чтение каналов: связь есть, вошли как {me.first_name}")
        else:
            print("✅ Чтение каналов: связь есть. Осталось войти в аккаунт: python -m bot.login")
    finally:
        await client.disconnect()
    return True


async def check_bot(settings: Settings) -> bool:
    if not settings.bot_token:
        print("❌ Бот: BOT_TOKEN не задан в .env")
        return False
    bot = Bot(settings.bot_token, session=AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None)
    try:
        me = await bot.get_me()
        print(f"✅ Бот: @{me.username}")
        return True
    except TelegramUnauthorizedError:
        print("❌ Бот: Telegram не принял BOT_TOKEN — проверь токен от @BotFather")
    except TelegramNetworkError as e:
        print(f"❌ Бот: нет связи с api.telegram.org ({e.message})")
    finally:
        await bot.session.close()
    return False


async def check_polza(settings: Settings) -> bool:
    if not settings.polza_api_key:
        print("❌ Нейросеть: POLZA_API_KEY не задан в .env")
        return False
    llm = LLM(settings.polza_api_key, settings.llm_base_url, settings.llm_model_fast, settings.llm_model_smart)
    ok = True
    for model in dict.fromkeys((settings.llm_model_fast, settings.llm_model_smart)):
        try:
            await llm.chat(model, "Отвечай одним словом.", "Скажи «готово».")
            print(f"✅ Нейросеть: {model} отвечает")
        except Exception as e:
            print(f"❌ Нейросеть: {model} — {error_text(e)}")
            ok = False
    return ok


async def main() -> None:
    settings = Settings()
    proxy = settings.proxy_url
    print(f"Прокси для Telegram: {proxy or 'нет, подключаюсь напрямую'} (TG_PROXY={settings.tg_proxy})\n")
    results = [await check_userbot(settings), await check_bot(settings), await check_polza(settings)]
    print("\nВсё в порядке, можно запускать бота." if all(results) else "\nИсправь пункты с ❌ и запусти проверку ещё раз.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())

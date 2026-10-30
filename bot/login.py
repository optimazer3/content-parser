"""Одноразовый вход в Telegram-аккаунт, который будет читать каналы: python -m bot.login"""

import asyncio
import os
import sys
from getpass import getpass

from .collectors.telegram import CONNECT_ERRORS, connection_hint, make_client
from .config import Settings


async def main() -> None:
    # --if-needed: для run.bat/run.sh — ничего не спрашивать, если вход уже выполнен или каналы не настроены
    if_needed = "--if-needed" in sys.argv
    settings = Settings()
    if not settings.telethon_enabled:
        if if_needed:
            return
        sys.exit("Впиши TG_API_ID и TG_API_HASH в .env (берутся на my.telegram.org → API development tools)")
    os.makedirs(os.path.dirname(settings.tg_session) or ".", exist_ok=True)
    client = make_client(settings, retries=2)
    proxy = settings.proxy_url
    print(f"Подключаюсь к Telegram{' через прокси ' + proxy if proxy else ''}…")
    try:
        await client.connect()
    except CONNECT_ERRORS:
        sys.exit("\n" + connection_hint(settings))
    try:
        if if_needed and await client.is_user_authorized():
            return
        await client.start(
            phone=lambda: input("Номер телефона аккаунта (например +79991234567): "),
            code_callback=lambda: input("Код, который пришёл в Telegram: "),
            password=lambda: getpass("Пароль двухэтапной проверки (если есть): "),
        )
        me = await client.get_me()
        print(f"\n✅ Вошли как {me.first_name} (@{me.username or 'без юзернейма'}).")
        print(f"Сессия сохранена в {settings.tg_session}.session — никому не передавай этот файл.")
        print("Теперь запускай бота: python -m bot")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

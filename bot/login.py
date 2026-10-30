"""Одноразовый вход в Telegram-аккаунт, который будет читать каналы: python -m bot.login"""

import asyncio
import os
import sys
from getpass import getpass

from telethon import TelegramClient

from .config import Settings, telethon_proxy


async def main() -> None:
    settings = Settings()
    if not settings.telethon_enabled:
        sys.exit("Впиши TG_API_ID и TG_API_HASH в .env (берутся на my.telegram.org → API development tools)")
    os.makedirs(os.path.dirname(settings.tg_session) or ".", exist_ok=True)
    client = TelegramClient(settings.tg_session, settings.tg_api_id, settings.tg_api_hash,
                            proxy=telethon_proxy(settings.tg_proxy))
    await client.start(
        phone=lambda: input("Номер телефона аккаунта (например +79991234567): "),
        code_callback=lambda: input("Код, который пришёл в Telegram: "),
        password=lambda: getpass("Пароль двухэтапной проверки (если есть): "),
    )
    me = await client.get_me()
    print(f"\n✅ Вошли как {me.first_name} (@{me.username or 'без юзернейма'}).")
    print(f"Сессия сохранена в {settings.tg_session}.session — никому не передавай этот файл.")
    print("Теперь запускай бота: python -m bot")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

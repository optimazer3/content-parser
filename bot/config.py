from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из файла .env (или переменных окружения)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    # Бот от @BotFather
    bot_token: str = ""
    # Telegram ID владельца. Если пусто — владельцем станет первый, кто напишет /start
    owner_id: int = 0

    # Userbot (my.telegram.org → API development tools)
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = "data/userbot"
    # Прокси для Telegram, например socks5://user:pass@host:1080 (необязательно)
    tg_proxy: str = ""

    # Нейросеть через Polza.ai (OpenAI-совместимый API)
    polza_api_key: str = ""
    llm_base_url: str = "https://polza.ai/api/v1"
    llm_model_fast: str = "openai/gpt-4.1-mini"  # оценка и отбор постов
    llm_model_smart: str = "openai/gpt-5.4"  # написание и уникализация

    # Расписание и отбор (можно поменять в боте командами /time и /size)
    digest_times: str = "10:00,19:00"
    digest_size: int = 7
    min_score: int = 6
    collect_interval_min: int = 30
    timezone: str = "Europe/Moscow"

    db_path: str = "data/bot.db"

    @property
    def telethon_enabled(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)


def parse_times(value: str) -> list[tuple[int, int]]:
    """'10:00, 19:30' → [(10, 0), (19, 30)]. Бросает ValueError на мусоре."""
    result = []
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        hours, _, minutes = part.partition(":")
        h, m = int(hours), int(minutes or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(part)
        result.append((h, m))
    if not result:
        raise ValueError(value)
    return sorted(set(result))


def telethon_proxy(url: str) -> dict | None:
    """Прокси в формате, который понимает Telethon (через python-socks)."""
    if not url:
        return None
    p = urlparse(url)
    return {
        "proxy_type": p.scheme.replace("socks5h", "socks5"),
        "addr": p.hostname,
        "port": p.port,
        "username": p.username,
        "password": p.password,
        "rdns": True,
    }

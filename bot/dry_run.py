"""Пробный прогон без Telegram: собрать с сайтов и показать подборку в консоли.

    python -m bot.dry_run https://habr.com/ru/rss/hubs/artificial_intelligence/articles/ https://www.opennet.ru/
    python -m bot.dry_run --write https://...   # ещё и написать пост по первой теме (нужен POLZA_API_KEY)
    python -m bot.dry_run --fake https://...    # без нейросети, оценка по ключевым словам
"""

import argparse
import asyncio
import logging

from .collectors.web import WebCollector, make_http
from .config import Settings
from .db import Database
from .fake_llm import FakeLLM
from .llm import LLM
from .pipeline import Pipeline
from .text import strip_html


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+", help="адреса сайтов или RSS")
    parser.add_argument("--fake", action="store_true", help="без нейросети")
    parser.add_argument("--write", action="store_true", help="написать пост по первой теме")
    args = parser.parse_args()

    settings = Settings()
    use_fake = args.fake or not settings.polza_api_key
    if use_fake and not args.fake:
        print("POLZA_API_KEY не задан — работаю с заглушкой вместо нейросети\n")
    llm = FakeLLM() if use_fake else LLM(settings.polza_api_key, settings.llm_base_url,
                                         settings.llm_model_fast, settings.llm_model_smart)

    db = await Database.open(":memory:")
    async with make_http() as http:
        pipeline = Pipeline(db, llm, WebCollector(http), None, settings)
        for url in args.urls:
            print(await pipeline.add_source(url))
        print("\nСобираю и оцениваю…")
        new = await pipeline.collect()
        print(f"Новых постов: {new}")
        for item in await db.recent_items():
            score = "–" if item.score is None else item.score
            print(f"  [{score:>2}] {item.title or item.url}")

        topics = await pipeline.make_digest()
        print(f"\n=== Подборка: тем {len(topics)} ===")
        for n, topic in enumerate(topics, 1):
            print(f"\n{n}. {topic.title}\n   {topic.summary or ''}")
            for item in await pipeline.materials(topic):
                print(f"   → {item.url}")

        if args.write and topics:
            print("\n=== Пост по первой теме ===\n")
            print(strip_html(await pipeline.write_post(topics[0])))
    await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())

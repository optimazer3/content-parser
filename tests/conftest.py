from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from bot.collectors import RawItem
from bot.collectors.web import WebCollector
from bot.config import Settings
from bot.db import Database
from bot.fake_llm import FakeLLM
from bot.pipeline import Pipeline

LOREM = (
    "Компания выпустила новую нейросеть, которая заметно лучше предыдущей версии в задачах программирования. "
    "Модель доступна через API, цена снизилась вдвое, а контекст вырос до миллиона токенов. "
)


def article_html(title: str, body: str) -> str:
    paragraphs = "".join(f"<p>{body} Абзац {n}.</p>" for n in range(4))
    return (f"<html><head><title>{title}</title></head><body><nav><a href='/'>Главная</a></nav>"
            f"<article><h1>{title}</h1>{paragraphs}</article><footer>© 2026</footer></body></html>")


# даты в RSS — относительно «сейчас», иначе через пару дней посты станут «старыми» и тесты сломаются
NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRESH_DATE = NOW - timedelta(hours=2)
OLD_DATE = NOW - timedelta(days=10)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>AI News</title><link>https://news.example/</link>
<item><title>OpenAI выпустила новую модель</title><link>https://news.example/2026/10/openai-model</link>
<description>Коротко: новая модель.</description><pubDate>""" + format_datetime(FRESH_DATE) + """</pubDate></item>
<item><title>Google обновила Gemini</title><link>https://news.example/2026/10/gemini-update</link>
<description><![CDATA[<p>""" + LOREM * 3 + """</p>]]></description><pubDate>""" + format_datetime(FRESH_DATE) + """</pubDate></item>
<item><title>Старая новость про нейросети</title><link>https://news.example/2026/01/old-news</link>
<description><![CDATA[<p>""" + LOREM * 3 + """</p>]]></description><pubDate>""" + format_datetime(OLD_DATE) + """</pubDate></item>
</channel></rss>"""

FEED_HOME = """<html><head><title>AI News — главная</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>Новости</body></html>"""

BLOG_HOME = """<html><head><title>Блог без RSS</title></head><body>
<a href="/">Главная</a> <a href="/about">О нас</a> <a href="/tag/ai">#ai</a> <a href="/img/logo.png">лого</a>
<a href="https://other.site/2026/10/foreign-post">чужой сайт</a>
<a href="/blog/2026/10/llm-agents-guide">Гайд по агентам</a>
<a href="/blog/how-to-use-neural-networks-at-work#comments">Нейросети на работе</a>
<a href="/blog/2026/10/llm-agents-guide">дубль</a>
</body></html>"""

# страница тега: в шапке — общий RSS всего сайта, в сайдбаре — «популярное» не по теме
TAG_PAGE = """<html><head><title>Новости с тегом «нейросеть»</title>
<link rel="alternate" type="application/rss+xml" href="/rss.xml"></head><body>
<header><a href="/news/2026/10/site-wide-header-link">Главная новость дня</a></header>
<main>
  <article><header><a href="/news/2026/10/ai-model-release">Вышла новая модель</a></header></article>
  <article><header><a href="/news/2026/10/ai-agents-office">Агенты в офисе</a></header></article>
  <article><header><a href="/news/2026/10/ai-chips-market">Чипы для ИИ</a></header></article>
  <div class="sidebar-popular"><a href="/news/2026/10/oil-prices-up">Нефть подорожала</a></div>
</main>
<aside><a href="/news/2026/10/football-results">Футбол</a></aside>
</body></html>"""

SITE_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Все новости</title>
<item><title>Нефть подорожала</title><link>https://media.example/news/2026/10/oil-prices-up</link>
<description>Цены на нефть выросли.</description></item></channel></rss>"""

PAGES = {
    "https://news.example/": FEED_HOME,
    "https://news.example/feed.xml": RSS,
    "https://news.example/2026/10/openai-model": article_html("OpenAI выпустила новую модель", LOREM),
    "https://news.example/2026/10/gemini-update": article_html("Google обновила Gemini", LOREM),
    "https://blog.example/": BLOG_HOME,
    "https://blog.example/blog/2026/10/llm-agents-guide": article_html("Гайд по LLM-агентам", LOREM),
    "https://blog.example/blog/how-to-use-neural-networks-at-work": article_html("Нейросети на работе", LOREM),
    "https://media.example/tags/neiroset/": TAG_PAGE,
    "https://media.example/rss.xml": SITE_RSS,
}


class FakeWeb:
    def __init__(self):
        self.pages = dict(PAGES)
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        body = self.pages.get(url)
        if body is None:
            return httpx.Response(404, text="not found")
        ctype = "application/rss+xml" if body.lstrip().startswith("<?xml") else "text/html; charset=utf-8"
        return httpx.Response(200, text=body, headers={"content-type": ctype})


class FakeTelegram:
    """Подмена TelegramCollector: каналы и их посты задаются словарём."""

    def __init__(self, posts: dict[str, list[str]]):
        self.posts = posts

    async def resolve(self, ref: str):
        if ref not in self.posts:
            raise LookupError("канал не найден")
        return ref, f"Канал {ref}"

    async def fetch_all(self, sources):
        for source in sources:
            texts = self.posts.get(source.ref, [])
            start = source.last_seen or 0
            items = [RawItem(url=f"https://t.me/{source.ref}/{n}", text=t)
                     for n, t in enumerate(texts, 1) if n > start]
            yield source, items, len(texts) or None


@pytest.fixture
def fake_web():
    return FakeWeb()


@pytest.fixture
async def http(fake_web):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_web.handler), follow_redirects=True) as client:
        yield client


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(str(tmp_path / "test.db"))
    yield database
    await database.close()


@pytest.fixture
def tg():
    return FakeTelegram({
        "ai_news": [
            "Вышла новая нейросеть GPT от OpenAI: модель пишет код и рассуждает лучше прошлой версии.",
            "Розыгрыш! Подпишись на канал и выиграй мерч, итоги в пятницу, всем удачи и хорошего дня!",
        ],
        "ml_digest": [
            # точный репост первого поста из другого канала — должен отсечься дедупом
            "Вышла новая нейросеть GPT от OpenAI: модель пишет код и рассуждает лучше прошлой версии.",
            "Anthropic показала Claude с новыми AI-агентами: нейросеть сама работает в браузере.",
        ],
    })


@pytest.fixture
def settings():
    return Settings(_env_file=None, bot_token="42:TEST", polza_api_key="x", owner_id="", min_score=5, digest_size=5)


@pytest.fixture
def pipeline(db, http, tg, settings):
    return Pipeline(db, FakeLLM(), WebCollector(http), tg, settings)

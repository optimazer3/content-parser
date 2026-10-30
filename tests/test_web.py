from datetime import datetime, timedelta, timezone

import pytest

from bot.collectors.web import WebCollector, article_links, feed_fits_page, parse_page
from bot.db import Source
from tests.conftest import BLOG_HOME, PAGES, TAG_PAGE


async def no_known(urls):
    return set()


def test_article_links_filters_service_pages():
    links = article_links(BLOG_HOME, "https://blog.example/")
    assert links == [
        "https://blog.example/blog/2026/10/llm-agents-guide",
        "https://blog.example/blog/how-to-use-neural-networks-at-work",
    ]


def test_parse_page_finds_feed_links():
    page = parse_page('<head><title> T </title><link rel="alternate" type="application/atom+xml" href="/atom"></head>',
                      "https://x.ru/news/")
    assert page.feeds == ["https://x.ru/atom"]
    assert page.title == "T"


async def test_discover_finds_rss_via_link_tag(http):
    title, feed = await WebCollector(http).discover("https://news.example/")
    assert (title, feed) == ("AI News — главная", "https://news.example/feed.xml")


async def test_discover_without_rss_uses_page(http):
    title, feed = await WebCollector(http).discover("https://blog.example/")
    assert (title, feed) == ("Блог без RSS", None)


async def test_discover_reports_unreachable_site(http):
    with pytest.raises(LookupError):
        await WebCollector(http).discover("https://missing.example/")


async def test_feed_reads_full_article_when_summary_is_short(http, fake_web):
    source = Source(1, "web", "https://news.example/", "AI News", "https://news.example/feed.xml", None)
    items, attempted = await WebCollector(http).fetch_new(source, no_known)
    by_url = {i.url: i for i in items}
    assert set(attempted) == set(by_url) == {
        "https://news.example/2026/10/openai-model", "https://news.example/2026/10/gemini-update",
        "https://news.example/2026/01/old-news"}
    # в RSS был только анонс — статью дочитали со страницы
    assert "миллиона токенов" in by_url["https://news.example/2026/10/openai-model"].text
    assert "https://news.example/2026/10/openai-model" in fake_web.requested
    # у второй записи полный текст уже в RSS — страницу не открывали
    assert "https://news.example/2026/10/gemini-update" not in fake_web.requested
    published = datetime.fromisoformat(by_url["https://news.example/2026/10/gemini-update"].published_at)
    assert abs(published - (datetime.now(timezone.utc) - timedelta(hours=2))) < timedelta(minutes=5)


async def test_feed_skips_known_urls(http):
    source = Source(1, "web", "https://news.example/", "AI News", "https://news.example/feed.xml", 1)

    async def known(urls):
        return {"https://news.example/2026/10/openai-model"}

    items, attempted = await WebCollector(http).fetch_new(source, known)
    assert "https://news.example/2026/10/openai-model" not in attempted
    assert sorted(i.url for i in items) == sorted(attempted)


async def test_page_mode_extracts_articles(http):
    source = Source(2, "web", "https://blog.example/", "Блог", None, None)
    items, attempted = await WebCollector(http).fetch_new(source, no_known)
    assert len(attempted) == 2
    assert {i.title for i in items} == {"Гайд по LLM-агентам", "Нейросети на работе"}
    assert all(len(i.text) > 300 for i in items)


def test_article_links_skip_header_sidebar_and_aside():
    assert article_links(TAG_PAGE, "https://media.example/tags/neiroset/") == [
        "https://media.example/news/2026/10/ai-model-release",
        "https://media.example/news/2026/10/ai-agents-office",
        "https://media.example/news/2026/10/ai-chips-market",
    ]


@pytest.mark.parametrize("page, feed, fits", [
    ("https://site.ru/", "https://site.ru/rss.xml", True),
    ("https://site.ru/tags/neiroset/", "https://site.ru/rss.xml", False),
    ("https://site.ru/tags/neiroset/", "https://site.ru/tags/neiroset/feed/", True),
    ("https://site.ru/tags/neiroset/", "https://site.ru/rss/tags/neiroset", True),
    ("https://site.ru/news/ai", "https://site.ru/rss?section=ai", True),
])
def test_feed_fits_page(page, feed, fits):
    assert feed_fits_page(page, feed) is fits


async def test_tag_page_does_not_switch_to_site_wide_rss(http):
    # на странице тега есть ссылка только на общий RSS сайта — он не подходит, читаем саму страницу
    title, feed = await WebCollector(http).discover("https://media.example/tags/neiroset/")
    assert (title, feed) == ("Новости с тегом «нейросеть»", None)


async def test_tag_page_uses_its_own_rss(http, fake_web):
    fake_web.pages["https://media.example/tags/neiroset/feed"] = PAGES["https://news.example/feed.xml"]
    _, feed = await WebCollector(http).discover("https://media.example/tags/neiroset/")
    assert feed == "https://media.example/tags/neiroset/feed"


def test_article_date_is_not_guessed_from_url():
    from bot.collectors.web import extract_article
    from tests.conftest import LOREM, article_html
    item = extract_article(article_html("Статья", LOREM), "https://site.ru/news/2026/01/some-article")
    assert item is not None and item.published_at is None
    with_meta = article_html("Статья", LOREM).replace(
        "<head>", '<head><meta property="article:published_time" content="2026-10-29T10:00:00+03:00">')
    assert extract_article(with_meta, "https://site.ru/a").published_at.startswith("2026-10-29")

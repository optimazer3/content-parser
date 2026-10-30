import pytest

from bot.collectors.web import WebCollector, article_links, parse_page
from bot.db import Source
from tests.conftest import BLOG_HOME


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
        "https://news.example/2026/10/openai-model", "https://news.example/2026/10/gemini-update"}
    # в RSS был только анонс — статью дочитали со страницы
    assert "миллиона токенов" in by_url["https://news.example/2026/10/openai-model"].text
    assert "https://news.example/2026/10/openai-model" in fake_web.requested
    # у второй записи полный текст уже в RSS — страницу не открывали
    assert "https://news.example/2026/10/gemini-update" not in fake_web.requested
    assert by_url["https://news.example/2026/10/gemini-update"].published_at == "2026-10-29T12:00:00+00:00"


async def test_feed_skips_known_urls(http):
    source = Source(1, "web", "https://news.example/", "AI News", "https://news.example/feed.xml", 1)

    async def known(urls):
        return {"https://news.example/2026/10/openai-model"}

    items, attempted = await WebCollector(http).fetch_new(source, known)
    assert [i.url for i in items] == attempted == ["https://news.example/2026/10/gemini-update"]


async def test_page_mode_extracts_articles(http):
    source = Source(2, "web", "https://blog.example/", "Блог", None, None)
    items, attempted = await WebCollector(http).fetch_new(source, no_known)
    assert len(attempted) == 2
    assert {i.title for i in items} == {"Гайд по LLM-агентам", "Нейросети на работе"}
    assert all(len(i.text) > 300 for i in items)

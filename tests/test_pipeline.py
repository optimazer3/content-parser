from tests.conftest import PAGES


async def add_all(pipeline):
    return [await pipeline.add_source(ref) for ref in ("@ai_news", "https://t.me/ml_digest", "news.example", "@nope_channel")]


async def test_add_sources(pipeline):
    results = await add_all(pipeline)
    assert results[0] == "✅ Канал «Канал ai_news»"
    assert results[2] == "✅ Сайт «AI News — главная» (RSS)"
    assert results[3].startswith("❌")
    assert (await pipeline.add_source("@ai_news")).startswith("ℹ️")
    assert (await pipeline.add_source("https://t.me/+invite")).startswith("⚠️")
    assert len(await pipeline.db.list_sources()) == 3


async def test_add_channel_without_telethon(pipeline):
    pipeline.tg = None
    assert "не настроено" in await pipeline.add_source("@ai_news")


async def test_collect_dedupes_scores_and_digests_once(pipeline):
    await add_all(pipeline)
    new = await pipeline.collect()
    # 2 + 2 поста в каналах, один — точный репост, плюс 2 статьи с сайта
    assert new == 5
    items = await pipeline.db.recent_items()
    assert all(i.score is not None for i in items)

    topics = await pipeline.make_digest()
    titles = " ".join(t.title for t in topics)
    assert topics and "Розыгрыш" not in titles  # реклама получила низкую оценку
    assert all(t.item_ids for t in topics)

    # повторный сбор ничего нового не находит, повторная подборка пустая
    assert await pipeline.collect() == 0
    assert await pipeline.make_digest() == []

    sources = {s.ref: s for s in await pipeline.db.list_sources()}
    assert sources["ai_news"].last_seen == 2
    assert sources["https://news.example/"].last_seen == 1


async def test_collect_survives_broken_site(pipeline, fake_web):
    await pipeline.add_source("news.example")
    fake_web.pages.pop("https://news.example/feed.xml")
    assert await pipeline.collect() == 0


async def test_settings_from_db_override_env(pipeline):
    assert await pipeline.int_setting("digest_size") == 5
    await pipeline.db.set_setting("digest_size", "3")
    assert await pipeline.int_setting("digest_size") == 3
    assert await pipeline.digest_times() == "10:00,19:00"


async def test_search_and_free_topic(pipeline):
    await add_all(pipeline)
    await pipeline.collect()
    found = await pipeline.search("агенты Claude")
    assert found and "Claude" in found[0].text
    topic = await pipeline.free_topic("пост про агентов Claude")
    assert topic.item_ids == [i.id for i in await pipeline.search("пост про агентов Claude")]
    assert "Материалов: " in await pipeline.write_post(topic)


async def test_manual_topic_reuses_collected_item(pipeline):
    await add_all(pipeline)
    await pipeline.collect()
    text = "Вышла новая нейросеть GPT от OpenAI: модель пишет код и рассуждает лучше прошлой версии."
    topic = await pipeline.manual_topic(text)
    original = [i for i in await pipeline.db.recent_items() if i.text == text][0]
    assert topic.item_ids == [original.id]
    assert topic.title.startswith("Вышла новая нейросеть")


async def test_rewrite_and_refine(pipeline):
    topic = await pipeline.manual_topic("Свой текст про нейросети " * 20)
    assert (await pipeline.rewrite(topic)).startswith("<b>[уникализация]</b>")
    draft_id = await pipeline.db.add_draft(topic.id, "черновик")
    draft = await pipeline.db.get_draft(draft_id)
    assert await pipeline.refine(draft, "короче") == "черновик\n[правка: короче]"


async def test_next_digest_has_only_new_posts(pipeline, tg):
    await add_all(pipeline)
    await pipeline.collect()
    await pipeline.db.set_setting("digest_size", "1")
    first = await pipeline.make_digest()
    assert len(first) == 1
    # не попавшие в подборку посты уже рассмотрены — повторно не предлагаем
    assert await pipeline.make_digest() == []

    tg.posts["ai_news"].append("Google представила новую нейросеть Gemini для работы с кодом и документами.")
    await pipeline.collect()
    second = await pipeline.make_digest()
    assert [len(t.item_ids) for t in second] == [1] and "Gemini" in (await pipeline.materials(second[0]))[0].text
    # отправленные темы попадают в список «уже было» для следующей подборки
    assert await pipeline.db.recent_topic_titles() == [second[0].title, first[0].title]


async def test_old_posts_are_skipped(pipeline):
    await pipeline.add_source("news.example")
    await pipeline.collect()
    urls = {i.url for i in await pipeline.db.recent_items()}
    assert "https://news.example/2026/01/old-news" not in urls
    assert "https://news.example/2026/10/gemini-update" in urls


async def test_site_wide_feed_is_replaced_for_tag_page(pipeline, fake_web):
    # так источник мог сохраниться в старой версии: страница тега, но RSS — общий для всего сайта
    await pipeline.db.add_source("web", "https://media.example/tags/neiroset/", "Тег", "https://media.example/rss.xml")
    for n in range(3):
        fake_web.pages[f"https://media.example/news/2026/10/ai-{n}"] = PAGES["https://news.example/2026/10/openai-model"]
    await pipeline.collect()
    source = (await pipeline.db.list_sources())[0]
    assert source.feed_url is None
    assert all("oil-prices" not in i.url for i in await pipeline.db.recent_items())


async def test_new_interests_trigger_rescoring(pipeline):
    await pipeline.add_source("@ml_digest")
    await pipeline.collect()
    await pipeline.set_interests("Только локальные нейросети и железо для них")
    assert len(await pipeline.db.unscored_items()) == 2

"""Сквозные проверки бота: апдейты идут через настоящий Dispatcher, запросы к Telegram записываются."""

import itertools
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageOriginChannel, Update, User
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.__main__ import make_dispatcher
from bot.ui import ChannelCb, DraftCb, SourceCb, TopicCb

OWNER, STRANGER = 111, 222


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            msg_id = next(self._ids) if isinstance(method, SendMessage) else method.message_id
            return Message(message_id=msg_id, date=datetime.now(), chat=Chat(id=method.chat_id or OWNER, type="private"),
                           text=method.text).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError
        yield

    async def close(self):
        pass


@pytest.fixture(scope="module")
def dp():
    return make_dispatcher()


class Harness:
    def __init__(self, dp, pipeline, settings):
        self.dp, self.pipeline, self.settings = dp, pipeline, settings
        self.session = RecordingSession()
        self.bot = Bot("42:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self._ids = itertools.count(1)

    async def _feed(self, **kwargs):
        update = Update(update_id=next(self._ids), **kwargs)
        await self.dp.feed_update(self.bot, update, pipeline=self.pipeline, scheduler=self.scheduler,
                                  settings=self.settings)

    def _message(self, text, user, **extra):
        return Message(message_id=next(self._ids), date=datetime.now(), chat=Chat(id=user, type="private"),
                       from_user=User(id=user, is_bot=False, first_name="Тест"), text=text, **extra)

    async def send(self, text, user=OWNER, reply_to: int | None = None, **extra):
        if reply_to:
            extra["reply_to_message"] = Message(message_id=reply_to, date=datetime.now(),
                                                chat=Chat(id=user, type="private"), text="…")
        await self._feed(message=self._message(text, user, **extra))

    async def click(self, data, message_id=1, user=OWNER):
        cb = CallbackQuery(id=str(next(self._ids)), from_user=User(id=user, is_bot=False, first_name="Тест"),
                           chat_instance="ci", data=data.pack(),
                           message=Message(message_id=message_id, date=datetime.now(),
                                           chat=Chat(id=user, type="private"), text="карточка"))
        await self._feed(callback_query=cb)

    def sent(self, kind=(SendMessage, EditMessageText)):
        return [r for r in self.session.requests if isinstance(r, kind)]

    def texts(self):
        return [r.text for r in self.sent()]

    def last(self):
        return self.sent(SendMessage)[-1]

    def reset(self):
        self.session.requests.clear()


@pytest.fixture
async def h(dp, pipeline, settings):
    harness = Harness(dp, pipeline, settings)
    await harness.send("/start")
    harness.reset()
    return harness


async def test_first_start_claims_owner_and_strangers_are_ignored(h):
    assert await h.pipeline.owner_id() == OWNER
    await h.send("/start", user=STRANGER)
    assert h.texts() == ["Это личный бот, он отвечает только своему владельцу."]
    h.reset()
    await h.send("/digest", user=STRANGER)
    await h.send("напиши пост про GPT", user=STRANGER)
    assert h.session.requests == []


async def test_add_digest_rewrite_and_refine(h):
    await h.send("/add @ai_news https://t.me/ml_digest news.example")
    result = h.texts()[-1]
    assert "✅ Канал «Канал ai_news»" in result and "✅ Сайт «AI News — главная» (RSS)" in result

    h.reset()
    await h.send("/digest")
    cards = [r for r in h.sent(SendMessage) if r.reply_markup]
    assert any("Подборка</b> — тем:" in t for t in h.texts())
    assert cards and cards[0].text.startswith("<b>1. ")
    assert "📎 <a href=" in cards[0].text

    topic = (await h.pipeline.db._topics("msg_id IS NOT NULL", (), "ORDER BY id LIMIT 1"))[0]
    topic_msg = topic.msg_id
    assert await h.pipeline.db.topic_by_msg(topic_msg) == topic

    h.reset()
    await h.click(TopicCb(action="rewrite", id=topic.id), message_id=topic_msg)
    assert h.texts()[0] == "⏳ Уникализирую…"
    draft_msg = h.last()
    assert "[уникализация]" in draft_msg.text
    buttons = [b.text for row in draft_msg.reply_markup.inline_keyboard for b in row]
    assert buttons == ["🔁 Ещё вариант", "✂️ Короче", "➕ Подробнее"]

    draft = await h.pipeline.db.get_draft(1)
    h.reset()
    await h.click(DraftCb(action="shorter", id=draft.id))
    assert "[правка: сократи примерно вдвое" in h.last().text

    # ответ (reply) на черновик своими словами
    h.reset()
    await h.send("убери эмодзи", reply_to=draft.msg_id)
    assert "[правка: убери эмодзи]" in h.last().text

    # ответ на карточку темы — пост по теме с указанием
    h.reset()
    await h.send("с акцентом на бизнес", reply_to=topic_msg)
    assert h.pipeline.llm.calls[-1] == ("write_post", (f"{topic.title}. {topic.summary}" if topic.summary else topic.title,
                                                      len(topic.item_ids), "с акцентом на бизнес"))


async def test_dislike_removes_buttons_and_is_remembered(h):
    await h.pipeline.add_source("@ai_news")
    await h.pipeline.collect()
    topics = await h.pipeline.make_digest()
    await h.click(TopicCb(action="dislike", id=topics[0].id))
    assert h.sent(AnswerCallbackQuery)[0].text.startswith("Учту")
    assert h.sent(EditMessageReplyMarkup)
    assert await h.pipeline.db.disliked_titles() == [topics[0].title]


async def test_free_text_writes_post_from_collected_materials(h):
    await h.pipeline.add_source("@ml_digest")
    await h.pipeline.collect()
    await h.send("Напиши пост про агентов Claude")
    assert h.texts()[0] == "⏳ Пишу пост (нашёл материалов: 1)…"
    assert h.last().text.startswith("<b>[пост] агентов Claude</b>")


async def test_long_text_and_forward_offer_actions(h):
    await h.send("Мой черновик про нейросети. " * 15)
    assert h.texts() == ["Что сделать с этим текстом?"]

    h.reset()
    origin = MessageOriginChannel(type="channel", date=datetime.now(), message_id=7,
                                  chat=Chat(id=-1001234, type="channel", title="AI News", username="ai_news"))
    await h.send("Пересланный пост про новую модель OpenAI и её возможности", forward_origin=origin)
    msg = h.last()
    assert msg.text == "Что сделать с этим постом?"
    buttons = [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row]
    assert ChannelCb(ref="ai_news").pack() in buttons

    h.reset()
    await h.click(ChannelCb(ref="ai_news"))
    assert h.last().text == "✅ Канал «Канал ai_news»"


async def test_settings_commands(h):
    await h.send("/time 9:00 18:30")
    assert h.texts()[-1] == "✅ Подборки будут приходить в: 09:00,18:30"
    assert sorted(j.id for j in h.scheduler.get_jobs()) == ["digest_0900", "digest_1830"]

    await h.send("/size 3")
    assert await h.pipeline.int_setting("digest_size") == 3
    await h.send("/size много")
    assert h.texts()[-1].startswith("Тем в подборке: 3")

    await h.send("/topics")
    await h.send("Только про локальные нейросети")
    assert await h.pipeline.interests() == "Только про локальные нейросети"

    await h.send("/settings")
    assert "Тем в подборке: 3" in h.texts()[-1]


async def test_sources_list_and_delete(h):
    await h.pipeline.add_source("@ai_news")
    await h.send("/sources")
    msg = h.last()
    assert "📢 Канал ai_news — @ai_news" in msg.text
    source_id = (await h.pipeline.db.list_sources())[0].id
    await h.click(SourceCb(id=source_id))
    assert await h.pipeline.db.list_sources() == []
    assert h.sent(EditMessageText)[-1].text.startswith("Источников пока нет")

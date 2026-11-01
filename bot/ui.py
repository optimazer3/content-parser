"""Кнопки, карточки тем и отправка длинных HTML-сообщений."""

import html
import logging

import openai
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Item, Topic
from .text import esc, sanitize_html, shorten, split_message, strip_html

log = logging.getLogger(__name__)


class TopicCb(CallbackData, prefix="t"):
    action: str  # open | rewrite | write | more | dislike
    id: int


class DraftCb(CallbackData, prefix="d"):
    action: str  # again | shorter | longer
    id: int


class SourceCb(CallbackData, prefix="s"):
    id: int


class AccessCb(CallbackData, prefix="a"):
    action: str  # allow | deny | remove
    user_id: int


class ChannelCb(CallbackData, prefix="c"):
    ref: str  # username или числовой id канала (-100…)


def topic_kb(topic_id: int, full: bool = True, manual: bool = False, channel_ref: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Уникализировать", callback_data=TopicCb(action="rewrite", id=topic_id))
    kb.button(text="📝 Написать по мотивам" if manual else "📝 Написать пост",
              callback_data=TopicCb(action="write", id=topic_id))
    if full:
        kb.button(text="📖 Подробнее", callback_data=TopicCb(action="more", id=topic_id))
        kb.button(text="👎 Не интересно", callback_data=TopicCb(action="dislike", id=topic_id))
    if channel_ref:
        kb.button(text="➕ Добавить канал в источники", callback_data=ChannelCb(ref=channel_ref))
    kb.adjust(2)
    return kb.as_markup()


def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Ещё вариант", callback_data=DraftCb(action="again", id=draft_id))
    kb.button(text="✂️ Короче", callback_data=DraftCb(action="shorter", id=draft_id))
    kb.button(text="➕ Подробнее", callback_data=DraftCb(action="longer", id=draft_id))
    kb.adjust(3)
    return kb.as_markup()


def digest_kb(numbered: list[tuple[int, Topic]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n, topic in numbered:
        kb.button(text=str(n), callback_data=TopicCb(action="open", id=topic.id))
    kb.adjust(5)
    return kb.as_markup()


def _source_links(items: list[Item], limit: int) -> str:
    return " · ".join(
        f'<a href="{html.escape(i.url, quote=True)}">{esc(shorten(i.source_name or "источник", 40))}</a>'
        for i in items[:limit]
        if i.url.startswith("http")
    )


def topic_card(topic: Topic, items: list[Item]) -> str:
    lines = [f"<b>{esc(topic.title)}</b>"]
    if topic.summary:
        lines.append(esc(topic.summary))
    links = _source_links(items, 6)
    if links:
        lines.append(f"\n📎 {links}")
    return "\n".join(lines)


def digest_entry(n: int, topic: Topic, items: list[Item]) -> str:
    lines = [f"<b>{n}. {esc(topic.title)}</b>"]
    if topic.summary:
        lines.append(esc(shorten(topic.summary, 220)))
    links = _source_links(items, 3)
    if links:
        lines.append(f"📎 {links}")
    return "\n".join(lines)


async def send_html(bot: Bot, chat_id: int, text: str, reply_markup=None, sanitize: bool = True) -> Message:
    """Отправляет HTML, длинное режет на части. Если Telegram не принял разметку — шлёт чистый текст."""
    chunks = split_message(sanitize_html(text) if sanitize else text)
    sent = None
    for n, chunk in enumerate(chunks):
        markup = reply_markup if n == len(chunks) - 1 else None
        try:
            sent = await bot.send_message(chat_id, chunk, reply_markup=markup)
        except TelegramBadRequest as e:
            if "parse" not in str(e).lower() and "tag" not in str(e).lower():
                raise
            log.info("Telegram не принял HTML, отправляю текстом: %s", e)
            sent = await bot.send_message(chat_id, strip_html(chunk), parse_mode=None, reply_markup=markup)
    return sent


DIGEST_PAGE = 3500  # символов в одном сообщении подборки (лимит Telegram — 4096)


async def send_digest(bot: Bot, chat_id: int, pipeline, topics: list[Topic], header: str) -> None:
    """Подборка списком. Длинная — несколькими сообщениями, у каждого кнопки с номерами его тем."""
    head = f"🗞 <b>{esc(header)}</b> — тем: {len(topics)}\nНажми номер темы — уникализирую, напишу пост или расскажу подробнее."
    pages: list[list[tuple[int, Topic, str]]] = [[]]
    size = len(head)
    for n, topic in enumerate(topics, 1):
        entry = digest_entry(n, topic, await pipeline.materials(topic))
        if pages[-1] and size + len(entry) > DIGEST_PAGE:
            pages.append([])
            size = 0
        pages[-1].append((n, topic, entry))
        size += len(entry) + 2
    for i, page in enumerate(pages):
        text = "\n\n".join(([head] if i == 0 else []) + [entry for _, _, entry in page])
        await send_html(bot, chat_id, text, digest_kb([(n, t) for n, t, _ in page]), sanitize=False)


async def send_topic_card(bot: Bot, chat_id: int, pipeline, topic: Topic) -> None:
    msg = await send_html(bot, chat_id, topic_card(topic, await pipeline.materials(topic)), topic_kb(topic.id),
                          sanitize=False)
    await pipeline.db.set_topic_msg(topic.id, msg.message_id)


def error_text(e: Exception) -> str:
    if isinstance(e, openai.AuthenticationError):
        return "Polza не принял ключ — проверь POLZA_API_KEY в .env"
    if isinstance(e, openai.APIStatusError):
        if e.status_code == 402:
            return "на балансе Polza закончились деньги"
        if e.status_code == 404:
            return "модель не найдена — проверь LLM_MODEL_* в .env (формат openai/имя-модели)"
        return f"нейросеть ответила ошибкой {e.status_code}: {shorten(str(e.message), 300)}"
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        return "нет связи с Polza, попробуй ещё раз"
    if isinstance(e, LookupError):
        return str(e)
    return f"{e.__class__.__name__}: {shorten(str(e), 300)}"


async def generate(message: Message, pipeline, work, topic_id: int | None, as_draft: bool = True,
                   wait_text: str = "⏳ Пишу пост…") -> None:
    """Показывает «пишу…», ждёт нейросеть и присылает результат с кнопками."""
    wait = await message.answer(wait_text)
    try:
        text = await work
    except Exception as e:
        log.exception("Генерация не удалась")
        await wait.edit_text(f"❌ Не получилось: {esc(error_text(e))}")
        return
    try:
        await wait.delete()
    except TelegramBadRequest:
        pass
    if not text:
        await message.answer("❌ Нейросеть вернула пустой ответ, попробуй ещё раз")
        return
    if as_draft:
        draft_id = await pipeline.db.add_draft(topic_id, text)
        sent = await send_html(message.bot, message.chat.id, text, draft_kb(draft_id))
        await pipeline.db.set_draft_msg(draft_id, sent.message_id)
    else:
        await send_html(message.bot, message.chat.id, text, topic_kb(topic_id, full=False) if topic_id else None)

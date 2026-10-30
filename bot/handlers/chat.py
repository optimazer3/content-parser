"""Свободный режим: пересланные посты, ответы на черновики, «напиши пост про …», ссылки и тексты."""

import re

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageOriginChannel

from ..sources import classify_ref
from ..text import esc
from ..ui import ChannelCb, generate, topic_kb

router = Router()

WRITE_RE = re.compile(
    r"^\s*(напиши|сделай|создай|подготовь)\s+(мне\s+)?(пост|текст|статью|заметку)?\s*"
    r"(про|о|об|обо|на тему|по теме)?\s*[:\-—]?\s*",
    re.I,
)
URL_RE = re.compile(r"^https?://\S+$")
LONG_TEXT = 280  # длиннее — считаем, что прислали материал, а не тему


@router.message(F.forward_origin)
async def on_forward(message: Message, pipeline):
    text = (message.text or message.caption or "").strip()
    origin = message.forward_origin
    channel_ref, url = None, None
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        refs = {s.ref for s in await pipeline.db.list_sources()}
        if chat.username:
            url = f"https://t.me/{chat.username}/{origin.message_id}"
            if chat.username.lower() not in refs:
                channel_ref = chat.username.lower()
        elif f"id:{chat.id}" not in refs:
            channel_ref = str(chat.id)

    if len(text) < 20:
        if channel_ref:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="➕ Добавить канал в источники", callback_data=ChannelCb(ref=channel_ref).pack())]])
            await message.answer("В посте нет текста. Добавить канал в источники?", reply_markup=kb)
        else:
            await message.answer("В пересланном сообщении нет текста.")
        return

    topic = await pipeline.manual_topic(text, url)
    await message.answer("Что сделать с этим постом?",
                         reply_markup=topic_kb(topic.id, full=False, manual=True, channel_ref=channel_ref))


@router.message(F.reply_to_message, F.text)
async def on_reply(message: Message, pipeline):
    replied = message.reply_to_message.message_id
    draft = await pipeline.db.draft_by_msg(replied)
    if draft:
        await generate(message, pipeline, pipeline.refine(draft, message.text), draft.topic_id,
                       wait_text="⏳ Переписываю…")
        return
    topic = await pipeline.db.topic_by_msg(replied)
    if topic:
        await generate(message, pipeline, pipeline.write_post(topic, instruction=message.text), topic.id)
        return
    await on_text(message, pipeline)


@router.message(F.text | F.caption)
async def on_text(message: Message, pipeline):
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        await message.answer("Не знаю такой команды. Список — /help")
        return

    if URL_RE.match(text):
        if classify_ref(text).kind in ("tg", "tg_invite"):
            await message.answer(f"Это ссылка на Telegram. Следить за каналом — <code>/add {esc(text)}</code>\n"
                                 "Уникализировать пост — перешли его мне.")
            return
        wait = await message.answer("⏳ Читаю статью…")
        article = await pipeline.fetch_article(text)
        if not article:
            await wait.edit_text("❌ Не смог вытащить текст статьи с этой страницы. Скопируй текст и пришли сообщением.")
            return
        topic = await pipeline.manual_topic(article.text, text, article.title)
        await wait.edit_text(f"Статья: <b>{esc(topic.title)}</b>\nЧто с ней сделать?",
                             reply_markup=topic_kb(topic.id, full=False, manual=True))
        return

    if len(text) >= LONG_TEXT and not WRITE_RE.match(text):
        topic = await pipeline.manual_topic(text)
        await message.answer("Что сделать с этим текстом?", reply_markup=topic_kb(topic.id, full=False, manual=True))
        return

    query = WRITE_RE.sub("", text).strip() or text
    topic = await pipeline.free_topic(query)
    note = (f"нашёл материалов: {len(topic.item_ids)}" if topic.item_ids
            else "свежих материалов в базе нет, пишу из общих знаний")
    await generate(message, pipeline, pipeline.write_post(topic), topic.id, wait_text=f"⏳ Пишу пост ({note})…")

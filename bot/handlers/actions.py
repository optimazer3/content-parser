from aiogram import Router
from aiogram.types import CallbackQuery

from .. import prompts
from ..text import esc
from ..ui import ChannelCb, DraftCb, TopicCb, generate, send_topic_card

router = Router()


@router.callback_query(TopicCb.filter())
async def on_topic(cb: CallbackQuery, callback_data: TopicCb, pipeline):
    topic = await pipeline.db.get_topic(callback_data.id)
    if not topic:
        await cb.answer("Тема не найдена", show_alert=True)
        return

    if callback_data.action == "dislike":
        await pipeline.db.set_feedback(topic.id, -1)
        await cb.answer("Учту — похожие темы буду оценивать ниже")
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    await cb.answer()
    if callback_data.action == "open":
        await send_topic_card(cb.message.bot, cb.message.chat.id, pipeline, topic)
    elif callback_data.action == "rewrite":
        await generate(cb.message, pipeline, pipeline.rewrite(topic), topic.id, wait_text="⏳ Уникализирую…")
    elif callback_data.action == "write":
        await generate(cb.message, pipeline, pipeline.write_post(topic), topic.id)
    elif callback_data.action == "more":
        await generate(cb.message, pipeline, pipeline.explain(topic), topic.id, as_draft=False,
                       wait_text="⏳ Разбираюсь в деталях…")


@router.callback_query(DraftCb.filter())
async def on_draft(cb: CallbackQuery, callback_data: DraftCb, pipeline):
    draft = await pipeline.db.get_draft(callback_data.id)
    if not draft:
        await cb.answer("Черновик не найден", show_alert=True)
        return
    await cb.answer()
    await generate(cb.message, pipeline, pipeline.refine(draft, prompts.PRESETS[callback_data.action]),
                   draft.topic_id, wait_text="⏳ Переписываю…")


@router.callback_query(ChannelCb.filter())
async def on_add_channel(cb: CallbackQuery, callback_data: ChannelCb, pipeline):
    ref = callback_data.ref
    result = await pipeline.add_source(f"https://t.me/c/{ref[4:]}/1" if ref.startswith("-100") else f"@{ref}")
    await cb.answer()
    await cb.message.answer(esc(result))

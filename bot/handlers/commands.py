from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import parse_times
from ..scheduler import run_digest, schedule_digests
from ..sources import split_refs
from ..text import esc, shorten
from ..ui import AccessCb, SourceCb, error_text, send_html

router = Router()

HELP = """<b>Что я умею</b>

Два раза в день (по умолчанию в 10:00 и 19:00) присылаю подборку интересных тем из твоих каналов и сайтов. Под каждой темой есть кнопки:
✍️ <b>Уникализировать</b> — перепишу пост своими словами
📝 <b>Написать пост</b> — новый пост по теме на основе источников
📖 <b>Подробнее</b> — расскажу детали
👎 <b>Не интересно</b> — такие темы буду оценивать ниже

Под готовым постом: 🔁 ещё вариант, ✂️ короче, ➕ подробнее. Или <b>ответь (reply) на пост</b> своими словами: «убери эмодзи», «добавь вывод», «сделай в виде списка».

<b>Ещё можно</b>
• написать «пост про Gemini 3» — напишу пост по свежим материалам из базы
• переслать пост или прислать текст/ссылку на статью — уникализирую

<b>Команды</b>
/add — добавить канал или сайт: <code>/add @channel https://site.ru</code>
/sources — список источников (с удалением)
/digest — подборка прямо сейчас
/topics — какие темы тебе интересны
/style — образцы твоих постов, чтобы писать в твоём стиле
/time — время подборок: <code>/time 9:00 18:30</code>
/size — сколько тем в подборке: <code>/size все</code> (по умолчанию) или <code>/size 10</code>
/threshold — минимальная оценка темы 0–10: <code>/threshold 6</code>
/settings — текущие настройки
/users — твои аккаунты с доступом к боту (например, второй аккаунт)"""


class Form(StatesGroup):
    interests = State()
    style = State()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(f"Привет! Я буду следить за каналами и сайтами про нейросети и IT.\n"
                         f"Твой Telegram ID: <code>{message.from_user.id}</code>\n\n"
                         "Начни с добавления источников: <code>/add @channel https://site.ru</code>\n\n" + HELP)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил.")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, pipeline):
    refs = split_refs(command.args or "")
    if not refs:
        await message.answer("Пришли источники после команды, можно несколько сразу:\n"
                             "<code>/add @neural_channel https://t.me/another https://habr.com/ru/hubs/artificial_intelligence/</code>\n\n"
                             "Закрытый канал: подпишись на него аккаунтом, который читает каналы, и перешли мне любой пост оттуда.")
        return
    wait = await message.answer("⏳ Проверяю…")
    results = [await pipeline.add_source(ref) for ref in refs]
    await wait.edit_text(esc("\n".join(results)) + "\n\nНовые посты подтянутся при следующем сборе или по /digest")


def _sources_view(sources) -> tuple[str, object]:
    if not sources:
        return "Источников пока нет. Добавь: <code>/add @channel https://site.ru</code>", None
    lines, kb = ["<b>Источники</b> (нажми, чтобы удалить):"], InlineKeyboardBuilder()
    for n, s in enumerate(sources, 1):
        icon = "📢" if s.kind == "tg" else ("📰" if s.feed_url else "🌐")
        ref = f"@{s.ref}" if s.kind == "tg" and not s.ref.startswith("id:") else s.ref
        lines.append(f"{n}. {icon} {esc(s.name)} — {esc(ref)}")
        kb.button(text=f"❌ {n}. {shorten(s.name, 28)}", callback_data=SourceCb(id=s.id))
    kb.adjust(2)
    return "\n".join(lines), kb.as_markup()


@router.message(Command("sources"))
async def cmd_sources(message: Message, pipeline):
    text, kb = _sources_view(await pipeline.db.list_sources())
    await send_html(message.bot, message.chat.id, text, kb, sanitize=False)


@router.callback_query(SourceCb.filter())
async def on_delete_source(cb: CallbackQuery, callback_data: SourceCb, pipeline):
    removed = await pipeline.db.delete_source(callback_data.id)
    await cb.answer(f"Удалил: {removed.name}" if removed else "Уже удалён")
    text, kb = _sources_view(await pipeline.db.list_sources())
    with suppress(TelegramBadRequest):  # «message is not modified» при двойном нажатии
        await cb.message.edit_text(text, reply_markup=kb)


@router.message(Command("digest"))
async def cmd_digest(message: Message, bot: Bot, pipeline):
    if not await pipeline.db.list_sources():
        await message.answer("Сначала добавь источники: <code>/add @channel https://site.ru</code>")
        return
    wait = await message.answer("⏳ Собираю свежие посты и отбираю темы, это займёт минуту-две…")
    try:
        count = await run_digest(bot, pipeline, "Подборка", chat_ids=[message.chat.id])
    except Exception as e:
        await wait.edit_text(f"❌ Не получилось: {esc(error_text(e))}")
        return
    if count:
        await wait.delete()
    else:
        await wait.edit_text("Новых интересных тем пока нет. Если так всегда — снизь порог: <code>/threshold 5</code>")


@router.message(Command("topics"))
async def cmd_topics(message: Message, command: CommandObject, state: FSMContext, pipeline):
    if command.args:
        await _save_interests(message, pipeline, command.args)
        return
    await state.set_state(Form.interests)
    await message.answer(f"<b>Сейчас я отбираю темы так:</b>\n{esc(await pipeline.interests())}\n\n"
                         "Пришли новое описание одним сообщением: что интересно и что не интересно. /cancel — оставить как есть.")


async def _save_interests(message: Message, pipeline, text: str) -> bool:
    text = text.strip()
    if len(text) < 15:
        await message.answer("Слишком коротко. Опиши 1–2 предложениями, какие темы нужны и какие нет. /cancel — оставить как есть.")
        return False
    await pipeline.set_interests(text)
    await message.answer("✅ Интересы обновил. Посты за последние сутки переоценю по ним при следующем сборе.")
    return True


@router.message(Form.interests, F.text)
async def set_interests(message: Message, state: FSMContext, pipeline):
    if await _save_interests(message, pipeline, message.text):
        await state.clear()


@router.message(Command("style"))
async def cmd_style(message: Message, command: CommandObject, state: FSMContext, pipeline):
    if command.args and command.args.strip().lower() in ("сброс", "reset", "-"):
        await pipeline.db.set_setting("style", "")
        await message.answer("✅ Стиль сброшен.")
        return
    current = await pipeline.style()
    await state.set_state(Form.style)
    await message.answer(
        (f"<b>Сейчас образец стиля такой:</b>\n{esc(shorten(current, 1500))}\n\n" if current else "") +
        "Пришли одним сообщением 3–5 своих постов (раздели их строкой ---) и, если хочешь, пару слов об аудитории и тоне. "
        "/style сброс — убрать образец, /cancel — оставить как есть."
    )


@router.message(Form.style, F.text)
async def set_style(message: Message, state: FSMContext, pipeline):
    await pipeline.db.set_setting("style", message.text.strip())
    await state.clear()
    await message.answer("✅ Запомнил стиль, буду писать похоже.")


@router.message(Command("time"))
async def cmd_time(message: Message, command: CommandObject, pipeline, scheduler):
    if not command.args:
        await message.answer(f"Подборки приходят в: {esc(await pipeline.digest_times())}\n"
                             "Поменять: <code>/time 9:00 18:30</code>")
        return
    try:
        times = parse_times(command.args)
    except ValueError:
        await message.answer("Не понял время. Пример: <code>/time 9:00 18:30</code>")
        return
    value = ",".join(f"{h:02d}:{m:02d}" for h, m in times)
    await pipeline.db.set_setting("digest_times", value)
    schedule_digests(scheduler, value, message.bot, pipeline)
    await message.answer(f"✅ Подборки будут приходить в: {value}")


async def _int_command(message: Message, command: CommandObject, pipeline, key: str, low: int, high: int, label: str):
    try:
        value = int((command.args or "").strip())
        if not low <= value <= high:
            raise ValueError
    except ValueError:
        await message.answer(f"{label}: {await pipeline.int_setting(key)}. Поменять — число от {low} до {high}, "
                             f"например <code>/{message.text.split()[0][1:]} {(low + high) // 2}</code>")
        return
    await pipeline.db.set_setting(key, str(value))
    await message.answer(f"✅ {label}: {value}")


def _size_label(size: int) -> str:
    return str(size) if size else "все новые по тематике"


@router.message(Command("size"))
async def cmd_size(message: Message, command: CommandObject, pipeline):
    arg = (command.args or "").strip().lower()
    if arg in ("все", "всё", "all", "0"):
        size = 0
    elif arg.isdigit() and 1 <= int(arg) <= 50:
        size = int(arg)
    else:
        await message.answer(f"Тем в подборке: {_size_label(await pipeline.int_setting('digest_size'))}.\n"
                             "Поменять: <code>/size все</code> — присылать все новые темы, "
                             "<code>/size 10</code> — не больше 10 самых интересных.")
        return
    await pipeline.db.set_setting("digest_size", str(size))
    await message.answer(f"✅ Тем в подборке: {_size_label(size)}")


@router.message(Command("threshold"))
async def cmd_threshold(message: Message, command: CommandObject, pipeline):
    await _int_command(message, command, pipeline, "min_score", 0, 10, "Минимальная оценка темы")


@router.message(Command("settings"))
async def cmd_settings(message: Message, pipeline, settings):
    sources = await pipeline.db.list_sources()
    channels = sum(1 for s in sources if s.kind == "tg")
    tg_state = "подключено" if pipeline.tg else "не настроено (нужны TG_API_ID, TG_API_HASH и python -m bot.login)"
    await message.answer(
        "<b>Настройки</b>\n"
        f"Аккаунтов с доступом: {len(await pipeline.owner_ids())} (/users)\n"
        f"Источники: каналов {channels}, сайтов {len(sources) - channels}\n"
        f"Чтение каналов: {tg_state}\n"
        f"Подборки: {esc(await pipeline.digest_times())} ({esc(settings.timezone)})\n"
        f"Тем в подборке: {_size_label(await pipeline.int_setting('digest_size'))}\n"
        f"Минимальная оценка: {await pipeline.int_setting('min_score')}\n"
        f"Сбор новых постов: каждые {settings.collect_interval_min} мин\n"
        f"Модель для отбора: <code>{esc(settings.llm_model_fast)}</code>\n"
        f"Модель для текстов: <code>{esc(settings.llm_model_smart)}</code>\n"
        f"Образец стиля: {'задан' if await pipeline.style() else 'нет'} (/style)\n\n"
        f"<b>Интересы</b> (/topics):\n{esc(shorten(await pipeline.interests(), 600))}"
    )


async def _users_view(pipeline) -> tuple[str, object]:
    owners = pipeline.settings.owner_ids or [int(v) for v in [await pipeline.db.get_setting("owner_id")] if v]
    lines, kb = ["<b>Аккаунты с доступом к боту</b>"], InlineKeyboardBuilder()
    for user_id in await pipeline.owner_ids():
        if user_id in owners:
            lines.append(f"• <code>{user_id}</code> — владелец")
        else:
            lines.append(f"• <code>{user_id}</code>")
            kb.button(text=f"❌ Убрать {user_id}", callback_data=AccessCb(action="remove", user_id=user_id))
    lines.append("\nДобавить аккаунт: нажми /start с него — сюда придёт запрос с кнопкой «Пустить». "
                 "Или отправь <code>/adduser ID</code>.")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@router.message(Command("users"))
async def cmd_users(message: Message, pipeline):
    text, kb = await _users_view(pipeline)
    await message.answer(text, reply_markup=kb)


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject, bot: Bot, pipeline):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Пришли ID аккаунта: <code>/adduser 123456789</code>. "
                             "Свой ID аккаунт увидит, если нажмёт /start в этом боте.")
        return
    await _grant(message, bot, pipeline, int(arg))


async def _grant(message: Message, bot: Bot, pipeline, user_id: int) -> None:
    if not await pipeline.add_user(user_id):
        await message.answer(f"ℹ️ У аккаунта <code>{user_id}</code> доступ уже есть.")
        return
    await message.answer(f"✅ Доступ открыт для <code>{user_id}</code>. Источники, интересы и подборки у аккаунтов общие.")
    with suppress(Exception):  # аккаунт мог ещё не нажать /start — тогда бот не может ему написать
        await bot.send_message(user_id, "✅ Доступ к боту открыт. Нажми /start.")


@router.callback_query(AccessCb.filter())
async def on_access(cb: CallbackQuery, callback_data: AccessCb, bot: Bot, pipeline):
    await cb.answer()
    user_id = callback_data.user_id
    if callback_data.action == "allow":
        await cb.message.edit_reply_markup(reply_markup=None)
        await _grant(cb.message, bot, pipeline, user_id)
    elif callback_data.action == "deny":
        await cb.message.edit_text(f"🚫 Запрос от <code>{user_id}</code> отклонён.")
    elif callback_data.action == "remove":
        if user_id == cb.from_user.id:
            await cb.message.answer("Нельзя убрать аккаунт, с которого ты сейчас пишешь.")
            return
        await pipeline.remove_user(user_id)
        text, kb = await _users_view(pipeline)
        with suppress(TelegramBadRequest):
            await cb.message.edit_text(text, reply_markup=kb)

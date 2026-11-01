import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import parse_times
from .ui import send_digest

log = logging.getLogger(__name__)


async def run_digest(bot: Bot, pipeline, header: str = "Подборка", chat_ids: list[int] | None = None) -> int:
    """Собрать свежее, отобрать темы и прислать (по умолчанию — во все аккаунты владельца). Возвращает число тем."""
    chat_ids = chat_ids or await pipeline.owner_ids()
    if not chat_ids:
        log.info("Владелец ещё не нажал /start — подборку отправлять некому")
        return 0
    await pipeline.collect()
    topics = await pipeline.make_digest()
    for chat_id in chat_ids if topics else []:
        try:
            await send_digest(bot, chat_id, pipeline, topics, header)
        except TelegramForbiddenError:
            log.warning("Аккаунт %s заблокировал бота — подборку ему не отправил", chat_id)
    return len(topics)


def schedule_digests(scheduler: AsyncIOScheduler, times: str, bot: Bot, pipeline) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("digest_"):
            job.remove()
    for h, m in parse_times(times):
        scheduler.add_job(
            run_digest, CronTrigger(hour=h, minute=m), args=(bot, pipeline, f"Подборка на {h:02d}:{m:02d}"),
            id=f"digest_{h:02d}{m:02d}",
            # компьютер мог спать в нужное время — догоняем в течение 3 часов
            misfire_grace_time=3 * 3600, coalesce=True,
        )


def make_scheduler(timezone: str, collect_minutes: int, bot: Bot, pipeline) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)

    async def collect():
        try:
            await pipeline.collect()
        except Exception:
            log.exception("Сбор упал")

    scheduler.add_job(collect, IntervalTrigger(minutes=collect_minutes), id="collect",
                      next_run_time=datetime.now(scheduler.timezone) + timedelta(seconds=30),
                      max_instances=1, coalesce=True)
    return scheduler

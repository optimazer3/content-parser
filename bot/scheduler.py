import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import parse_times
from .ui import send_digest

log = logging.getLogger(__name__)


async def run_digest(bot: Bot, pipeline, header: str = "Подборка") -> int:
    """Собрать свежее, отобрать темы и прислать владельцу. Возвращает число тем."""
    owner = await pipeline.owner_id()
    if not owner:
        log.info("Владелец ещё не нажал /start — подборку отправлять некому")
        return 0
    await pipeline.collect()
    topics = await pipeline.make_digest()
    if topics:
        await send_digest(bot, owner, pipeline, topics, header)
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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from src.config import settings


class TaskScheduler:
    """Manages scheduled and periodic tasks."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def add_interval_task(
        self,
        func,
        seconds: int,
        task_id: str,
        args: tuple | None = None,
    ) -> None:
        """Add a task that runs at fixed intervals."""
        self.scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=seconds),
            id=task_id,
            args=args or (),
            replace_existing=True,
            misfire_grace_time=30,
        )
        logger.info(f"Scheduled interval task '{task_id}' every {seconds}s")

    def add_daily_task(
        self,
        func,
        hour: int,
        minute: int,
        task_id: str,
        args: tuple | None = None,
    ) -> None:
        """Add a task that runs once per day at a specific time."""
        self.scheduler.add_job(
            func,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
            id=task_id,
            args=args or (),
            replace_existing=True,
        )
        logger.info(f"Scheduled daily task '{task_id}' at {hour:02d}:{minute:02d} ({settings.timezone})")

    def add_weekly_task(
        self,
        func,
        day_of_week: str,
        hour: int,
        minute: int,
        task_id: str,
        args: tuple | None = None,
    ) -> None:
        """Add a task that runs once per week."""
        self.scheduler.add_job(
            func,
            trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=settings.timezone),
            id=task_id,
            args=args or (),
            replace_existing=True,
        )
        logger.info(f"Scheduled weekly task '{task_id}' on {day_of_week} at {hour:02d}:{minute:02d} ({settings.timezone})")

    def remove_task(self, task_id: str) -> None:
        """Remove a scheduled task."""
        try:
            self.scheduler.remove_job(task_id)
            logger.info(f"Removed task '{task_id}'")
        except Exception:
            logger.warning(f"Task '{task_id}' not found for removal")

    def pause_task(self, task_id: str) -> None:
        """Pause a scheduled task."""
        try:
            self.scheduler.pause_job(task_id)
            logger.info(f"Paused task '{task_id}'")
        except Exception:
            logger.warning(f"Task '{task_id}' not found for pause")

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task."""
        try:
            self.scheduler.resume_job(task_id)
            logger.info(f"Resumed task '{task_id}'")
        except Exception:
            logger.warning(f"Task '{task_id}' not found for resume")

    def start(self) -> None:
        """Start the scheduler."""
        self.scheduler.start()
        logger.info("Task scheduler started")

    def shutdown(self) -> None:
        """Shutdown the scheduler."""
        self.scheduler.shutdown(wait=False)
        logger.info("Task scheduler stopped")

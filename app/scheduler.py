"""定时任务：每天自动扣月费并刷新提醒。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .database import get_db, init_engine
from .services import run_daily_billing

logger = logging.getLogger("kuandai.scheduler")
_scheduler: BackgroundScheduler | None = None


def daily_job() -> None:
    """每日巡检：按套餐日扣费 + 刷新催缴提醒。"""
    init_engine()
    db_gen = get_db()
    db = next(db_gen)
    try:
        stats = run_daily_billing(db)
        logger.info("每日巡检完成：%s", stats)
    except Exception:
        logger.exception("每日巡检失败")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()

    if not settings.enable_scheduler:
        logger.info("定时任务已关闭（ENABLE_SCHEDULER=false）")
        return None
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        daily_job,
        trigger="cron",
        hour=settings.daily_check_hour,
        minute=settings.daily_check_minute,
        id="daily_check",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "定时任务已启动，每天 %02d:%02d 执行一次自动扣费与提醒巡检",
        settings.daily_check_hour,
        settings.daily_check_minute,
    )
    return scheduler

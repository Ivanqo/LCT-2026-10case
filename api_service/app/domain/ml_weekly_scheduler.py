"""Optional, dependency-free background scheduler for persisted weekly ML reports."""
from __future__ import annotations

import asyncio
import logging

from ..config import settings
from .ml_weekly_report import run_scheduler_tick

logger = logging.getLogger("api.ml_weekly_scheduler")


async def run_scheduler(initial_delay: float = 30.0) -> None:
    """Long-lived API loop: wait out the start-up, then check the persisted interval every poll period.

    Started only by the API's startup hook (`app.main`), never by the CLI, the worker or an in-process test client.
    """
    await asyncio.sleep(max(0.0, float(initial_delay)))
    while True:
        try:
            result = await asyncio.to_thread(run_scheduler_tick)
            if result.get("ran"):
                logger.info("Scheduled weekly ML reports saved | count=%s next_due_at=%s", result["reports_saved"], result["next_due_at"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled weekly ML report failed")
        await asyncio.sleep(max(1, int(settings.ML_WEEKLY_REPORT_POLL_SECONDS)))

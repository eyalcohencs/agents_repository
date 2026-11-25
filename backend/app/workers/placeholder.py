from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def run() -> None:
    """Standby worker entrypoint until Procrastinate worker wiring lands in T-012."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Worker placeholder received signal %s; shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "OnTrack worker placeholder started (env=%s, upload_storage_dir=%s). "
        "Awaiting T-012 Procrastinate worker integration.",
        settings.app_env,
        settings.upload_storage_dir,
    )
    stop_event.wait()
    logger.info("OnTrack worker placeholder stopped cleanly.")


if __name__ == "__main__":
    run()

"""Graceful shutdown and resource cleanup helpers."""

from __future__ import annotations

import asyncio
import logging
import signal
import tempfile
from pathlib import Path
from typing import Any, Iterable, Set

from .utils import maybe_await

logger = logging.getLogger(__name__)


class ApplicationLifecycle:
    def __init__(self, redis_client: Any = None, workers: Iterable[Any] = ()):
        self.redis = redis_client
        self.workers = list(workers or [])
        self.in_flight_jobs: Set[str] = set()
        self.temp_files: Set[str] = set()
        self.shutdown_event = asyncio.Event()
        self.is_shutting_down = False

    def register_in_flight(self, job_id: str) -> None:
        self.in_flight_jobs.add(str(job_id))

    def unregister_in_flight(self, job_id: str) -> None:
        self.in_flight_jobs.discard(str(job_id))

    def register_temp_file(self, path: str) -> None:
        if path:
            self.temp_files.add(str(path))

    def unregister_temp_file(self, path: str) -> None:
        self.temp_files.discard(str(path))

    async def shutdown(self, signal_name: str = 'UNKNOWN', drain_timeout_seconds: float = 30.0) -> None:
        if self.is_shutting_down:
            return
        self.is_shutting_down = True
        self.shutdown_event.set()
        logger.info('shutdown initiated via %s', signal_name)

        if self.in_flight_jobs:
            try:
                await asyncio.wait_for(self._wait_for_in_flight(), timeout=drain_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning('timeout waiting for in-flight jobs: %s', sorted(self.in_flight_jobs))

        for worker in self.workers:
            close = getattr(worker, 'close', None)
            if close:
                await maybe_await(close())

        if self.redis is not None:
            close = getattr(self.redis, 'close', None)
            if close:
                await maybe_await(close())

        self.cleanup_temp_files()
        self.cleanup_orphaned_temps()
        logger.info('shutdown complete')

    async def _wait_for_in_flight(self) -> None:
        while self.in_flight_jobs:
            await asyncio.sleep(0.25)

    def cleanup_temp_files(self) -> None:
        for path in list(self.temp_files):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                logger.warning('failed to remove temp file %s', path)
            finally:
                self.unregister_temp_file(path)

    def cleanup_orphaned_temps(self) -> None:
        temp_dir = Path(tempfile.gettempdir())
        for path in temp_dir.glob('fb_upload_*'):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning('failed to remove orphaned temp file %s', path)

    def register_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda selected=sig: asyncio.create_task(self.shutdown(signal.Signals(selected).name)),
                )
            except (NotImplementedError, RuntimeError):
                # Signal handlers are unavailable on some platforms/test loops.
                continue

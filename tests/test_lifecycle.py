import asyncio
from pathlib import Path

from fb_automation.lifecycle import ApplicationLifecycle

from .fakes import FakeRedis


def test_lifecycle_shutdown_cleans_temp_files(tmp_path: Path):
    async def run():
        redis = FakeRedis()
        temp_file = tmp_path / 'fb_upload_test.tmp'
        temp_file.write_text('temporary')
        lifecycle = ApplicationLifecycle(redis)
        lifecycle.register_temp_file(str(temp_file))
        await lifecycle.shutdown('TEST', drain_timeout_seconds=0.1)
        assert not temp_file.exists()
        assert lifecycle.temp_files == set()
        assert lifecycle.shutdown_event.is_set()

    asyncio.run(run())


def test_lifecycle_tracks_in_flight_jobs():
    async def run():
        lifecycle = ApplicationLifecycle()
        lifecycle.register_in_flight('job-1')
        assert lifecycle.in_flight_jobs == {'job-1'}
        lifecycle.unregister_in_flight('job-1')
        assert lifecycle.in_flight_jobs == set()

    asyncio.run(run())

import importlib.util
from pathlib import Path


ENGINE_PATH = Path(__file__).resolve().parents[1] / "playwright_engine.py"
SPEC = importlib.util.spec_from_file_location("current_playwright_engine_speed_test", ENGINE_PATH)
assert SPEC and SPEC.loader
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


def test_parallel_video_portal_timeout_is_capped_below_single_post_timeout():
    single_timeout = engine._pages_portal_timeout_seconds("video", True)
    parallel_timeout = engine._parallel_pages_portal_timeout_seconds("video", True)

    assert parallel_timeout < single_timeout
    assert parallel_timeout == 120


def test_parallel_media_portal_timeout_keeps_positive_budget():
    assert engine._parallel_pages_portal_timeout_seconds("image", True) >= 45
    assert engine._parallel_pages_portal_timeout_seconds("post", False) >= 45


def test_parallel_effective_concurrency_caps_to_render_context_budget(monkeypatch):
    monkeypatch.setattr(engine, "MAX_PARALLEL_PAGES", 3)
    monkeypatch.setattr(engine, "POST_PARALLEL_SAME_COOKIE_MAX_CONTEXTS", 3)

    assert engine._parallel_batch_effective_concurrency(total=2) == 2
    assert engine._parallel_batch_effective_concurrency(total=4) == 3
    assert engine._parallel_batch_effective_concurrency(total=4, max_parallel=2) == 2

import importlib.util
import asyncio
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


def test_batch_posting_mode_defaults_to_sequential_and_parallel_is_opt_in(monkeypatch):
    calls = []

    async def stage_posts(posts):
        return list(posts), []

    async def sequential_path(cookies_json, posts, progress_callback=None):
        del cookies_json, progress_callback
        calls.append(("sequential", len(posts)))
        return [{"page": post.get("page_name"), "success": True, "result": "ok"} for post in posts]

    async def parallel_path(cookies_json, posts, progress_callback=None, max_parallel=None):
        del cookies_json, progress_callback
        calls.append(("parallel", len(posts), max_parallel))
        return [{"page": post.get("page_name"), "success": True, "result": "ok"} for post in posts]

    monkeypatch.setattr(engine, "POST_PARALLEL_BATCH_ENABLED", True)
    monkeypatch.setattr(engine, "MAX_PARALLEL_PAGES", 3)
    monkeypatch.setattr(engine, "_stage_batch_media_sources", stage_posts)
    monkeypatch.setattr(engine, "_cleanup_staged_batch_media", lambda paths: None)
    monkeypatch.setattr(engine, "_create_facebook_posts_unstaged", sequential_path)
    monkeypatch.setattr(engine, "_create_facebook_posts_parallel_unstaged", parallel_path)

    posts = [
        {"page_name": "Page A", "post_type": "text", "media_url": ""},
        {"page_name": "Page B", "post_type": "text", "media_url": ""},
    ]

    asyncio.run(engine._create_facebook_posts_browser("[]", posts))
    asyncio.run(engine._create_facebook_posts_browser("[]", posts, posting_mode="parallel"))

    assert calls == [("sequential", 2), ("parallel", 2, 3)]


def test_parallel_worker_cookies_replace_page_actor_cookie():
    cookies = [
        {
            "name": "c_user",
            "value": "111",
            "domain": ".facebook.com",
            "path": "/",
            "expires": 1780000000,
            "sameSite": "Lax",
        },
        {"name": "i_user", "value": "old-page", "domain": ".facebook.com", "path": "/"},
        {"name": "xs", "value": "session", "domain": ".facebook.com", "path": "/"},
    ]

    scoped = engine._cookies_with_page_actor(cookies, "222")
    actor_cookies = [cookie for cookie in scoped if cookie["name"] == "i_user"]

    assert len(actor_cookies) == 1
    assert actor_cookies[0]["value"] == "222"
    assert actor_cookies[0]["domain"] == ".facebook.com"
    assert actor_cookies[0]["path"] == "/"
    assert actor_cookies[0]["sameSite"] == "Lax"


def test_target_page_actor_id_comes_from_profile_url():
    assert (
        engine._target_page_actor_id_from_post(
            {"page_id_or_url": "https://www.facebook.com/profile.php?id=61590567386488"}
        )
        == "61590567386488"
    )
    assert engine._target_page_actor_id_from_post({"page_id_or_url": "https://www.facebook.com/some-page"}) == ""

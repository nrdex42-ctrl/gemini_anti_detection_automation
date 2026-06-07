import asyncio
import importlib.util
from pathlib import Path


ENGINE_PATH = Path(__file__).resolve().parents[1] / "playwright_engine.py"
SPEC = importlib.util.spec_from_file_location("current_playwright_engine_direct_post_test", ENGINE_PATH)
assert SPEC and SPEC.loader
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


def test_direct_composer_route_failure_is_recoverable():
    detail = (
        "Could not publish through direct composer. "
        "Pages Portal fallback is disabled. "
        "Diagnostic: /diagnostics/direct_post_all_routes_failed"
    )

    assert engine._post_result_is_safe_to_recover(detail)


def test_unconfirmed_publish_is_not_recoverable():
    detail = "Clicked publish, but Facebook did not confirm the post. Diagnostic: /diagnostics/post_publish_unverified"

    assert not engine._post_result_is_safe_to_recover(detail)


def test_switch_dialog_selection_is_confirmed_before_waiting(monkeypatch):
    async def run():
        calls = []

        async def dialog_visible(page):
            calls.append(("visible", page))
            return True

        async def click_option(page, page_name, timeout=4500):
            calls.append(("select", page_name, timeout))
            return True

        async def confirm(page, timeout=2500):
            calls.append(("confirm", timeout))
            return True

        async def settle(page, timeout_ms=5000, initial_grace_seconds=0.0):
            calls.append(("settle", timeout_ms, initial_grace_seconds))
            return True

        monkeypatch.setattr(engine, "_profile_switch_dialog_visible", dialog_visible)
        monkeypatch.setattr(engine, "_click_profile_switch_option", click_option)
        monkeypatch.setattr(engine, "_confirm_profile_switch_dialog", confirm)
        monkeypatch.setattr(engine, "_wait_for_profile_switch_to_settle", settle)

        fake_page = object()
        assert await engine._click_onscreen_switch_button(fake_page, "Oppo")
        assert calls == [
            ("visible", fake_page),
            ("select", "Oppo", 4500),
            ("confirm", 2500),
            ("settle", 9000, engine.POST_PROFILE_SWITCH_SETTLE_GRACE_SECONDS),
        ]

    asyncio.run(run())

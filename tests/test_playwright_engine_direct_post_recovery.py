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


def test_security_text_regex_ignores_generic_arabic_feed_terms():
    text = "منشور عادي عن قيود الحياة وتسجيل الدخول في النقاش"

    assert engine._FACEBOOK_SECURITY_TEXT_RE.search(text) is None


def test_security_text_regex_keeps_account_specific_arabic_security_terms():
    assert engine._FACEBOOK_SECURITY_TEXT_RE.search("تم تقييد حسابك بسبب نشاط غير معتاد")
    assert engine._FACEBOOK_SECURITY_TEXT_RE.search("يرجى تأكيد هويتك")


def test_caption_text_match_requires_complete_arabic_and_english_text():
    caption = "استشعار وجود الله أمر يجعلني في غاية السكينة.\nEnglish caption: quiet, exact words."

    assert engine._caption_text_matches(caption, caption)
    assert engine._caption_text_matches(caption, f"{caption}\n")
    assert not engine._caption_text_matches(caption, "استشعار وجود الله أمر يجعلني")
    assert not engine._caption_text_matches(caption, caption.replace("quiet", "quite"))
    assert not engine._caption_text_matches(caption, caption.replace("السكينة", "السكينه"))


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


def test_initial_video_confirmation_uses_fast_publish_click_acceptance(monkeypatch):
    async def run():
        calls = []

        async def slow_network_confirmation(network_monitor, timeout_ms=None):
            calls.append(("network", network_monitor, timeout_ms))
            await asyncio.sleep(30)
            return None, "late network confirmation"

        async def verify_publish(
            page,
            *,
            caption="",
            post_type="post",
            timeout_ms=0,
            accept_publish_click=False,
        ):
            calls.append(("verify", page, post_type, timeout_ms, accept_publish_click))
            return True, "video publish accepted after final publish click"

        monkeypatch.setattr(engine, "_await_post_network_confirmation", slow_network_confirmation)
        monkeypatch.setattr(engine, "_verify_post_published", verify_publish)
        monkeypatch.setattr(engine, "POST_ACCEPT_VIDEO_PUBLISH_CLICK_AS_SUCCESS", True)

        fake_page = object()
        fake_monitor = object()
        result = await engine._await_initial_publish_confirmation(
            fake_page,
            fake_monitor,
            caption="caption",
            post_type="video",
        )

        assert result == (
            True,
            "quick UI publish confirmation: video publish accepted after final publish click",
        )
        assert ("network", fake_monitor, engine._post_network_confirmation_timeout_ms("video")) in calls
        assert ("verify", fake_page, "video", engine.POST_INITIAL_UI_CONFIRMATION_TIMEOUT_MS, True) in calls

    asyncio.run(run())

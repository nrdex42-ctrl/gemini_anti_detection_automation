import asyncio
import sys
import time
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from telegram_bot import TelegramBotApp


def test_multi_video_caption_prompt_uses_shared_caption_step():
    app = TelegramBotApp.__new__(TelegramBotApp)
    session = {
        "lang": "en",
        "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
        "selected_pages": [0, 1],
        "multi_media_paths": ["/tmp/one.mp4", "/tmp/two.mp4"],
        "caption_draft": "Shared caption",
    }

    text = app.multi_video_caption_prompt(session)

    assert "Video Caption" in text
    assert "Videos received: 2/2" in text
    assert "Current caption: Shared caption" in text
    assert "Send one shared caption for all videos." not in text


def test_final_multi_video_upload_sends_single_caption_card_without_instructions_card():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_video_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "multi_media_paths": ["/tmp/one.mp4"],
            }
        )

        async def download_file(file_id, account_id):
            return "/tmp/two.mp4"

        app.download_file = download_file

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "",
            {"video": {"file_id": "video_2"}},
        )

        assert handled is True
        assert len(sent) == 1
        assert "✅ All 2 videos received." in sent[0]["text"]
        assert "Videos received: 2/2" in sent[0]["text"]
        assert "Send one shared caption" not in sent[0]["text"]
        assert "Caption Instructions" not in sent[0]["text"]
        assert sent[0]["reply_markup"]["keyboard"][0] == ["⬅️ Back to Dashboard", "❌ Cancel", "⏭️ Skip"]
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_caption"
        assert session["multi_media_paths"] == ["/tmp/one.mp4", "/tmp/two.mp4"]

    asyncio.run(run())


def test_partial_multi_video_upload_does_not_send_received_card():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_video_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "multi_media_paths": [],
            }
        )

        async def download_file(file_id, account_id):
            return "/tmp/one.mp4"

        app.download_file = download_file

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "",
            {"video": {"file_id": "video_1"}},
        )

        assert handled is True
        assert sent == []
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_video_upload"
        assert session["multi_media_paths"] == ["/tmp/one.mp4"]

    asyncio.run(run())


def test_edit_message_ignores_telegram_message_not_modified():
    app = TelegramBotApp.__new__(TelegramBotApp)
    calls = []

    async def fake_telegram_api(method, payload):
        calls.append((method, payload))
        return {
            "ok": False,
            "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
        }

    app.telegram_api = fake_telegram_api

    asyncio.run(app.edit_message(123, 456, "same text", reply_markup={"inline_keyboard": []}))

    assert calls[0][0] == "editMessageText"


def build_session_app(session, *, lang="en"):
    app = TelegramBotApp.__new__(TelegramBotApp)
    app.dashboard_sessions = {"123:99": {**session, "updated_at": time.time()}}
    app.update_locks = {}
    sent = []

    def start_background_task(coro, label):
        coro.close()
        return None

    async def send_message(chat_id, text, reply_to_message_id=0, *, reply_markup=None, parse_mode=""):
        sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": 777}}

    async def user_language(user_id=0):
        return lang

    app.start_background_task = start_background_task
    app.send_message = send_message
    app.user_language = user_language
    return app, sent


def test_page_selection_dashboard_post_button_switches_post_type_instead_of_warning():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "page_select",
                "lang": "ar",
                "account_id": "acct_1",
                "pages": [{"page_name": "Insan"}, {"page_name": "Oppo"}],
                "selected_pages": [],
                "post_type": "video",
            },
            lang="ar",
        )
        refreshed = []

        async def refresh_open_page_selection_card(chat_id, user_id, fallback_message_id, session, *, prefix=""):
            refreshed.append(
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "fallback_message_id": fallback_message_id,
                    "session": dict(app.get_dashboard_session(chat_id, user_id)),
                    "prefix": prefix,
                }
            )

        async def prompt_for_page(*args, **kwargs):
            raise AssertionError("active page selection should not send a new page-selection card")

        app.refresh_open_page_selection_card = refresh_open_page_selection_card
        app.prompt_for_page = prompt_for_page

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "📸 منشور صورة",
            {"text": "📸 منشور صورة"},
        )

        assert handled is True
        assert sent == []
        assert refreshed
        assert "صورة" in refreshed[0]["prefix"]
        session = app.get_dashboard_session(123, 99)
        assert session["post_type"] == "image"
        assert session["step"] == "page_select"
        assert "select_all_pages" not in session

    asyncio.run(run())


def test_page_selection_post_all_button_restarts_all_pages_flow():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "page_select",
                "lang": "ar",
                "account_id": "acct_1",
                "pages": [{"page_name": "Insan"}, {"page_name": "Oppo"}],
                "selected_pages": [],
                "post_type": "video",
            },
            lang="ar",
        )
        refreshed = []

        async def refresh_open_page_selection_card(chat_id, user_id, fallback_message_id, session, *, prefix=""):
            refreshed.append({"session": dict(app.get_dashboard_session(chat_id, user_id)), "prefix": prefix})

        async def prompt_for_page(*args, **kwargs):
            raise AssertionError("active page selection should not send a new page-selection card")

        app.refresh_open_page_selection_card = refresh_open_page_selection_card
        app.prompt_for_page = prompt_for_page

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "📋 انشر لكل الصفحات",
            {"text": "📋 انشر لكل الصفحات"},
        )

        assert handled is True
        assert sent == []
        assert refreshed
        session = app.get_dashboard_session(123, 99)
        assert session["select_all_pages"] is True
        assert session["selected_pages"] == [0, 1]
        assert "post_type" not in session
        assert session["step"] == "page_select"

    asyncio.run(run())


def test_page_selection_fallback_instruction_uses_session_language():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "page_select",
                "lang": "ar",
                "account_id": "acct_1",
            },
            lang="ar",
        )

        async def dashboard_reply_markup(user_id):
            return {"keyboard": [["لوحة التحكم"]]}

        app.dashboard_reply_markup = dashboard_reply_markup

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "نص غير متعلق بالصفحات",
            {"text": "نص غير متعلق بالصفحات"},
        )

        assert handled is True
        assert len(sent) == 1
        assert "استخدم أزرار الصفحات" in sent[0]["text"]
        assert "Use the page buttons" not in sent[0]["text"]

    asyncio.run(run())


def test_page_selection_stage_controls_are_removed():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "page_select",
                "lang": "ar",
                "account_id": "acct_1",
                "post_stage_controls_message_id": 444,
                "post_stage_controls_key": "review:ar",
            },
            lang="ar",
        )
        deleted = []

        async def delete_message(chat_id, message_id):
            deleted.append((chat_id, message_id))

        app.delete_message = delete_message

        await app.send_post_stage_controls(
            123,
            99,
            456,
            app.get_dashboard_session(123, 99),
            "page_select",
        )

        assert sent == []
        assert deleted == [(123, 444)]
        session = app.get_dashboard_session(123, 99)
        assert "post_stage_controls_message_id" not in session
        assert "post_stage_controls_key" not in session

    asyncio.run(run())


def test_active_multi_video_upload_consumes_stale_dashboard_button_text():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_video_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "multi_media_paths": [],
            }
        )

        async def handle_dashboard_button(*args, **kwargs):
            raise AssertionError("stale dashboard button should not replace the upload card")

        app.handle_dashboard_button = handle_dashboard_button

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "🎬 Video Post",
                    "chat": {"id": 123},
                    "from": {"id": 99},
                }
            }
        )

        assert len(sent) == 1
        assert "Multi Video Upload" in sent[0]["text"]
        assert "Send video 1 of 2 now." in sent[0]["text"]

    asyncio.run(run())


def test_video_mode_reply_keyboard_button_moves_to_matching_upload_stage():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "video_mode",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "post_type": "video",
            }
        )

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "📚 Multi Video Upload",
            {"text": "📚 Multi Video Upload"},
        )

        assert handled is True
        assert len(sent) == 1
        assert "Multi Video Upload" in sent[0]["text"]
        assert sent[0]["reply_markup"]["keyboard"][0] == ["⬅️ Back to Dashboard", "❌ Cancel"]
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_video_upload"
        assert session["video_mode"] == "multi_upload"

    asyncio.run(run())


def test_image_mode_reply_keyboard_button_moves_to_matching_upload_stage():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "image_mode",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "post_type": "image",
            }
        )

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "📚 Multi Image Upload",
            {"text": "📚 Multi Image Upload"},
        )

        assert handled is True
        assert len(sent) == 1
        assert "Multi Image Upload" in sent[0]["text"]
        assert sent[0]["reply_markup"]["keyboard"][0] == ["⬅️ Back to Dashboard", "❌ Cancel"]
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_image_upload"
        assert session["image_mode"] == "multi_upload"

    asyncio.run(run())


def test_final_multi_image_upload_sends_image_caption_cards():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_image_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "post_type": "image",
                "multi_media_paths": ["/tmp/one.jpg"],
            }
        )

        async def download_file(file_id, account_id):
            return "/tmp/two.jpg"

        app.download_file = download_file

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "",
            {"photo": [{"file_id": "image_small"}, {"file_id": "image_2"}]},
        )

        assert handled is True
        assert len(sent) == 1
        assert "✅ All 2 images received." in sent[0]["text"]
        assert "Images received: 2/2" in sent[0]["text"]
        assert "Send one shared caption for all images." not in sent[0]["text"]
        assert "Caption Instructions" not in sent[0]["text"]
        assert sent[0]["reply_markup"]["keyboard"][0] == ["⬅️ Back to Dashboard", "❌ Cancel", "⏭️ Skip"]
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_caption"
        assert session["post_type"] == "image"
        assert session["multi_media_paths"] == ["/tmp/one.jpg", "/tmp/two.jpg"]

    asyncio.run(run())


def test_multi_caption_message_opens_review_card_immediately():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_caption",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "post_type": "video",
                "caption_draft": "",
                "multi_media_paths": ["/tmp/one.mp4", "/tmp/two.mp4"],
            }
        )
        reviewed = []

        async def show_post_review(chat_id, message_id, user_id, session, *, edit=False):
            reviewed.append(dict(session))

        app.show_post_review = show_post_review

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "Shared caption",
            {"text": "Shared caption"},
        )

        assert handled is True
        assert sent == []
        assert reviewed
        assert reviewed[0]["caption"] == "Shared caption"
        assert "caption_draft" not in reviewed[0]

    asyncio.run(run())


def test_multi_caption_skip_opens_review_without_caption():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_caption",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "post_type": "video",
                "caption_draft": "Draft caption",
                "multi_media_paths": ["/tmp/one.mp4", "/tmp/two.mp4"],
            }
        )
        reviewed = []

        async def show_post_review(chat_id, message_id, user_id, session, *, edit=False):
            reviewed.append(dict(session))

        app.show_post_review = show_post_review

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "⏭️ Skip",
            {"text": "⏭️ Skip"},
        )

        assert handled is True
        assert sent == []
        assert reviewed
        assert reviewed[0]["caption"] == ""
        assert "caption_draft" not in reviewed[0]

    asyncio.run(run())


def test_free_text_without_active_session_does_not_send_dashboard_card():
    async def run():
        app, sent = build_session_app({})
        app.dashboard_sessions = {}

        async def show_dashboard(*args, **kwargs):
            raise AssertionError("free text without a session should be ignored")

        app.show_dashboard = show_dashboard

        await app.handle_update(
            {
                "message": {
                    "message_id": 456,
                    "text": "caption that is not part of an active flow",
                    "chat": {"id": 123},
                    "from": {"id": 99},
                }
            }
        )

        assert sent == []

    asyncio.run(run())


def test_multi_video_download_error_keeps_upload_session_active():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "multi_video_upload",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "multi_media_paths": [],
            }
        )

        async def download_file(file_id, account_id):
            raise RuntimeError("Telegram did not return a downloadable file path")

        app.download_file = download_file

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "",
            {"video": {"file_id": "video_1"}},
        )

        assert handled is True
        assert len(sent) == 1
        assert "I could not download that video from Telegram" in sent[0]["text"]
        assert "Send video 1 of 2 now." in sent[0]["text"]
        session = app.get_dashboard_session(123, 99)
        assert session["step"] == "multi_video_upload"
        assert session["multi_media_paths"] == []

    asyncio.run(run())


def test_caption_after_media_preserves_raw_arabic_english_caption_text():
    async def run():
        app, _sent = build_session_app(
            {
                "action": "post",
                "step": "caption_after_media",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}],
                "selected_pages": [0],
                "post_type": "image",
                "media_path": "/tmp/image.jpg",
            }
        )
        raw_caption = "  استشعار وجود الله أمر يجعلني في غاية السكينة...\nEnglish caption: exact words stay here.  "
        reviewed = []

        async def show_post_review(chat_id, message_id, user_id, session, *, edit=False):
            reviewed.append(dict(session))

        app.show_post_review = show_post_review

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            raw_caption,
            {"text": raw_caption},
        )

        assert handled is True
        assert reviewed
        assert reviewed[0]["caption"] == raw_caption

    asyncio.run(run())


def test_done_button_review_card_sends_new_message_instead_of_editing_user_message():
    async def run():
        app, sent = build_session_app(
            {
                "action": "post",
                "step": "video_caption",
                "lang": "en",
                "account_id": "acct_1",
                "pages": [{"page_name": "Huawei"}, {"page_name": "Oppo"}],
                "selected_pages": [0, 1],
                "post_type": "video",
                "caption": "Shared caption",
                "multi_media_paths": ["/tmp/one.mp4", "/tmp/two.mp4"],
            }
        )
        app.storage = types.SimpleNamespace(get_account=lambda account_id, owner_id=None: {"name": "Omar Mohamed"})
        app.account_owner_scope = lambda user_id: str(user_id)

        async def edit_or_send_message(*args, **kwargs):
            raise AssertionError("review card should not edit the user's Done/caption message")

        app.edit_or_send_message = edit_or_send_message

        await app.show_post_review(123, 456, 99, app.get_dashboard_session(123, 99))

        assert len(sent) == 1
        assert sent[0]["reply_to_message_id"] == 456
        assert "Review Post" in sent[0]["text"]
        keyboard = sent[0]["reply_markup"]["inline_keyboard"]
        callback_data = [button["callback_data"] for row in keyboard for button in row]
        assert "post:confirm" in callback_data
        assert app.get_dashboard_session(123, 99)["step"] == "review"

    asyncio.run(run())


def test_account_rename_rejects_dashboard_button_label():
    async def run():
        app, sent = build_session_app(
            {
                "action": "manage_accounts",
                "step": "rename_input",
                "lang": "ar",
                "rename_account_id": "acct_1",
            },
            lang="ar",
        )
        updates = []

        def update_account_label(*args):
            updates.append(args)
            return True

        app.storage = types.SimpleNamespace(update_account_label=update_account_label)
        app.account_owner_scope = lambda user_id: str(user_id)

        handled = await app.handle_dashboard_session(
            123,
            99,
            456,
            "🔁 تغيير الحساب",
            {"text": "🔁 تغيير الحساب"},
        )

        assert handled is True
        assert updates == []
        assert "زر من لوحة التحكم" in sent[-1]["text"]
        assert app.get_dashboard_session(123, 99)["step"] == "rename_input"

    asyncio.run(run())

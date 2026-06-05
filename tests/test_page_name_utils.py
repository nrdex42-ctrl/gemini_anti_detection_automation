from page_name_utils import clean_facebook_page_name
from telegram_dashboard import page_display_name


def test_clean_facebook_page_name_strips_profile_picture_wrapper():
    assert clean_facebook_page_name("Profile picture for Oppo") == "Oppo"
    assert clean_facebook_page_name("Profile picture of Huawei") == "Huawei"
    assert clean_facebook_page_name("Oppo profile picture") == "Oppo"


def test_clean_facebook_page_name_uses_slug_fallback_for_generic_text():
    assert clean_facebook_page_name("Create post", "https://www.facebook.com/OppoEgypt") == "OppoEgypt"


def test_page_display_name_sanitizes_cached_bad_names():
    page = {
        "page_name": "Profile picture for Oppo",
        "page_url": "https://www.facebook.com/profile.php?id=123456789",
    }
    assert page_display_name(page) == "Oppo"

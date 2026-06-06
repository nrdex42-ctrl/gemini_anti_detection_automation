from telegram_dashboard import account_display_name


def test_account_display_name_keeps_resolved_facebook_name():
    account = {"account_id": "615123", "label": "Mohammed Mohammed"}

    assert account_display_name(account) == "Mohammed Mohammed"


def test_account_display_name_shows_id_for_generic_label():
    account = {"account_id": "615123", "label": "Facebook Account"}

    assert account_display_name(account) == "Facebook Account (615123)"


def test_account_display_name_shows_id_for_blank_label():
    account = {"account_id": "615123", "label": ""}

    assert account_display_name(account) == "615123"

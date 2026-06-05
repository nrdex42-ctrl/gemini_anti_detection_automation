from fb_automation.utils import (
    classify_error,
    cookies_json_to_header,
    extract_page_id,
    generate_client_id,
    generate_idempotence_token,
    sanitize_caption,
)


def test_cookie_header_and_page_id_helpers():
    header = cookies_json_to_header('[{"name":"c_user","value":"1"},{"name":"xs","value":"abc"}]')
    assert header == 'c_user=1; xs=abc'
    assert extract_page_id('https://www.facebook.com/profile.php?id=123&x=1') == '123'
    assert extract_page_id('https://www.facebook.com/some-page/') == 'some-page'


def test_hash_and_error_helpers_are_stable():
    assert len(generate_client_id('acct-1')) == 16
    token_a = generate_idempotence_token('acct-1', 'page-1', 'hello', time_bucket_minutes=10)
    token_b = generate_idempotence_token('acct-1', 'page-1', 'hello', time_bucket_minutes=10)
    assert token_a == token_b
    assert classify_error("Can't Read Files error=1366046") == 'UPLOAD_REJECTED'
    assert sanitize_caption('  hello\n\nworld  ') == 'hello world'

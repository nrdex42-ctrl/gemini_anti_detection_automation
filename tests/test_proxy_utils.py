import pytest

from proxy_utils import (
    normalize_proxy_url,
    playwright_proxy_config,
    proxy_display_url,
    requests_proxy_config,
)


def test_proxy_url_helpers_redact_credentials_for_display():
    proxy = normalize_proxy_url("http://user:pass@example.com:8080")

    assert proxy == "http://user:pass@example.com:8080"
    assert proxy_display_url(proxy) == "http://example.com:8080"
    assert requests_proxy_config(proxy) == {"http": proxy, "https": proxy}
    assert playwright_proxy_config(proxy) == {
        "server": "http://example.com:8080",
        "username": "user",
        "password": "pass",
    }


def test_proxy_url_helpers_accept_bare_host_port_as_http():
    assert normalize_proxy_url("proxy.example.com:9000") == "http://proxy.example.com:9000"
    assert playwright_proxy_config("proxy.example.com:9000") == {
        "server": "http://proxy.example.com:9000",
    }


@pytest.mark.parametrize("value", ["", "clear", "none", "off"])
def test_proxy_url_helpers_treat_clear_values_as_empty(value):
    assert normalize_proxy_url(value) == ""
    assert requests_proxy_config(value) is None
    assert playwright_proxy_config(value) is None


@pytest.mark.parametrize("value", ["ftp://example.com:21", "http://example.com", "http://example.com:8080/path"])
def test_proxy_url_helpers_reject_unsupported_or_incomplete_urls(value):
    with pytest.raises(ValueError):
        normalize_proxy_url(value)

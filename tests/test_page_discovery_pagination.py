import asyncio
import json

import playwright_engine


def _page_node(index):
    page_id = str(1000000000 + index)
    return {
        "id": page_id,
        "name": f"Managed Page {index:02d}",
        "url": f"https://www.facebook.com/profile.php?id={page_id}",
    }


def _graphql_payload(nodes, include_marker=True):
    payload = {"nodes": nodes}
    if include_marker:
        payload["__typename"] = "PagesCometLaunchpointUnifiedQueryPagesListRedesignedQuery"
    return payload


class FakeRequest:
    def __init__(self, friendly_name=""):
        self.headers = {"x-fb-friendly-name": friendly_name} if friendly_name else {}
        self.post_data = ""

    async def all_headers(self):
        return dict(self.headers)


class FakeResponse:
    url = "https://www.facebook.com/api/graphql/"

    def __init__(self, payload, friendly_name=""):
        self.payload = payload
        self.request = FakeRequest(friendly_name)

    async def text(self):
        return json.dumps(self.payload)


class FakePage:
    def __init__(self, batches):
        self.batches = list(batches)
        self.batch_index = 0
        self.response_handler = None
        self.scroll_calls = 0

    def on(self, event, handler):
        assert event == "response"
        self.response_handler = handler

    async def _emit_next_batch(self):
        if self.response_handler is None or self.batch_index >= len(self.batches):
            return
        batch = self.batches[self.batch_index]
        self.batch_index += 1
        if isinstance(batch, tuple):
            nodes, include_marker, friendly_name = batch
        else:
            nodes, include_marker, friendly_name = batch, True, ""
        payload = _graphql_payload(nodes, include_marker=include_marker)
        task = self.response_handler(FakeResponse(payload, friendly_name=friendly_name))
        if task is not None:
            await task

    async def goto(self, *args, **kwargs):
        await self._emit_next_batch()

    async def wait_for_selector(self, *args, **kwargs):
        return None

    async def evaluate(self, script, *args):
        script_text = str(script)
        if "scrollBy" in script_text or "scrollTop" in script_text:
            self.scroll_calls += 1
            await self._emit_next_batch()
        if "document.readyState" in script_text:
            return True
        return True


class FakeBrowser:
    async def close(self):
        return None


class FakePlaywright:
    async def stop(self):
        return None


def test_page_discovery_collects_graphql_pages_after_scroll(monkeypatch):
    async def run():
        first_batch = [_page_node(index) for index in range(1, 21)]
        second_batch = [_page_node(index) for index in range(21, 31)]
        fake_page = FakePage(
            [
                first_batch,
                (
                    second_batch,
                    False,
                    "PagesCometLaunchpointUnifiedQueryPagesListRedesignedQuery",
                ),
            ]
        )

        async def launch_browser_session(cookies_json, proxy_url=""):
            return FakePlaywright(), FakeBrowser(), object(), fake_page

        async def no_op(*args, **kwargs):
            return None

        async def false_async(*args, **kwargs):
            return False

        async def empty_text(*args, **kwargs):
            return ""

        async def empty_cards(*args, **kwargs):
            return []

        monkeypatch.setattr(playwright_engine, "PAGE_DISCOVERY_SCROLLS", 2)
        monkeypatch.setattr(playwright_engine, "PAGE_DISCOVERY_GRAPHQL_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(playwright_engine, "launch_browser_session", launch_browser_session)
        monkeypatch.setattr(playwright_engine, "_acquire_cookie_session_guard", no_op)
        monkeypatch.setattr(playwright_engine, "_release_cookie_session_guard", no_op)
        monkeypatch.setattr(playwright_engine, "_enable_fast_discovery_mode", no_op)
        monkeypatch.setattr(playwright_engine, "_resume_facebook_cookie_session", no_op)
        monkeypatch.setattr(playwright_engine, "_page_looks_logged_out", false_async)
        monkeypatch.setattr(playwright_engine, "_facebook_security_block_detail", empty_text)
        monkeypatch.setattr(playwright_engine, "_extract_page_cards", empty_cards)
        monkeypatch.setattr(playwright_engine, "_extract_page_links", empty_cards)
        monkeypatch.setattr(playwright_engine, "_smart_wait", no_op)

        ok, pages, detail = await playwright_engine.discover_facebook_pages("[]")

        assert ok is True
        assert detail == ""
        assert len(pages) == 30
        assert fake_page.scroll_calls >= 1
        assert pages[0]["name"] == "Managed Page 01"
        assert pages[-1]["name"] == "Managed Page 30"

    asyncio.run(run())

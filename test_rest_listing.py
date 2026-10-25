"""Постраничный REST-обход: первые файлы доступны до завершения списка."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from discovery import stream_source


class RestListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_page_is_yielded_before_next_page_and_failure_keeps_it(self):
        calls = []
        async def handle(request):
            self.assertEqual(request.headers["Authorization"], "OAuth fixture-token")
            calls.append(dict(request.url.params))
            if len(calls) == 1:
                return httpx.Response(200, json={"type": "dir", "_embedded": {"total": 2, "items": [
                    {"path": "disk:/Фото/a.jpg", "type": "file", "size": 3}]}})
            return httpx.Response(503)
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handle), **kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch("httpx.AsyncClient", side_effect=client), \
             patch("discovery.asyncio.create_subprocess_exec", side_effect=AssertionError("REST не должен ждать буфер rclone lsjson")):
            stream = stream_source(":yandex:/Фото/", Path(tmp), {"backend": "rest", "oauth_token": "fixture-token"}, {})
            self.assertEqual(await stream.__anext__(), {"Path": "a.jpg", "Size": 3, "IsDir": False})
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(RuntimeError, "503"):
                await stream.__anext__()
            self.assertEqual(calls[1]["offset"], "1")
            self.assertNotIn("fixture-token", (Path(tmp) / "listing.jsonl").read_text())

    async def test_pagination_subfolders_and_empty_directories(self):
        async def handle(request):
            params = request.url.params
            path, offset = params["path"], int(params["offset"])
            if path == "disk:/Фото" and offset == 0:
                items = [{"path": "disk:/Фото/Пустая", "type": "dir"},
                         {"path": "disk:/Фото/a.jpg", "type": "file", "size": 3}]
                total = 3
            elif path == "disk:/Фото" and offset == 2:
                items, total = [{"path": "disk:/Фото/b.jpg", "type": "file", "size": 4}], 3
            elif path == "disk:/Фото/Пустая":
                items, total = [], 0
            else:
                raise AssertionError(dict(params))
            return httpx.Response(200, json={"type": "dir", "_embedded": {"items": items, "total": total}})
        original = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp, patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            found = [item async for item in stream_source(":yandex:/Фото/", Path(tmp), {"backend": "rest", "oauth_token": "fixture-token"}, {})]
        self.assertEqual(found, [{"Path": "Пустая", "Size": 0, "IsDir": True},
                                 {"Path": "a.jpg", "Size": 3, "IsDir": False},
                                 {"Path": "b.jpg", "Size": 4, "IsDir": False}])

    async def test_cancellation_interrupts_request_without_background_thread(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        original = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp, patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
            stream = stream_source(":yandex:/Фото/", Path(tmp), {"backend": "rest", "oauth_token": "fixture-token"}, {})
            pending = asyncio.create_task(stream.__anext__())
            await asyncio.wait_for(entered.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(cancelled.is_set())
            await stream.aclose()

    async def test_incomplete_or_unsafe_page_is_not_success(self):
        cases = [
            {"type": "dir", "_embedded": {"items": [], "total": 2}},
            {"type": "dir", "_embedded": {"items": [{"path": "disk:/Other/a", "type": "file", "size": 1}], "total": 1}},
            {"type": "dir", "_embedded": {"items": [{"path": "disk:/Фото/../a", "type": "file", "size": 1}], "total": 1}},
        ]
        original = httpx.AsyncClient
        for page in cases:
            with self.subTest(page=page), tempfile.TemporaryDirectory() as tmp:
                with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page)), **kw)):
                    with self.assertRaises((RuntimeError, ValueError)):
                        async for _ in stream_source(":yandex:/Фото/", Path(tmp), {"backend": "rest", "oauth_token": "fixture-token"}, {}):
                            pass

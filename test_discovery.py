import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from discovery import Inventory, stream_source


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_item_before_process_exits_and_stops_on_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            item = {"Path": "Фото.jpg", "Size": 3, "IsDir": False}
            text = "[\n" + json.dumps(item) + ",\n"
            original = asyncio.create_subprocess_exec
            processes = []
            async def launch(*args, **kwargs):
                self.assertEqual(args[args.index("--timeout") + 1], "0.3s")
                self.assertIn("ListR", args)
                code = f"import sys,time; sys.stdout.write({text!r}); sys.stdout.flush(); time.sleep(60)"
                process = await original(sys.executable, "-c", code, **kwargs)
                processes.append(process)
                return process
            with patch("discovery.asyncio.create_subprocess_exec", side_effect=launch):
                stream = stream_source("remote:/", folder, {"listing_timeout": .3}, os.environ.copy())
                self.assertEqual(await anext(stream), item)
                self.assertIsNone(processes[0].returncode)
                with self.assertRaisesRegex(RuntimeError, "ожидания новых"):
                    await anext(stream)
            self.assertIsNotNone(processes[0].returncode)

    async def test_nonzero_exit_preserves_items_but_does_not_signal_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            item = {"Path": "found.jpg", "Size": 3, "IsDir": False}
            text = "[\n" + json.dumps(item) + "\n]\n"
            original = asyncio.create_subprocess_exec
            async def launch(*args, **kwargs):
                code = f"import sys; sys.stdout.write({text!r}); sys.stdout.flush(); sys.exit(5)"
                return await original(sys.executable, "-c", code, **kwargs)
            found = []
            with patch("discovery.asyncio.create_subprocess_exec", side_effect=launch):
                with self.assertRaisesRegex(RuntimeError, "код 5"):
                    async for record in stream_source("remote:/", folder, {}, os.environ.copy()):
                        found.append(record)
            self.assertEqual(found, [item])

    async def test_closing_iterator_stops_discovery_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            text = '[\n{"Path":"a","Size":1,"IsDir":false},\n'
            original = asyncio.create_subprocess_exec
            processes = []
            async def launch(*args, **kwargs):
                code = f"import sys,time; sys.stdout.write({text!r}); sys.stdout.flush(); time.sleep(60)"
                process = await original(sys.executable, "-c", code, **kwargs)
                processes.append(process)
                return process
            with patch("discovery.asyncio.create_subprocess_exec", side_effect=launch):
                stream = stream_source("remote:/", folder, {}, os.environ.copy())
                await anext(stream)
                await stream.aclose()
            self.assertIsNotNone(processes[0].returncode)

    async def test_truncated_array_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = asyncio.create_subprocess_exec
            async def launch(*args, **kwargs):
                return await original(sys.executable, "-c", "print('[\\n{\"Path\":\"a\",\"Size\":1,\"IsDir\":false}')", **kwargs)
            with patch("discovery.asyncio.create_subprocess_exec", side_effect=launch):
                stream = stream_source("remote:/", Path(tmp), {}, os.environ.copy())
                self.assertEqual((await anext(stream))["Path"], "a")
                with self.assertRaisesRegex(RuntimeError, "оборвался"):
                    await anext(stream)


class InventoryTests(unittest.TestCase):
    def test_commits_each_record_and_keeps_partial_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.sqlite3"
            first = Inventory(path)
            first.remember({"Path": "a.jpg", "Size": 1, "IsDir": False})
            reader = Inventory(path)
            self.assertEqual([item["Path"] for item in reader.cached()], ["a.jpg"])
            reader.close()
            first.close()
            second = Inventory(path)
            second.remember({"Path": "b.jpg", "Size": 2, "IsDir": False})
            second.close()
            third = Inventory(path)
            self.assertEqual([item["Path"] for item in third.cached()], ["a.jpg", "b.jpg"])
            third.remember({"Path": "b.jpg", "Size": 3, "IsDir": False})
            third.finish()
            self.assertEqual(list(third.cached()), [{"Path": "b.jpg", "Size": 3, "IsDir": False}])
            third.close()

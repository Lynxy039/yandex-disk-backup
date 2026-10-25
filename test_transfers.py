import asyncio
from pathlib import Path
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import transfers


class ConfigTests(unittest.TestCase):
    def test_uses_in_memory_config_instead_of_reserved_windows_device(self):
        for device in ("nul", "/dev/null"):
            with self.subTest(device=device), patch("transfers.os.devnull", device):
                flags = transfers.flags({})
                self.assertEqual(flags[flags.index("--config") + 1], "")


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloads_first_file_before_discovery_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            finished_first = asyncio.Event()
            entries = []
            async def discover():
                yield {"path": "first.jpg", "size": 3, "status": "pending", "attempts": 0}
                await asyncio.wait_for(finished_first.wait(), timeout=2)
                yield {"path": "second.jpg", "size": 3, "status": "pending", "attempts": 0}
            async def attempt(source, temporary, item, config, env, active):
                temporary.write_bytes(b"abc")
                if item["path"] == "first.jpg":
                    finished_first.set()
                return None
            with patch.object(transfers, "attempt", side_effect=attempt):
                result = await transfers.run_files(entries, "remote:/", root, {}, {}, lambda _: None,
                                                   discover=discover())
            self.assertTrue(result["discovery_complete"])
            self.assertEqual([item["status"] for item in entries], ["downloaded", "downloaded"])

    async def test_discovery_failure_does_not_discard_already_found_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = []
            async def discover():
                yield {"path": "found.jpg", "size": 3, "status": "pending", "attempts": 0}
                raise RuntimeError("Список оборвался")
            async def attempt(source, temporary, item, config, env, active):
                temporary.write_bytes(b"abc")
                return None
            with patch.object(transfers, "attempt", side_effect=attempt):
                result = await transfers.run_files(entries, "remote:/", root, {}, {}, lambda _: None,
                                                   discover=discover())
            self.assertFalse(result["discovery_complete"])
            self.assertIn("Список оборвался", result["discovery_error"])
            self.assertEqual((root / "found.jpg").read_bytes(), b"abc")

    async def test_new_size_for_cached_path_is_downloaded_serially(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{"path": "changed.jpg", "size": 1, "status": "pending", "attempts": 0}]
            started, updated = asyncio.Event(), asyncio.Event()
            calls = []
            async def discover():
                await started.wait()
                yield {"path": "changed.jpg", "size": 2, "status": "pending", "attempts": 0}
                updated.set()
            async def attempt(source, temporary, item, config, env, active):
                calls.append(item["size"])
                if item["size"] == 1:
                    started.set()
                    await updated.wait()
                temporary.write_bytes(b"x" * item["size"])
                return None
            with patch.object(transfers, "attempt", side_effect=attempt):
                result = await asyncio.wait_for(transfers.run_files(entries, "remote:/", root, {}, {}, lambda _: None,
                                                                    discover=discover()), timeout=3)
            self.assertEqual(calls, [1, 2])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["size"], 2)
            self.assertEqual((root / "changed.jpg").read_bytes(), b"xx")

    async def test_retries_deferred_file_only_after_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{"path": name, "size": 3, "status": "pending", "attempts": 0}
                       for name in ("hang.jpg", "ok.jpg")]
            calls, events = [], []
            async def attempt(source, temporary, item, config, env, active):
                calls.append(item["path"])
                if item["path"] == "hang.jpg" and item["attempts"] == 1:
                    return "Нет данных 8 с"
                temporary.write_bytes(b"abc")
                return None
            with patch.object(transfers, "attempt", side_effect=attempt):
                await transfers.run_files(entries, "remote:/", root, {"transfers": 1, "retry_passes": 1}, {}, events.append)
            self.assertEqual(calls, ["hang.jpg", "ok.jpg", "hang.jpg"])
            self.assertEqual([item["status"] for item in entries], ["downloaded", "downloaded"])
            self.assertEqual((root / "hang.jpg").read_bytes(), b"abc")
            self.assertTrue(any(event.get("state") == "deferred" for event in events))

    async def test_stalled_process_is_killed_and_partial_file_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file.jpg"
            target.write_bytes(b"old")
            entries = [{"path": "file.jpg", "size": 5, "status": "pending", "attempts": 0},
                       {"path": "next.jpg", "size": 3, "status": "pending", "attempts": 0}]
            processes = []
            original = asyncio.create_subprocess_exec
            async def launch(*args, **kwargs):
                staging = args[3]
                if "file.jpg" in args[2]:
                    code = f"from pathlib import Path; import time; Path({staging!r}).write_bytes(b'x'); time.sleep(60)"
                else:
                    code = f"from pathlib import Path; Path({staging!r}).write_bytes(b'abc')"
                process = await original(sys.executable, "-c", code, **kwargs)
                processes.append(process)
                return process
            start = time.monotonic()
            with patch("transfers.asyncio.create_subprocess_exec", side_effect=launch):
                await transfers.run_files(entries, "remote:/", root,
                                          {"transfers": 1, "retry_passes": 0, "idle_timeout": .3, "file_timeout": 1},
                                          os.environ.copy(), lambda _: None)
            self.assertLess(time.monotonic() - start, 4)
            self.assertEqual(entries[0]["status"], "failed")
            self.assertEqual(entries[1]["status"], "downloaded")
            self.assertEqual(target.read_bytes(), b"old")
            self.assertTrue(all(process.returncode is not None for process in processes))
            self.assertEqual(list(root.glob(".yadisk-*")), [])

    async def test_rclone_removing_own_part_is_deferred_not_local_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{"path": "gone.jpg", "size": 5, "status": "pending", "attempts": 0}]
            original = asyncio.create_subprocess_exec
            async def launch(*args, **kwargs):
                code = ("import sys, time\nfrom pathlib import Path\n"
                        f"Path({args[3]!r}).write_bytes(b'x'); time.sleep(.3); Path({args[3]!r}).unlink()\n"
                        "print('{\"msg\":\"503 Service Unavailable\"}', file=sys.stderr); sys.exit(1)")
                return await original(sys.executable, "-c", code, **kwargs)
            with patch("transfers.asyncio.create_subprocess_exec", side_effect=launch):
                await transfers.run_files(entries, "remote:/", root, {"retry_passes": 0}, {}, lambda _: None)
            self.assertEqual(entries[0]["status"], "failed")
            self.assertEqual(entries[0]["reason"], "503 Service Unavailable")

    async def test_total_deadline_applies_even_when_data_keeps_arriving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{"path": "slow.jpg", "size": 100, "status": "pending", "attempts": 0}]
            original = asyncio.create_subprocess_exec
            async def launch(*args, **kwargs):
                code = ("import time\n"
                        f"with open({args[3]!r}, 'wb', buffering=0) as f:\n"
                        " for _ in range(100): f.write(b'x'); time.sleep(.05)\n")
                return await original(sys.executable, "-c", code, **kwargs)
            with patch("transfers.asyncio.create_subprocess_exec", side_effect=launch):
                await transfers.run_files(entries, "remote:/", root,
                                          {"retry_passes": 0, "idle_timeout": .5, "file_timeout": .6},
                                          os.environ.copy(), lambda _: None)
            self.assertIn("Лимит попытки", entries[0]["reason"])
            self.assertFalse((root / "slow.jpg").exists())

    async def test_cancellation_stops_active_process_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{"path": "hang.jpg", "size": 100, "status": "pending", "attempts": 0}]
            original = asyncio.create_subprocess_exec
            started = asyncio.Event()
            processes = []
            async def launch(*args, **kwargs):
                process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
                processes.append(process)
                started.set()
                return process
            with patch("transfers.asyncio.create_subprocess_exec", side_effect=launch):
                task = asyncio.create_task(transfers.run_files(entries, "remote:/", root, {},
                                                               os.environ.copy(), lambda _: None))
                await asyncio.wait_for(started.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(all(process.returncode is not None for process in processes))
            self.assertEqual(list(root.iterdir()), [])

    async def test_wrong_size_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{"path": "bad.jpg", "size": 10, "status": "pending", "attempts": 0}]
            async def attempt(source, temporary, item, config, env, active):
                temporary.write_bytes(b"short")
                return None
            with patch.object(transfers, "attempt", side_effect=attempt):
                await transfers.run_files(entries, "remote:/", root, {"retry_passes": 0}, {}, lambda _: None)
            self.assertEqual(entries[0]["status"], "failed")
            self.assertFalse((root / "bad.jpg").exists())

    async def test_skips_existing_file_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ready.jpg").write_bytes(b"abc")
            entries = [{"path": "ready.jpg", "size": 3, "status": "pending", "attempts": 0}]
            with patch.object(transfers, "attempt") as attempt:
                await transfers.run_files(entries, "remote:/", root, {}, {}, lambda _: None)
            attempt.assert_not_called()
            self.assertEqual(entries[0]["status"], "present")


class PathTests(unittest.TestCase):
    def test_rejects_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backup"
            root.mkdir()
            (root / "link").symlink_to(Path(tmp), target_is_directory=True)
            for name in ("../outside.jpg", "/absolute.jpg", "link/outside.jpg"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    transfers.safe_path(root, name)

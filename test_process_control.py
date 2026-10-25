import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import process_control


class ProcessControlTests(unittest.IsolatedAsyncioTestCase):
    def test_windows_uses_new_process_group_not_unix_session(self):
        self.assertEqual(process_control.spawn_options(True), {"creationflags": 0x00000200})
        self.assertEqual(process_control.spawn_options(False), {"start_new_session": True})

    async def test_windows_emergency_cleanup_uses_taskkill_tree(self):
        for code in (0, 1):
            with self.subTest(taskkill_code=code):
                process = SimpleNamespace(pid=12345, returncode=None, kill=Mock(), wait=AsyncMock(return_value=0))
                killer = SimpleNamespace(wait=AsyncMock(return_value=code))
                with patch("process_control.asyncio.create_subprocess_exec", return_value=killer) as launch:
                    await process_control.kill_tree(process, True)
                launch.assert_awaited_once_with("taskkill", "/PID", "12345", "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self.assertEqual(process.kill.call_count, int(code != 0))
                process.wait.assert_awaited_once()

    async def test_no_stop_file_preserves_result(self):
        async def work():
            return 42
        self.assertEqual(await process_control.run_with_stop(work()), 42)

    async def test_stop_marker_cancels_work_and_waits_for_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "stop"
            started, cleaned = asyncio.Event(), asyncio.Event()
            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    cleaned.set()
            async def request_stop():
                await started.wait()
                marker.touch()
            request = asyncio.create_task(request_stop())
            try:
                with self.assertRaises(process_control.StopRequested):
                    await asyncio.wait_for(process_control.run_with_stop(work(), marker), 2)
                self.assertTrue(cleaned.is_set())
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

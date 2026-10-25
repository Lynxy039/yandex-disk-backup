import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Input, Static

from tui import YandexApp, JobRow, LogTail


async def wait_until(pilot, predicate):
    for _ in range(100):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("Не дождались состояния интерфейса")


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_edits_and_saves_all_settings_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            app = YandexApp(path)
            async with app.run_test(size=(120, 45)) as pilot:
                app.query_one("#login", Input).value = "мой-логин"
                app.query_one("#password", Input).value = "секрет"
                row = app.query_one(JobRow)
                row.query_one(".source", Input).value = "/Фото/Отпуск"
                row.query_one(".destination", Input).value = str(Path(tmp) / "backup")
                await pilot.click("#save")
                await pilot.pause()
                saved = json.loads(path.read_text())
                self.assertEqual(saved["jobs"][0]["source"], "/Фото/Отпуск")
                self.assertEqual(saved["app_password"], "секрет")
                self.assertTrue(app.query_one("#password", Input).password)
                self.assertEqual(saved["idle_timeout"], 8)
                self.assertEqual(saved["file_timeout"], 20)
                self.assertEqual(saved["retry_passes"], 2)
                self.assertNotIn("low_level_retries", saved)
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_rejects_bad_settings_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            app = YandexApp(path)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.click("#start")
                await pilot.pause()
                self.assertFalse(app.running)
                self.assertFalse(path.exists())
                self.assertIn("login", str(app.query_one("#form-message", Static).render()))

    async def test_adds_and_removes_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = YandexApp(Path(tmp) / "settings.json")
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.click("#add")
                await pilot.pause()
                self.assertEqual(len(app.query(JobRow)), 2)
                await app.query(JobRow).last().remove()
                self.assertEqual(len(app.query(JobRow)), 1)

    async def test_stop_button_remains_visible_in_small_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = YandexApp(Path(tmp) / "settings.json")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.click("#--content-tab-run-tab")
                await pilot.pause()
                button = app.query_one("#stop")
                self.assertGreater(button.region.height, 0)
                self.assertLessEqual(button.region.bottom, 23)
                self.assertLessEqual(app.query_one("#stats").region.bottom, button.region.y)

    async def test_runs_real_backend_and_displays_completed_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "Фото.jpg").write_bytes(b"photo")
            config = root / "settings.json"
            config.write_text(json.dumps({"login": "user", "app_password": "secret", "jobs": [
                {"source": "/Фото", "destination": str(root / "backup")}] }))
            config.chmod(0o600)
            original = asyncio.create_subprocess_exec
            async def launch(python, *args, **kwargs):
                code = f"import download; download.remote_source=lambda _: {str(source)!r}; raise SystemExit(download.main())"
                return await original(python, "-u", "-c", code, str(config), "--events", **kwargs)
            app = YandexApp(config)
            with patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual((root / "backup/Фото.jpg").read_bytes(), b"photo")
                    self.assertIn("проверено", str(app.query_one("#run-status", Static).render()))
                    self.assertEqual(app.exit_code, 0)
                    self.assertTrue(list((root / "logs").glob("*/01/downloaded.jsonl")))

    async def test_partial_discovery_still_downloads_and_shows_incomplete_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "found.jpg").write_bytes(b"photo")
            config = root / "settings.json"
            config.write_text(json.dumps({"login": "user", "app_password": "secret", "jobs": [
                {"source": "/Фото", "destination": str(root / "backup")}] }))
            config.chmod(0o600)
            original = asyncio.create_subprocess_exec
            async def launch(python, *args, **kwargs):
                code = ("import asyncio,download\nfrom pathlib import Path\n"
                        f"download.remote_source=lambda _: {str(source)!r}\n"
                        "async def discover(*args):\n"
                        " yield {'Path':'found.jpg','Size':5,'IsDir':False}\n"
                        f" while not Path({str(root / 'backup/found.jpg')!r}).exists(): await asyncio.sleep(.02)\n"
                        " raise RuntimeError('Обрыв списка')\n"
                        "download.list_source=discover\nraise SystemExit(download.main())\n")
                return await original(python, "-u", "-c", code, str(config), "--events", **kwargs)
            app = YandexApp(config)
            with patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual((root / "backup/found.jpg").read_bytes(), b"photo")
                    self.assertEqual(app.exit_code, 1)
                    self.assertIn("неполный", str(app.query_one("#run-status", Static).render()))
                    summary = json.loads(next((root / "logs").glob("*/01/summary.json")).read_text())
                    self.assertTrue(summary["known_files_verified"])
                    self.assertFalse(summary["manifest_complete"])

    async def test_stop_interrupts_backend_and_saves_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "settings.json"
            config.write_text(json.dumps({"login": "user", "app_password": "secret", "jobs": [
                {"source": "/Фото", "destination": str(root / "backup")}] }))
            config.chmod(0o600)
            original = asyncio.create_subprocess_exec
            async def launch(python, *args, **kwargs):
                code = ("import download, transfers, asyncio, sys\nfrom pathlib import Path\n"
                        "async def discover(*args):\n"
                        " yield {'Path':'hang.jpg','Size':10,'IsDir':False}\n"
                        "download.list_source=discover\n"
                        "async def attempt(*args):\n"
                        " p = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)')\n"
                        f" Path({str(root / 'child.pid')!r}).write_text(str(p.pid))\n"
                        " try: return await p.wait()\n"
                        " finally: await transfers.stop_process(p)\n"
                        "transfers.attempt=attempt\nraise SystemExit(download.main())\n")
                return await original(python, "-u", "-c", code, str(config), "--events", **kwargs)
            app = YandexApp(config)
            with patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: (root / "child.pid").exists())
                    await pilot.press("f8")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual(app.exit_code, 130)
                    report = next((root / "logs").glob("*/01/summary.json"))
                    self.assertEqual(json.loads(report.read_text())["status"], "interrupted")
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((root / "child.pid").read_text()), 0)

    async def test_progress_events_and_failure_do_not_show_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = YandexApp(Path(tmp) / "settings.json")
            async with app.run_test(size=(120, 45)):
                app.handle_event({"type": "job_started", "source": "/Фото", "destination": "/backup", "folder": tmp})
                app.handle_event({"type": "phase", "phase": "copy", "log": str(Path(tmp) / "copy.jsonl")})
                app.handle_log({"stats": {"bytes": 1024, "totalBytes": 2048, "speed": 512, "transfers": 1,
                                          "totalTransfers": 2, "errors": 1, "deferred": 1,
                                          "processed": 1, "passTotal": 2,
                                          "transferring": [{"name": "hang.jpg", "idle": 6.5}]}})
                self.assertIn("Отложено: 1", str(app.query_one("#stats", Static).render()))
                self.assertIn("6.5 с", str(app.query_one("#active-files", Static).render()))
                self.assertIn("512", str(app.query_one("#stats", Static).render()))
                app.handle_log({"stats": {"discovering": True, "listed": 2, "discovery_idle": 10}})
                self.assertIn("Обход + скачивание", str(app.query_one("#phase", Static).render()))
                app.handle_log({"stats": {"discovery_complete": False, "listed": 2}})
                self.assertIn("Список неполный", str(app.query_one("#phase", Static).render()))
                app.handle_event({"type": "job_finished", "folder": tmp,
                                  "result": {"status": "failed", "source": "/Фото"}})
                app.handle_event({"type": "run_finished", "status": "failed", "folder": tmp})
                self.assertIn("ошиб", str(app.query_one("#run-status", Static).render()).lower())


class TailTests(unittest.TestCase):
    def test_keeps_incomplete_json_until_next_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copy.jsonl"
            tail = LogTail(path)
            path.write_bytes(b'{"msg":"a')
            self.assertEqual(tail.read(), [])
            with path.open("ab") as output:
                output.write(b'"}\n{"msg":"b"}\n')
            self.assertEqual([event["msg"] for event in tail.read()], ["a", "b"])
            self.assertEqual(tail.read(), [])

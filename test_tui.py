import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Checkbox, Input, Select, Static

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

    async def test_enables_encryption_and_can_disable_after_unlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            app = YandexApp(path)
            async with app.run_test(size=(120, 45)):
                app.query_one("#login", Input).value = "user"
                app.query_one("#password", Input).value = "app-secret"
                app.query_one(JobRow).query_one(".destination", Input).value = str(Path(tmp) / "backup")
                app.query_one("#encrypt-password", Checkbox).value = True
                app.query_one("#master-password", Input).value = "случайные слова для защиты"
                app.query_one("#master-confirm", Input).value = "не совпадает"
                self.assertFalse(app.save_settings())
                self.assertFalse(path.exists())
                app.query_one("#master-confirm", Input).value = "случайные слова для защиты"
                self.assertTrue(app.save_settings())
                saved = json.loads(path.read_text())
                self.assertNotIn("app_password", saved)
                self.assertIn("app_password_encrypted", saved)
                self.assertNotIn("app-secret", path.read_text())
                self.assertNotIn("случайные слова", path.read_text())
            original = path.read_bytes()
            reopened = YandexApp(path)
            async with reopened.run_test(size=(120, 45)):
                self.assertEqual(reopened.query_one("#password", Input).value, "")
                reopened.query_one("#encrypt-password", Checkbox).value = False
                reopened.query_one("#master-password", Input).value = "неверный мастер-пароль"
                self.assertFalse(reopened.save_settings())
                self.assertEqual(path.read_bytes(), original)
                reopened.query_one("#master-password", Input).value = "случайные слова для защиты"
                self.assertTrue(reopened.save_settings())
                saved = json.loads(path.read_text())
                self.assertEqual(saved["app_password"], "app-secret")
                self.assertNotIn("app_password_encrypted", saved)

    async def test_stale_backend_event_does_not_clear_token_or_touch_unmounted_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = YandexApp(Path(tmp) / "settings.json")
            async with app.run_test(size=(100, 32)) as pilot:
                selector = app.query_one("#backend", Select)
                selector.value = "rest"
                await wait_until(pilot, lambda: "OAuth" in str(app.query_one("#credential-label").render()))
                app.query_one("#password", Input).value = "new-token"
                app.backend_changed(Select.Changed(selector, "webdav"))
                self.assertEqual(app.query_one("#password", Input).value, "new-token")
                await app.query_one("#credential-label").remove()
                app.backend_changed(Select.Changed(selector, "rest"))

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
                code = f"import download; download.remote_source=lambda *args: {str(source)!r}; raise SystemExit(download.main())"
                return await original(python, "-u", "-c", code, *args[2:], **kwargs)
            app = YandexApp(config)
            with patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual((root / "backup/Фото.jpg").read_bytes(), b"photo")
                    self.assertIn("проверено", str(app.query_one("#run-status", Static).render()))
                    self.assertEqual(app.exit_code, 0)
                    self.assertTrue(list((root / "logs").glob("*/01/downloaded.jsonl")))

    async def test_encrypted_settings_launch_real_backend_through_private_pipe(self):
        import passwords
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "Фото.jpg").write_bytes(b"photo")
            config = root / "settings.json"
            passwords.save_config(config, {"login": "user", "app_password_encrypted":
                passwords.encrypt_password("app-secret", "случайные слова для защиты"), "jobs": [
                {"source": "/Фото", "destination": str(root / "backup")}]})
            original = asyncio.create_subprocess_exec
            async def launch(python, *args, **kwargs):
                self.assertIn("--password-stdin", args)
                self.assertNotIn("app-secret", str(args))
                self.assertNotIn("случайные слова", str(args))
                self.assertEqual(kwargs["stdin"], asyncio.subprocess.PIPE)
                code = f"import download; download.remote_source=lambda *args: {str(source)!r}; raise SystemExit(download.main())"
                return await original(python, "-u", "-c", code, *args[2:], **kwargs)
            app = YandexApp(config)
            with patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    app.query_one("#master-password", Input).value = "случайные слова для защиты"
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual((root / "backup/Фото.jpg").read_bytes(), b"photo")
                    self.assertEqual(app.exit_code, 0)
                    self.assertNotIn("app_password", json.loads(config.read_text()))
                    self.assertNotIn("app-secret", config.read_text())
                    app.append_log("app-secret случайные слова для защиты")
                    self.assertEqual(app.query_one("#run-log").lines[-1].text, "*** ***")

    async def test_rest_switch_encrypts_token_and_launches_without_webdav_password(self):
        import passwords
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "Фото.jpg").write_bytes(b"photo")
            config = root / "settings.json"
            passwords.save_config(config, {"login": "user", "app_password": "old-webdav-secret", "jobs": [
                {"source": "/Фото", "destination": str(root / "backup")}]})
            original = asyncio.create_subprocess_exec
            async def launch(python, *args, **kwargs):
                self.assertIn("--password-stdin", args)
                self.assertNotIn("test-polygon-token", str(args))
                code = ("import download\n"
                        f"download.remote_source=lambda *args: {str(source)!r}\n"
                        "async def listing(*args):\n"
                        " yield {'Path':'Фото.jpg','Size':5,'IsDir':False}\n"
                        "download.list_source=listing\nraise SystemExit(download.main())")
                return await original(python, "-u", "-c", code, *args[2:], **kwargs)
            app = YandexApp(config)
            with patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    app.query_one("#backend", Select).value = "rest"
                    await wait_until(pilot, lambda: "OAuth" in str(app.query_one("#credential-label").render()))
                    self.assertEqual(app.query_one("#password", Input).value, "")
                    self.assertIn("OAuth", str(app.query_one("#credential-label").render()))
                    app.query_one("#password", Input).value = "test-polygon-token"
                    app.query_one("#encrypt-password", Checkbox).value = True
                    app.query_one("#master-password", Input).value = "несколько случайных слов"
                    app.query_one("#master-confirm", Input).value = "несколько случайных слов"
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual(app.exit_code, 0)
                    self.assertEqual((root / "backup/Фото.jpg").read_bytes(), b"photo")
            saved = json.loads(config.read_text())
            self.assertEqual(saved["backend"], "rest")
            self.assertIn("oauth_token_encrypted", saved)
            self.assertNotIn("oauth_token", saved)
            self.assertNotIn("app_password", saved)
            self.assertNotIn("old-webdav-secret", config.read_text())
            self.assertNotIn("test-polygon-token", config.read_text())
            reopened = YandexApp(config)
            async with reopened.run_test(size=(100, 32)) as pilot:
                self.assertEqual(reopened.query_one("#backend", Select).value, "rest")
                reopened.query_one("#master-password", Input).value = "несколько случайных слов"
                reopened.query_one("#encrypt-password", Checkbox).value = False
                self.assertTrue(reopened.save_settings())
                self.assertEqual(json.loads(config.read_text())["oauth_token"], "test-polygon-token")
                reopened.query_one("#backend", Select).value = "webdav"
                await wait_until(pilot, lambda: "OAuth" not in str(reopened.query_one("#credential-label").render()))
                self.assertEqual(reopened.query_one("#password", Input).value, "")
                self.assertFalse(reopened.save_settings())
                self.assertEqual(json.loads(config.read_text())["backend"], "rest")

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
                        f"download.remote_source=lambda *args: {str(source)!r}\n"
                        "async def discover(*args):\n"
                        " yield {'Path':'found.jpg','Size':5,'IsDir':False}\n"
                        f" while not Path({str(root / 'backup/found.jpg')!r}).exists(): await asyncio.sleep(.02)\n"
                        " raise RuntimeError('Обрыв списка')\n"
                        "download.list_source=discover\nraise SystemExit(download.main())\n")
                return await original(python, "-u", "-c", code, *args[2:], **kwargs)
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
        await self.assert_backend_stop(os.name == "nt")

    async def test_windows_stop_marker_cancels_child_and_saves_reports(self):
        await self.assert_backend_stop(True)

    async def assert_backend_stop(self, file_control):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "settings.json"
            config.write_text(json.dumps({"login": "user", "app_password": "secret", "jobs": [
                {"source": "/Фото", "destination": str(root / "backup")}] }))
            config.chmod(0o600)
            original = asyncio.create_subprocess_exec
            stop_files = []
            async def launch(python, *args, **kwargs):
                if file_control:
                    self.assertIn("--stop-file", args)
                    stop_files.append(Path(args[args.index("--stop-file") + 1]))
                    self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
                    if os.name != "nt":
                        self.assertEqual(kwargs.pop("creationflags"), 0x00000200)
                        kwargs["start_new_session"] = True
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
                return await original(python, "-u", "-c", code, *args[2:], **kwargs)
            app = YandexApp(config)
            with patch("tui.WINDOWS", file_control), patch("tui.asyncio.create_subprocess_exec", side_effect=launch):
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("f5")
                    await wait_until(pilot, lambda: (root / "child.pid").exists())
                    await pilot.press("f8")
                    await wait_until(pilot, lambda: app.final_event and not app.running)
                    self.assertEqual(app.exit_code, 130)
                    report = next((root / "logs").glob("*/01/summary.json"))
                    self.assertEqual(json.loads(report.read_text())["status"], "interrupted")
                    pid = int((root / "child.pid").read_text())
                    if os.name == "nt":
                        import subprocess
                        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
                        self.assertNotIn(f'"{pid}"', result.stdout)
                    else:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                    for marker in stop_files:
                        self.assertFalse(marker.parent.exists())

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

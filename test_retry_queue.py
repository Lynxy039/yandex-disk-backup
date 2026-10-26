import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download
from process_control import StopRequested
from test_download import records


class RetryQueueTests(unittest.TestCase):
    def queue(self, path, names=("a.jpg", "b.jpg"), **changes):
        data = {"format": "yandex-disk-backup-retry", "version": 1, "login": "demo",
                "source": "/Фото", "manifest_complete": True,
                "files": [{"path": name, "size": 3} for name in names]}
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_retry_remembers_successes_after_photos_are_moved_and_never_lists_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue)
            job = {"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)}
            calls = []
            async def attempt(source, temporary, item, config, env, active):
                calls.append(item["path"])
                if item["path"] == "b.jpg":
                    return "500"
                temporary.write_bytes(b"abc")
                return None
            with patch.object(download, "list_source", side_effect=AssertionError("Полный обход запрещён")), \
                 patch.object(download, "Inventory", side_effect=AssertionError("Полный индекс запрещён")), \
                 patch("transfers.attempt", side_effect=attempt):
                first = download.run_job(job, root / "run1", {"login": "demo", "transfers": 1}, {})
            self.assertEqual(first["files"], 2)
            self.assertEqual(first["scope"], "retry_queue")
            self.assertEqual([f["path"] for f in json.loads(queue.read_text())["files"]], ["b.jpg"])
            (root / "photos/a.jpg").rename(root / "processed-a.jpg")
            calls.clear()
            async def success(source, temporary, item, config, env, active):
                calls.append(item["path"])
                temporary.write_bytes(b"abc")
                return None
            with patch.object(download, "list_source", side_effect=AssertionError("Полный обход запрещён")), \
                 patch("transfers.attempt", side_effect=success):
                second = download.run_job(job, root / "run2", {"login": "demo"}, {})
                (root / "photos/b.jpg").rename(root / "processed-b.jpg")
                empty = download.run_job(job, root / "run3", {"login": "demo"}, {})
            self.assertEqual(calls, ["b.jpg"])
            self.assertEqual(second["status"], "ok")
            self.assertEqual(empty["files"], 0)
            self.assertEqual(empty["status"], "ok")
            self.assertEqual(json.loads(queue.read_text())["files"], [])
            self.assertEqual((root / "processed-a.jpg").read_bytes(), b"abc")

    def test_normal_run_exports_failed_queue_with_account_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = [{"Path": "bad.jpg", "Size": 3, "IsDir": False}]
            with patch.object(download, "list_source", return_value=records(listing)), \
                 patch("transfers.attempt", return_value="500"):
                result = download.run_job({"source": "/Фото", "destination": str(root / "photos")},
                                          root / "run", {"login": "demo"}, {})
            queue = json.loads((root / "run/retry.json").read_text())
            self.assertEqual(queue["files"], [{"path": "bad.jpg", "size": 3}])
            self.assertEqual(queue["login"], "demo")
            self.assertEqual(queue["source"], "/Фото")
            self.assertTrue(queue["manifest_complete"])
            self.assertEqual(result["retry_queue"], str(root / "run/retry.json"))

    def test_legacy_failed_report_is_imported_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            old.mkdir()
            (old / "summary.json").write_text(json.dumps({"source": "/Фото", "manifest_complete": True}))
            original = json.dumps({"path": "bad.jpg", "size": 3, "reason": "500", "attempts": 5}) + "\n"
            (old / "failed.jsonl").write_text(original)
            job = {"source": "/Фото", "destination": str(root / "new-photos"), "retry_from": str(old / "failed.jsonl")}
            async def success(source, temporary, item, config, env, active):
                temporary.write_bytes(b"abc")
                return None
            with patch.object(download, "list_source", side_effect=AssertionError("Обход запрещён")), \
                 patch("transfers.attempt", side_effect=success):
                result = download.run_job(job, root / "run1", {"login": "demo"}, {})
                (root / "new-photos/bad.jpg").unlink()
                again = download.run_job(job, root / "run2", {"login": "demo"}, {})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(again["files"], 0)
            self.assertEqual((old / "failed.jsonl").read_text(), original)
            self.assertEqual(json.loads((old / "retry.json").read_text())["files"], [])

    def test_stop_checkpoints_only_still_pending_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue)
            async def attempt(source, temporary, item, config, env, active):
                if item["path"] == "b.jpg":
                    raise StopRequested
                temporary.write_bytes(b"abc")
                return None
            job = {"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)}
            with patch("transfers.attempt", side_effect=attempt), self.assertRaises(KeyboardInterrupt):
                download.run_job(job, root / "run", {"login": "demo", "transfers": 1}, {})
            self.assertEqual([f["path"] for f in json.loads(queue.read_text())["files"]], ["b.jpg"])

    def test_invalid_queue_never_falls_back_to_full_download_or_overwrites_input(self):
        changes = [{"login": "someone-else"}, {"source": "/Видео"}, {"version": 2},
                   {"files": [{"path": "../escape", "size": 3}]},
                   {"files": [{"path": "a", "size": True}]},
                   {"files": [{"path": "a", "size": 3}, {"path": "a", "size": 3}]}]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                queue = root / "retry.json"
                self.queue(queue, **change)
                original = queue.read_bytes()
                job = {"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)}
                with patch.object(download, "list_source", side_effect=AssertionError("Обход запрещён")), \
                     patch("transfers.attempt", side_effect=AssertionError("Загрузка запрещена")):
                    result = download.run_job(job, root / "run", {"login": "demo"}, {})
                self.assertEqual(result["status"], "failed")
                self.assertEqual(queue.read_bytes(), original)
                self.assertFalse((root / "run/retry.json").exists())

    def test_checkpoint_write_error_keeps_input_and_exposes_new_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue, names=("a.jpg",))
            original, save = queue.read_bytes(), download.save_json
            def save_or_fail(path, data):
                if path.resolve() == queue.resolve():
                    raise PermissionError("Очередь только для чтения")
                return save(path, data)
            async def success(source, temporary, item, config, env, active):
                temporary.write_bytes(b"abc")
                return None
            with patch.object(download, "save_json", side_effect=save_or_fail), patch("transfers.attempt", side_effect=success):
                result = download.run_job({"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)},
                                          root / "run", {"login": "demo"}, {})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(queue.read_bytes(), original)
            self.assertIn("Не удалось сохранить очередь", result["error"])
            self.assertEqual(json.loads((root / "run/retry.json").read_text())["files"], [])

    def test_initialization_error_keeps_all_requested_files_in_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue)
            check, count = download.transfers.safe_path, 0
            def fail_during_record(*args):
                nonlocal count
                count += 1
                if count == 3:
                    raise ValueError("Назначение изменилось после чтения очереди")
                return check(*args)
            with patch("transfers.safe_path", side_effect=fail_during_record):
                result = download.run_job({"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)},
                                          root / "run", {"login": "demo"}, {})
            self.assertEqual(result["status"], "failed")
            self.assertEqual([f["path"] for f in json.loads(queue.read_text())["files"]], ["a.jpg", "b.jpg"])

    def test_cli_retry_override_is_not_saved_into_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue, names=("a.jpg",))
            config = root / "settings.json"
            config.write_text(json.dumps({"login": "demo", "app_password": "secret", "jobs": [
                {"source": "/Фото", "destination": str(root / "photos")}]}))
            config.chmod(0o600)
            original = config.read_bytes()
            async def success(source, temporary, item, config, env, active):
                temporary.write_bytes(b"abc")
                return None
            old_mask = os.umask(0o077)
            try:
                with patch.object(sys, "argv", ["download.py", str(config), "--retry-from", str(queue)]), \
                     patch.object(download, "rclone_environment", return_value={}), \
                     patch("download.shutil.which", return_value="rclone"), \
                     patch.object(download, "list_source", side_effect=AssertionError("Обход запрещён")), \
                     patch("transfers.attempt", side_effect=success):
                    self.assertEqual(download.main(), 0)
            finally:
                os.umask(old_mask)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(json.loads(queue.read_text())["files"], [])

    def test_validate_rejects_invalid_type_shared_queue_and_queue_in_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue)
            def config(jobs):
                return {"login": "demo", "app_password": "secret", "jobs": jobs}
            first = {"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)}
            second = {**first, "destination": str(root / "photos2")}
            with self.assertRaisesRegex(ValueError, "нескольких заданиях"):
                download.validate(config([first.copy(), second]), root)
            with self.assertRaisesRegex(ValueError, "retry_from"):
                download.validate(config([{**first, "retry_from": False}]), root)
            with self.assertRaisesRegex(ValueError, "отдельно"):
                download.validate(config([{**first, "destination": str(root)}]), root)

    def test_incomplete_original_listing_is_not_claimed_complete_after_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "retry.json"
            self.queue(queue, names=(), manifest_complete=False)
            result = download.run_job({"source": "/Фото", "destination": str(root / "photos"), "retry_from": str(queue)},
                                      root / "run", {"login": "demo"}, {})
            self.assertFalse(result["manifest_complete"])
            self.assertFalse(result["verification_complete"])
            self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()

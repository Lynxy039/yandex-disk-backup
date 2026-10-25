import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download


async def records(items=(), error=None):
    for item in items:
        yield item
    if error:
        raise error


class DownloadTests(unittest.TestCase):
    def test_rejects_overlapping_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"login": "user", "app_password": "secret", "jobs": [
                {"source": "/Фото", "destination": tmp},
                {"source": "/Видео", "destination": tmp + "/video"}]}
            with self.assertRaises(ValueError):
                download.validate(config, Path(tmp))

    def test_copy_preserves_tree_and_skips_completed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst = root / "source", root / "dest"
            (src / "Подпапка").mkdir(parents=True)
            (src / "Пустая").mkdir()
            (src / "Подпапка/фото 1.jpg").write_bytes(b"photo")
            dst.mkdir()
            (dst / "лишний.txt").write_text("не удалять")
            with patch.object(download, "remote_source", return_value=str(src)):
                first = download.run_job({"source": "/Фото", "destination": str(dst)},
                                         root / "run1", {}, os.environ.copy())
                second = download.run_job({"source": "/Фото", "destination": str(dst)},
                                          root / "run2", {}, os.environ.copy())
            self.assertEqual(first["status"], "ok")
            self.assertEqual((dst / "Подпапка/фото 1.jpg").read_bytes(), b"photo")
            self.assertTrue((dst / "Пустая").is_dir())
            self.assertTrue((dst / "лишний.txt").exists())
            self.assertEqual(len((root / "run1/downloaded.jsonl").read_text().splitlines()), 1)
            self.assertEqual((root / "run2/downloaded.jsonl").read_text(), "")
            self.assertEqual(second["present"], 1)
            self.assertTrue(second["verification_complete"])

    def test_listing_deadline_is_recorded_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(download, "list_source", return_value=records(error=RuntimeError("Истёк общий лимит"))):
                result = download.run_job({"source": "/Фото", "destination": str(root / "dest")},
                                          root / "run", {}, {})
            self.assertEqual(result["status"], "failed")
            self.assertIn("Истёк общий лимит", result["error"])
            self.assertFalse(result["manifest_complete"])

    def test_failed_listing_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(download, "remote_source", return_value=str(root / "missing")):
                result = download.run_job({"source": "/Нет", "destination": str(root / "dest")},
                                          root / "run", {}, os.environ.copy())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["manifest_complete"])
            self.assertFalse(result["verification_complete"])

    def test_writes_failed_file_list_with_reason_and_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = [{"Path": "Папка/фото.jpg", "Size": 10, "IsDir": False}]
            with patch.object(download, "list_source", return_value=records(listing)), \
                 patch("transfers.attempt", return_value="Нет данных 8 с"):
                result = download.run_job({"source": "/Фото", "destination": str(root / "dest")},
                                          root / "run", {"retry_passes": 1}, {})
            self.assertEqual(result["status"], "failed")
            self.assertIn("Нет данных 8 с\tПапка/фото.jpg", (root / "run/failed.txt").read_text())
            failed = json.loads((root / "run/failed.jsonl").read_text())
            self.assertEqual(failed["attempts"], 2)
            self.assertFalse(result["verification_complete"])

    def test_interrupt_saves_pending_file_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = [{"Path": "фото.jpg", "Size": 10, "IsDir": False}]
            async def interrupted(entries, *args, discover=None):
                async for item in discover:
                    entries.append(item)
                    raise KeyboardInterrupt
            with patch.object(download, "list_source", return_value=records(listing)), \
                 patch("transfers.run_files", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    download.run_job({"source": "/Фото", "destination": str(root / "dest")},
                                     root / "run", {}, {})
            result = json.loads((root / "run/summary.json").read_text())
            self.assertEqual(result["status"], "interrupted")
            self.assertFalse(result["verification_complete"])
            pending = json.loads((root / "run/failed.jsonl").read_text())
            self.assertEqual(pending["status"], "interrupted")

    def test_restart_downloads_cached_paths_even_when_new_discovery_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {"source": "/Фото", "destination": str(root / "backup")}
            item = {"Path": "known.jpg", "Size": 3, "IsDir": False}
            with patch.object(download, "list_source", return_value=records([item], RuntimeError("Обрыв списка"))), \
                 patch("transfers.attempt", return_value="503"):
                first = download.run_job(job, root / "run1", {"retry_passes": 0}, {})
            self.assertFalse(first["manifest_complete"])
            self.assertEqual(first["failed"], 1)
            async def attempt(source, temporary, item, config, env, active):
                temporary.write_bytes(b"abc")
                return None
            with patch.object(download, "list_source", return_value=records(error=RuntimeError("Сервер не отвечает"))), \
                 patch("transfers.attempt", side_effect=attempt):
                second = download.run_job(job, root / "run2", {}, {})
            self.assertEqual(second["cached_files"], 1)
            self.assertEqual(second["downloaded"], 1)
            self.assertTrue(second["known_files_verified"])
            self.assertFalse(second["manifest_complete"])
            self.assertEqual(second["status"], "failed")
            self.assertEqual((root / "backup/known.jpg").read_bytes(), b"abc")
            self.assertEqual(json.loads((root / "run1/manifest.jsonl").read_text())["path"], "known.jpg")

    def test_cached_paths_are_isolated_between_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {"source": "/Фото", "destination": str(root / "backup")}
            item = {"Path": "private.jpg", "Size": 3, "IsDir": False}
            with patch.object(download, "list_source", return_value=records([item])), \
                 patch("transfers.attempt", return_value="503"):
                download.run_job(job, root / "run1", {"login": "first", "retry_passes": 0}, {})
            with patch.object(download, "list_source", return_value=records(error=RuntimeError("Нет доступа"))):
                result = download.run_job(job, root / "run2", {"login": "second"}, {})
            self.assertEqual(result["cached_files"], 0)
            self.assertEqual(result["files"], 0)

    def test_main_continues_after_failed_job_without_logging_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"login": "user", "app_password": "TOP_SECRET", "jobs": [
                {"source": "/Нет", "destination": str(root / "one")},
                {"source": "/Фото", "destination": str(root / "two")}]}
            settings = root / "settings.json"
            settings.write_text(json.dumps(config))
            settings.chmod(0o600)
            with patch.object(sys, "argv", ["download.py", str(settings)]), \
                 patch.object(download, "list_source", side_effect=[records(error=RuntimeError("Нет доступа")), records()]):
                code = download.main()
            self.assertEqual(code, 1)
            run = next(path for path in (root / "logs").iterdir() if (path / "summary.json").exists())
            summary = json.loads((run / "summary.json").read_text())
            self.assertEqual([job["status"] for job in summary["jobs"]], ["failed", "ok"])
            self.assertFalse((root / "logs/.download.lock").exists())
            for report in run.rglob("*"):
                if report.is_file():
                    self.assertNotIn("TOP_SECRET", report.read_text())

    def test_emits_listing_copy_check_and_final_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = []
            with patch.object(download, "list_source", return_value=records()):
                result = download.run_job({"source": "/Фото", "destination": str(root / "dest")},
                                          root / "run", {}, {}, observer=events.append)
            self.assertEqual([event["type"] for event in events],
                             ["job_started", "phase", "phase", "job_finished"])
            self.assertEqual([event["phase"] for event in events if event["type"] == "phase"], ["copy", "check"])
            self.assertEqual(events[-1]["result"], result)


if __name__ == "__main__":
    unittest.main()

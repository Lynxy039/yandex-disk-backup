"""REST-режим: настройка native-бэкенда rclone без пользовательского токена."""
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import download
import passwords
from test_download import records


class RestTests(unittest.TestCase):
    def test_validates_token_instead_of_app_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"backend": "rest", "login": "user", "oauth_token": "test-polygon-token",
                      "jobs": [{"source": "/Фото", "destination": str(Path(tmp) / "backup")}]}
            download.validate(config, Path(tmp))
            config["oauth_token"] = "token\nwith-space"
            with self.assertRaises(ValueError):
                download.validate(config, Path(tmp))
            config["backend"] = "unsupported"
            with self.assertRaises(ValueError):
                download.validate(config, Path(tmp))

    def test_remote_uses_native_yandex_backend(self):
        self.assertEqual(download.remote_source("/Фото", "rest"), ":yandex:/Фото/")
        self.assertEqual(download.remote_source("/", "rest"), ":yandex:/")
        self.assertEqual(download.remote_source("/Фото"), ":webdav:/Фото/")

    def test_token_is_only_in_environment_with_dummy_refresh_and_future_expiry(self):
        config = {"backend": "rest", "login": "user", "oauth_token": "test-polygon-token"}
        with patch.dict(os.environ, {"RCLONE_WEBDAV_PASS": "old-pass", "RCLONE_CONFIG": "old.conf"}), \
             patch("download.subprocess.run") as subprocess_run:
            env = download.rclone_environment(config)
        subprocess_run.assert_not_called()
        self.assertNotIn("RCLONE_WEBDAV_PASS", env)
        self.assertNotIn("RCLONE_CONFIG", env)
        self.assertEqual(json.loads(env["RCLONE_YANDEX_TOKEN"]), {
            "access_token": "test-polygon-token", "token_type": "OAuth",
            "refresh_token": "unused", "expiry": "2099-01-01T00:00:00Z"})

    def test_rest_does_not_reuse_unverified_webdav_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {"source": "/Фото", "destination": str(root / "backup")}
            listing = [{"Path": "cached.jpg", "Size": 3, "IsDir": False}]
            with patch.object(download, "list_source", return_value=records(listing)), \
                 patch("transfers.attempt", return_value="503"):
                download.run_job(job, root / "webdav", {"login": "user", "retry_passes": 0}, {})
            with patch.object(download, "list_source", return_value=records(error=RuntimeError("Нет списка"))), \
                 patch("transfers.attempt", return_value="503") as attempt:
                result = download.run_job(job, root / "rest", {"backend": "rest", "login": "user"}, {})
            attempt.assert_not_called()
            self.assertEqual(result["cached_files"], 0)
            self.assertEqual(result["backend"], "rest")

    def test_cli_receives_encrypted_rest_token_from_tui_without_obscure_or_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "settings.json"
            config = {"backend": "rest", "login": "user", "oauth_token_encrypted":
                      passwords.encrypt_password("test-polygon-token", "несколько случайных слов"),
                      "jobs": [{"source": "/Фото", "destination": str(root / "backup")}]}
            passwords.save_config(path, config)
            original = path.read_bytes()
            with patch.object(sys, "argv", ["download.py", str(path), "--password-stdin"]), \
                 patch.object(sys, "stdin", io.StringIO(json.dumps("test-polygon-token") + "\n")), \
                 patch.object(download, "list_source", return_value=records()), \
                 patch("download.subprocess.run") as subprocess_run:
                self.assertEqual(download.main(), 0)
            subprocess_run.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            for report in (root / "logs").rglob("*.json*"):
                self.assertNotIn("test-polygon-token", report.read_text())

    def test_master_password_protects_rest_token_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            config = {"backend": "rest", "login": "user", "oauth_token": "test-polygon-token"}
            with patch("passwords.prompt_master", side_effect=["несколько случайных слов"] * 2):
                passwords.configure_encryption(path, config, True)
            encrypted = json.loads(path.read_text())
            self.assertIn("oauth_token_encrypted", encrypted)
            self.assertNotIn("oauth_token", encrypted)
            self.assertNotIn("app_password", encrypted)
            self.assertNotIn("test-polygon-token", path.read_text())
            with patch("passwords.prompt_master", return_value="несколько случайных слов"):
                self.assertEqual(passwords.read_password(encrypted), "test-polygon-token")
                passwords.configure_encryption(path, encrypted, False)
            self.assertEqual(json.loads(path.read_text()), config)

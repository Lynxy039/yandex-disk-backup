import base64
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import passwords


class PasswordTests(unittest.TestCase):
    def test_round_trip_does_not_store_either_password(self):
        envelope = passwords.encrypt_password("пароль-Яндекса", "случайные слова для защиты")
        self.assertNotIn("пароль-Яндекса", json.dumps(envelope, ensure_ascii=False))
        self.assertNotIn("случайные слова для защиты", json.dumps(envelope, ensure_ascii=False))
        self.assertEqual(passwords.decrypt_password(envelope, "случайные слова для защиты"), "пароль-Яндекса")

    def test_same_password_uses_random_salt_and_nonce(self):
        first = passwords.encrypt_password("secret", "случайные слова для защиты")
        second = passwords.encrypt_password("secret", "случайные слова для защиты")
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotEqual(first["ciphertext"], second["ciphertext"])

    def test_wrong_master_or_modified_ciphertext_is_rejected(self):
        envelope = passwords.encrypt_password("secret", "случайные слова для защиты")
        with self.assertRaisesRegex(ValueError, "Неверный мастер-пароль"):
            passwords.decrypt_password(envelope, "ошибочный пароль")
        changed = copy.deepcopy(envelope)
        data = bytearray(base64.b64decode(changed["ciphertext"]))
        data[0] ^= 1
        changed["ciphertext"] = base64.b64encode(data).decode()
        with self.assertRaisesRegex(ValueError, "Неверный мастер-пароль"):
            passwords.decrypt_password(changed, "случайные слова для защиты")

    def test_short_master_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "12"):
            passwords.encrypt_password("secret", "АЗБУКА")

    def test_cli_enables_and_disables_encryption_with_private_atomic_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            config = {"login": "user", "app_password": "app-secret", "jobs": []}
            path.write_text(json.dumps(config))
            with patch("passwords.prompt_master", side_effect=["случайные слова для защиты"] * 2):
                passwords.configure_encryption(path, config, True)
            encrypted = json.loads(path.read_text())
            self.assertNotIn("app_password", encrypted)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            with patch("passwords.prompt_master", return_value="ошибочный пароль"):
                with self.assertRaises(ValueError):
                    passwords.configure_encryption(path, encrypted, False)
            self.assertEqual(path.read_bytes(), original)
            with patch("passwords.prompt_master", return_value="случайные слова для защиты"):
                passwords.configure_encryption(path, encrypted, False)
            self.assertEqual(json.loads(path.read_text()), config)
            self.assertFalse(list(Path(tmp).glob("*.tmp")))

    def test_failed_atomic_save_preserves_original_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"app_password":"original"}')
            original = path.read_bytes()
            with patch("passwords.Path.replace", side_effect=OSError("Ошибка записи")):
                with self.assertRaises(OSError):
                    passwords.save_config(path, {"app_password": "replacement"})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_confirmation_mismatch_does_not_overwrite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            config = {"app_password": "secret"}
            path.write_text(json.dumps(config))
            original = path.read_bytes()
            with patch("passwords.prompt_master", side_effect=["случайные слова для защиты", "другая фраза"]):
                with self.assertRaisesRegex(ValueError, "не совпадают"):
                    passwords.configure_encryption(path, config, True)
            self.assertEqual(path.read_bytes(), original)

    def test_pipe_accepts_only_json_password_and_no_terminal_fallback(self):
        config = {"app_password_encrypted": {"version": 1}}
        with patch("passwords.sys.stdin", io.StringIO(json.dumps("app-secret") + "\n")):
            self.assertEqual(passwords.read_password(config, from_stdin=True), "app-secret")
        for line in ("", "not json", "{}", '""'):
            with patch("passwords.sys.stdin", io.StringIO(line)), self.assertRaises(ValueError):
                passwords.read_password(config, from_stdin=True)
        with patch("passwords.sys.stdin", io.StringIO()), self.assertRaisesRegex(ValueError, "интерактивный"):
            passwords.read_password(config)

    def test_invalid_envelope_is_rejected_before_key_derivation(self):
        for envelope in ({}, {"version": 99}, {"version": 1, "salt": "!"}, None):
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                passwords.decrypt_password(envelope, "случайные слова для защиты")

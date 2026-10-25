"""Опциональное AES-GCM-шифрование; мастер-пароль и ключ существуют только в памяти."""
import base64
import binascii
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

AAD = b"yandex-disk-backup/app-password/v1"


def _crypto():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise ValueError("Для шифрования установите зависимости: python3 -m pip install -r requirements.txt") from None
    return AESGCM, Scrypt, InvalidTag


def _key(master, salt, scrypt):
    # Параметры фиксированы версией формата: файл настроек не может навязать огромные затраты памяти.
    return scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(master.encode("utf-8"))


def encrypt_password(password, master):
    if not isinstance(master, str) or len(master) < 12:
        raise ValueError("Мастер-пароль: не менее 12 символов; лучше несколько случайных слов")
    if not isinstance(password, str) or not password.strip() or len(password.encode("utf-8")) > 16368:
        raise ValueError("Некорректный пароль приложения")
    aes, scrypt, _ = _crypto()
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = aes(_key(master, salt, scrypt)).encrypt(nonce, password.encode("utf-8"), AAD)
    return {"version": 1, **{key: base64.b64encode(value).decode("ascii")
            for key, value in (("salt", salt), ("nonce", nonce), ("ciphertext", ciphertext))}}


def decrypt_password(envelope, master):
    try:
        if not isinstance(envelope, dict) or type(envelope.get("version")) is not int or envelope["version"] != 1:
            raise ValueError
        values = []
        for key in ("salt", "nonce", "ciphertext"):
            value = envelope[key]
            if not isinstance(value, str) or len(value) > 32768:
                raise ValueError
            values.append(base64.b64decode(value, validate=True))
        salt, nonce, ciphertext = values
        if len(salt) != 16 or len(nonce) != 12 or not 16 <= len(ciphertext) <= 16384:
            raise ValueError
    except (KeyError, ValueError, TypeError, binascii.Error):
        raise ValueError("Повреждён или не поддерживается формат зашифрованного пароля") from None
    aes, scrypt, invalid_tag = _crypto()
    try:
        return aes(_key(master, salt, scrypt)).decrypt(nonce, ciphertext, AAD).decode("utf-8")
    except (invalid_tag, UnicodeDecodeError):
        raise ValueError("Неверный мастер-пароль или повреждён зашифрованный пароль") from None


def prompt_master(label="Мастер-пароль: "):
    if not sys.stdin.isatty():
        raise ValueError("Для мастер-пароля нужен интерактивный терминал. Запустите CLI вручную или используйте TUI")
    return getpass.getpass(label)


def read_password(config, from_stdin=False):
    if "app_password_encrypted" in config:
        if from_stdin:
            # TUI передаёт только пароль приложения по отдельному pipe, не мастер-пароль и не аргументы.
            try:
                line = sys.stdin.readline(65536)
                if len(line) >= 65536:
                    raise ValueError
                password = json.loads(line)
            except ValueError:
                raise ValueError("Не получен пароль приложения от TUI") from None
        else:
            password = decrypt_password(config["app_password_encrypted"], prompt_master())
    else:
        if from_stdin:
            raise ValueError("Передача пароля из TUI ожидает зашифрованные настройки")
        password = config.get("app_password", "")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("Заполните пароль приложения в настройках")
    return password


def save_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_encryption(path, config, enabled):
    password = read_password(config)
    updated = dict(config)
    updated.pop("app_password", None)
    updated.pop("app_password_encrypted", None)
    if enabled:
        master = prompt_master("Новый мастер-пароль (не менее 12 символов): ")
        if master != prompt_master("Повторите новый мастер-пароль: "):
            raise ValueError("Мастер-пароли не совпадают; настройки не изменены")
        updated["app_password_encrypted"] = encrypt_password(password, master)
    else:
        updated["app_password"] = password
    save_config(path, updated)

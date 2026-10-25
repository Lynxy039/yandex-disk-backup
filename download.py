#!/usr/bin/env python3
"""Выгрузка выбранных папок Яндекс Диска через rclone/WebDAV."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys

import transfers
import passwords
from discovery import Inventory, stream_source as list_source


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate(config, base):
    for key in ("login", "app_password"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"Заполните {key} в настройках")
    jobs = config.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("jobs должен содержать хотя бы одну папку")
    destinations = []
    for job in jobs:
        source = job.get("source")
        if not isinstance(source, str) or not source.startswith("/") or ".." in PurePosixPath(source).parts:
            raise ValueError("source должен быть абсолютным путём на Диске без '..'")
        destination = job.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("Укажите destination для каждой папки")
        path = (base / Path(destination).expanduser()).resolve()
        if path == Path(path.anchor):
            raise ValueError("Нельзя скачивать в корень файловой системы")
        for previous in destinations:
            if path == previous or path in previous.parents or previous in path.parents:
                raise ValueError("Папки назначения не должны совпадать или быть вложены друг в друга")
        destinations.append(path)
        job["destination"] = str(path)
    for key, default in transfers.DEFAULTS.items():
        value = config.get(key, default)
        minimum = 0 if key == "retry_passes" else 1
        if type(value) is not int or value < minimum:
            raise ValueError(f"{key} должен быть целым числом не меньше {minimum}")
    logs = (base / Path(config.get("logs_dir", "logs")).expanduser()).resolve()
    for destination in destinations:
        if logs == destination or destination in logs.parents or logs in destination.parents:
            raise ValueError("logs_dir должен находиться отдельно от папок назначения")
    return logs


def remote_source(source):
    return ":webdav:" + source.rstrip("/") + "/"


def write_reports(entries, root, folder, interrupted):
    reports = {key: [] for key in ("present", "missing", "different", "errors", "failed")}
    for item in entries:
        try:
            path = transfers.safe_path(root, item["path"])
            if not path.is_file():
                category = "missing"
            elif path.stat().st_size != item["size"]:
                category = "different"
            else:
                category = "present"
        except (OSError, ValueError) as error:
            category = "errors"
            item["reason"] = str(error)
        reports[category].append(item["path"])
        if category != "present" or item["status"] not in ("downloaded", "present"):
            item["reason"] = item.get("reason") or ("Прервано / не успели скачать" if interrupted else category)
            item["status"] = "interrupted" if interrupted else "failed"
            reports["failed"].append(item)
    for category in ("present", "missing", "different", "errors"):
        (folder / f"{category}.txt").write_text("".join(path + "\n" for path in reports[category]), encoding="utf-8")
    (folder / "failed.txt").write_text("".join(f"{item['reason']}\t{item['path']}\n" for item in reports["failed"]), encoding="utf-8")
    for filename, items in (("results.jsonl", entries), ("failed.jsonl", reports["failed"])):
        with (folder / filename).open("w", encoding="utf-8") as stream:
            for item in items:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    save_json(folder / "check.json", {"mode": "local_sizes_against_manifest", "files": len(entries),
                                     "present": len(reports["present"]), "failed": len(reports["failed"])})
    (folder / "check.jsonl").write_text(json.dumps({"stats": {"checks": len(entries), "totalChecks": len(entries),
        "errors": len(reports["failed"]), "listed": len(entries)}}, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(reports["failed"])


def run_job(job, folder, config, env, observer=None):
    folder.mkdir(parents=True, exist_ok=False)
    root = Path(job["destination"])
    result = {"source": job["source"], "destination": str(root), "status": "running",
              "manifest_complete": False, "verification_complete": False}
    entries, directories = [], set()
    inventory = None
    save_json(folder / "summary.json", result)
    log = (folder / "copy.jsonl").open("a", encoding="utf-8", buffering=1)
    downloaded = (folder / "downloaded.jsonl").open("a", encoding="utf-8", buffering=1)
    manifest_log = (folder / "manifest.jsonl").open("a", encoding="utf-8", buffering=1)

    def emit(event):
        if event.get("discovery") == "complete":
            result["manifest_complete"] = True
        elif event.get("discovery") == "failed":
            result["error"] = event["msg"]
        state = event.get("state")
        event = dict(event, time=datetime.now().astimezone().isoformat())
        event["level"] = "warning" if state == "deferred" else ("error" if state == "failed" else event.get("level", "info"))
        if state:
            event["object"] = event["path"]
            event["msg"] = {"active": "Начало попытки", "downloaded": "Скачан", "present": "Уже есть",
                            "deferred": "Отложен", "failed": "Не удалось"}[state]
            if event.get("reason"):
                event["msg"] += ": " + event["reason"]
        elif "pass_started" in event:
            event["msg"] = f"Проход {event['pass_started']}: файлов в очереди {event['remaining']}"
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
        if state == "downloaded":
            downloaded.write(json.dumps({key: event[key] for key in ("time", "path", "size", "attempt")}, ensure_ascii=False) + "\n")
        if not observer and "stats" not in event:
            print(f"{event.get('object', '')}: {event.get('msg', '')}", flush=True)

    def phase(name, filename):
        if observer:
            observer({"type": "phase", "phase": name, "log": str(folder / filename)})

    if observer:
        observer({"type": "job_started", "source": job["source"], "destination": str(root), "folder": str(folder)})
    else:
        print(f"\n{job['source']} → {root}\nОтчёты: {folder}", flush=True)
    try:
        root.mkdir(parents=True, exist_ok=True)
        source = remote_source(job["source"])
        phase("copy", "copy.jsonl")
        identity = json.dumps([config.get("login", ""), job["source"], str(root.resolve())], ensure_ascii=False)
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        cache_dir = Path(config.get("_cache_dir", folder.parent / ".inventory"))
        inventory = Inventory(cache_dir / f"{key}.sqlite3")

        def record(item, origin):
            path, size, is_dir = item["Path"], item["Size"], item["IsDir"]
            if not isinstance(path, str) or type(is_dir) is not bool or type(size) is not int or (not is_dir and size < 0):
                raise ValueError("Некорректный путь, тип или размер в списке")
            destination = transfers.safe_path(root, path)
            if origin == "remote":
                inventory.remember(item)
            manifest_log.write(json.dumps({"path": path, "size": size, "is_dir": is_dir, "origin": origin}, ensure_ascii=False) + "\n")
            if is_dir:
                directories.add(path)
                destination.mkdir(parents=True, exist_ok=True)
                return None
            return {"path": path, "size": size, "status": "pending", "attempts": 0}

        for item in inventory.cached():
            entry = record(item, "cache")
            if entry is not None:
                entries.append(entry)
        result["cached_files"] = len(entries)
        emit({"msg": f"Из сохранённого индекса загружено файлов: {len(entries)}"})

        async def discover():
            stream = list_source(source, folder, config, env)
            try:
                async for item in stream:
                    entry = record(item, "remote")
                    if entry is not None:
                        yield entry
                    else:
                        await asyncio.sleep(0)
                inventory.finish()
            finally:
                await stream.aclose()

        outcome = asyncio.run(transfers.run_files(entries, source, root, config, env, emit, discover=discover()))
        result["manifest_complete"] = outcome["discovery_complete"]
        result["status"] = "ok" if result["manifest_complete"] else "failed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        raise
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as error:
        result["status"] = "failed"
        result["error"] = str(error)
        emit({"msg": str(error), "level": "error"})
    finally:
        phase("check", "check.jsonl")
        failures = write_reports(entries, root, folder, result["status"] == "interrupted")
        result["files"] = len(entries)
        save_json(folder / "manifest.json", {"files": entries, "directories": sorted(directories),
                                             "complete": result["manifest_complete"]})
        result["failed"] = failures
        result["downloaded"] = sum(item["status"] == "downloaded" for item in entries)
        result["present"] = sum(item["status"] == "present" for item in entries)
        result["known_files_verified"] = failures == 0 and result["status"] != "interrupted"
        result["verification_complete"] = result["manifest_complete"] and result["known_files_verified"] and result["status"] == "ok"
        if result["status"] == "ok" and failures:
            result["status"] = "failed"
        log.close()
        downloaded.close()
        manifest_log.close()
        if inventory is not None:
            inventory.close()
        save_json(folder / "summary.json", result)
        if observer:
            observer({"type": "job_finished", "result": result, "folder": str(folder)})
        else:
            print(f"Результат: {result['status']}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default=str(Path(__file__).with_name("settings.json")))
    parser.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    encryption = parser.add_mutually_exclusive_group()
    encryption.add_argument("--encrypt-password", action="store_true", help="Зашифровать пароль приложения мастер-паролем без запуска выгрузки")
    encryption.add_argument("--decrypt-password", action="store_true", help="Отключить шифрование и сохранить пароль открытым текстом без запуска выгрузки")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    observer = (lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)) if args.events else None
    os.umask(0o077)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("Ожидался JSON-объект настроек")
        if os.name != "nt" and config_path.stat().st_mode & 0o077:
            raise ValueError(f"Закройте доступ к паролю: chmod 600 '{config_path}'")
        if args.encrypt_password or args.decrypt_password:
            passwords.configure_encryption(config_path, config, enabled=args.encrypt_password)
            print("Шифрование включено" if args.encrypt_password else "Шифрование отключено: пароль хранится открытым текстом")
            return 0
        if "app_password_encrypted" in config or args.password_stdin:
            config["app_password"] = passwords.read_password(config, from_stdin=args.password_stdin)
        logs = validate(config, config_path.parent)
        if not shutil.which("rclone"):
            raise ValueError("Установите rclone: brew install rclone (macOS) или sudo apt install rclone (Ubuntu)")
        # Пароль не передаётся аргументом процесса и не сохраняется в rclone.conf.
        obscured = subprocess.run(["rclone", "obscure", "-"], input=config["app_password"],
                                  text=True, capture_output=True, check=True).stdout.strip()
        env = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
        env.update(RCLONE_WEBDAV_URL="https://webdav.yandex.ru/", RCLONE_WEBDAV_VENDOR="other",
                   RCLONE_WEBDAV_USER=config["login"], RCLONE_WEBDAV_PASS=obscured)
        logs.mkdir(parents=True, exist_ok=True)
        config["_cache_dir"] = str(logs / ".inventory")
        lock = logs / ".download.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError(f"Уже запущена выгрузка (или осталась блокировка после сбоя): {lock}")
        os.close(descriptor)
        try:
            run = logs / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            run.mkdir()
            if observer:
                observer({"type": "run_started", "folder": str(run), "jobs": len(config["jobs"])})
            results = []
            status = "running"
            try:
                for index, job in enumerate(config["jobs"], 1):
                    if observer:
                        results.append(run_job(job, run / f"{index:02d}", config, env, observer=observer))
                    else:
                        results.append(run_job(job, run / f"{index:02d}", config, env))
                status = "ok" if all(item["status"] == "ok" for item in results) else "failed"
            except KeyboardInterrupt:
                status = "interrupted"
            finally:
                save_json(run / "summary.json", {"status": status, "jobs": results,
                          "configured_jobs": len(config["jobs"]),
                          "note": "При interrupted см. также summary.json текущей папки; остальные могли не запускаться"})
                if observer:
                    observer({"type": "run_finished", "status": status, "folder": str(run)})
                else:
                    print(f"\nИтог: {status}. Все логи: {run}")
            return 0 if status == "ok" else (130 if status == "interrupted" else 1)
        finally:
            lock.unlink()
    except KeyboardInterrupt:
        return 130
    except (EOFError, OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

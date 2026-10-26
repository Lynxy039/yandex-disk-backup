"""Переносимая очередь недокачанных файлов, независимая от готовых копий."""
import json
from pathlib import Path, PurePosixPath

import transfers

FORMAT = "yandex-disk-backup-retry"


def load(path, job, login):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / ("retry.json" if (path / "retry.json").exists() else "failed.jsonl")
    if path.name == "failed.jsonl" and (path.parent / "retry.json").exists():
        path = path.parent / "retry.json"
    if path.name == "failed.jsonl":
        summary = json.loads((path.parent / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            raise ValueError("Некорректный summary.json старого отчёта")
        data = {"format": FORMAT, "version": 1, "source": summary.get("source"),
                "login": summary.get("login"), "manifest_complete": summary.get("manifest_complete", False),
                "files": [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]}
        checkpoint = path.parent / "retry.json"
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        checkpoint = path
    if not isinstance(data, dict) or data.get("format") != FORMAT or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Неизвестный формат/версия очереди докачки")
    source = data.get("source")
    if not isinstance(source, str) or PurePosixPath(source) != PurePosixPath(job["source"]):
        raise ValueError("Папка на Диске не совпадает с источником очереди докачки")
    account = data.get("login")
    if account is not None and (not isinstance(account, str) or account.strip().casefold() != login.strip().casefold()):
        raise ValueError("Логин не совпадает с аккаунтом очереди докачки")
    if type(data.get("manifest_complete")) is not bool or not isinstance(data.get("files"), list):
        raise ValueError("Некорректный список или полнота исходного обхода в очереди")
    checkpoint = checkpoint.resolve()
    root = Path(job["destination"]).expanduser().resolve()
    if checkpoint == root or root in checkpoint.parents:
        raise ValueError("Храните очередь докачки отдельно от папки фотографий")
    files, seen = [], set()
    for item in data["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or type(item.get("size")) is not int or item["size"] < 0:
            raise ValueError("Некорректный путь или размер файла в очереди")
        transfers.safe_path(root, item["path"])
        key = str(PurePosixPath(item["path"]))
        if key in seen:
            raise ValueError("Повторяющийся путь в очереди докачки")
        seen.add(key)
        files.append({"path": item["path"], "size": item["size"]})
    return {**data, "files": files}, checkpoint

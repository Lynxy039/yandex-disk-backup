"""Потоковый список rclone и сохраняемый индекс уже найденных путей."""
import asyncio
from collections import deque
import json
from pathlib import PurePosixPath
import sqlite3
import time
import uuid

import transfers


class Inventory:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS entries (path TEXT PRIMARY KEY, size INTEGER, is_dir INTEGER, seen TEXT)")
        self.generation = uuid.uuid4().hex

    def cached(self):
        for path, size, is_dir in self.db.execute("SELECT path, size, is_dir FROM entries ORDER BY path").fetchall():
            yield {"Path": path, "Size": size, "IsDir": bool(is_dir)}

    def remember(self, item):
        self.db.execute("INSERT INTO entries VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                        "size=excluded.size, is_dir=excluded.is_dir, seen=excluded.seen",
                        (item["Path"], item["Size"], int(item["IsDir"]), self.generation))
        self.db.commit()

    def finish(self):
        # Удаляем устаревшие пути из индекса только после полного успешного обхода, не с HDD.
        self.db.execute("DELETE FROM entries WHERE seen != ?", (self.generation,))
        self.db.commit()

    def close(self):
        self.db.close()


async def stream_rest(source, folder, config):
    import httpx

    timeout = config.get("listing_timeout", transfers.DEFAULTS["listing_timeout"])
    if not source.startswith(":yandex:"):
        raise ValueError("REST-обход ожидает источник :yandex:")
    root = str(PurePosixPath("/" + source.removeprefix(":yandex:").strip("/")))
    prefix = root.rstrip("/") + "/"
    pending = deque([root])
    seen = {root}
    fields = "type,_embedded.total,_embedded.items.path,_embedded.items.type,_embedded.items.size"
    try:
        async with httpx.AsyncClient(headers={"Authorization": "OAuth " + config["oauth_token"]},
                                     timeout=httpx.Timeout(min(60, timeout), connect=min(5, timeout))) as client:
            while pending:
                directory = pending.popleft()
                offset = 0
                while True:
                    response = await asyncio.wait_for(client.get("https://cloud-api.yandex.net/v1/disk/resources",
                        params={"path": "disk:" + directory, "limit": 1000, "offset": offset, "fields": fields}), timeout=timeout)
                    if response.status_code != 200:
                        hint = "; проверьте токен и права доступа" if response.status_code in (401, 403) else ""
                        raise RuntimeError(f"REST API: HTTP {response.status_code}{hint}")
                    page = response.json()
                    embedded = page["_embedded"]
                    items, total = embedded["items"], embedded["total"]
                    if page.get("type") != "dir" or not isinstance(items, list) or type(total) is not int or total < 0:
                        raise ValueError("Некорректная страница списка")
                    if (not items and offset < total) or offset + len(items) > total:
                        raise RuntimeError("REST API: неполная или изменившаяся страница списка")
                    for item in items:
                        raw_path, kind = item["path"], item["type"]
                        if not isinstance(raw_path, str) or not raw_path.startswith("disk:") or kind not in ("dir", "file"):
                            raise ValueError("Некорректный элемент REST-списка")
                        path = raw_path[5:].rstrip("/")
                        if not path.startswith(prefix) or ".." in PurePosixPath(path).parts or str(PurePosixPath(path).parent) != directory:
                            raise ValueError("Путь REST-списка вне текущей папки")
                        if kind == "dir" and path not in seen:
                            pending.append(path)
                            seen.add(path)
                        yield {"Path": path[len(prefix):], "Size": 0 if kind == "dir" else item["size"], "IsDir": kind == "dir"}
                    offset += len(items)
                    if offset >= total:
                        break
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            message = str(error)
        elif isinstance(error, (httpx.HTTPError, asyncio.TimeoutError)):
            message = f"REST: ошибка сети/таймаут страницы ({type(error).__name__})"
        else:
            message = "REST: некорректная страница списка; полный обход не подтверждён"
        with (folder / "listing.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"level": "error", "msg": message}, ensure_ascii=False) + "\n")
        raise RuntimeError(message) from None


async def stream_source(source, folder, config, env):
    if config.get("backend", "webdav") == "rest":
        stream = stream_rest(source, folder, config)
        try:
            async for item in stream:
                yield item
        finally:
            await stream.aclose()
        return
    timeout = config.get("listing_timeout", transfers.DEFAULTS["listing_timeout"])
    listing_config = dict(config, idle_timeout=min(60, timeout))
    with (folder / "listing.jsonl").open("a", encoding="utf-8") as errors:
        process = await asyncio.create_subprocess_exec(
            "rclone", "lsjson", source, "--recursive", "--no-mimetype", "--no-modtime",
            "--disable", "ListR", "--checkers", "1", *transfers.flags(listing_config),
            stdout=asyncio.subprocess.PIPE, stderr=errors, env=env, limit=2 * 1024 * 1024)
        started, closed, has_item, comma = False, False, False, False
        last_item = time.monotonic()
        try:
            while True:
                remaining = timeout - (time.monotonic() - last_item)
                if remaining <= 0:
                    raise RuntimeError("Истёк лимит ожидания новых элементов списка")
                try:
                    raw = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise RuntimeError("Истёк лимит ожидания новых элементов списка") from None
                if not raw:
                    break
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                if closed:
                    raise ValueError("Лишние данные после конца списка rclone")
                if not started:
                    if line != "[":
                        raise ValueError("Ожидался поток JSON-массива rclone")
                    started = True
                    continue
                if line == "]":
                    if comma:
                        raise ValueError("Повреждён конец списка rclone")
                    closed = True
                    continue
                if has_item and not comma:
                    raise ValueError("Нет разделителя между элементами списка rclone")
                comma = line.endswith(",")
                item = json.loads(line[:-1] if comma else line)
                if not isinstance(item, dict):
                    raise ValueError("Некорректный элемент списка rclone")
                has_item = True
                last_item = time.monotonic()
                yield item
            try:
                code = await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                raise RuntimeError("Процесс обхода не завершился после закрытия списка") from None
            if code:
                raise RuntimeError(f"Неполный обход (код {code}); см. listing.jsonl")
            if not started or not closed:
                raise RuntimeError("Список оборвался до закрытия JSON-массива")
        finally:
            await transfers.stop_process(process)

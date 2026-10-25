"""Потоковый список rclone и сохраняемый индекс уже найденных путей."""
import asyncio
import json
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


async def stream_source(source, folder, config, env):
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

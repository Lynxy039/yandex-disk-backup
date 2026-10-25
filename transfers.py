"""Пофайловая очередь: короткие попытки, отложенные повторы, атомарная публикация."""
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import time

DEFAULTS = {"transfers": 2, "idle_timeout": 8, "file_timeout": 20,
            "retry_passes": 2, "listing_timeout": 180}


def safe_path(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or ".." in parts or "\\" in relative or "\x00" in relative:
        raise ValueError(f"Небезопасный путь в списке Диска: {relative!r}")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Символическая ссылка в назначении: {relative!r}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Путь выходит за пределы назначения: {relative!r}")
    return path


def flags(config):
    idle = config.get("idle_timeout", DEFAULTS["idle_timeout"])
    return ["--config", os.devnull, "--contimeout", f"{min(5, idle)}s", "--timeout", f"{idle}s",
            "--retries", "1", "--low-level-retries", "1", "--stats", "0",
            "--use-json-log", "--log-level", "ERROR"]


async def stop_process(process):
    if process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass


async def attempt(source, temporary, item, config, env, active):
    process = await asyncio.create_subprocess_exec(
        "rclone", "copyto", source.rstrip("/") + "/" + item["path"], str(temporary),
        *flags(config), "--multi-thread-streams", "0", "--inplace", "--ignore-times",
        "--disable", "Copy", "--local-no-preallocate", "--buffer-size", "0",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env)
    communication = asyncio.create_task(process.communicate())
    idle = config.get("idle_timeout", DEFAULTS["idle_timeout"])
    # Фото получают короткую попытку; крупным видео даём больше общего времени, но не простоя.
    deadline = max(config.get("file_timeout", DEFAULTS["file_timeout"]), idle + item["size"] / (1024 * 1024))
    started = last_data = time.monotonic()
    previous = 0
    reason = None
    try:
        while not communication.done():
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=.2)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            try:
                written = temporary.stat().st_size
            except FileNotFoundError:
                # rclone удаляет временник после неудачи: это отсутствие данных, а не локальная ошибка.
                written = previous
            if written != previous:
                last_data = now
                previous = written
            active.update(bytes=written, idle=now - last_data, elapsed=now - started)
            if not communication.done() and now - last_data >= idle:
                reason = f"Нет новых данных {idle} с"
                break
            if not communication.done() and now - started >= deadline:
                reason = f"Лимит попытки {deadline:.0f} с"
                break
        if reason:
            await stop_process(process)
        _, stderr = await communication
        if reason:
            return reason
        if process.returncode:
            messages = []
            for line in stderr.decode("utf-8", errors="replace").splitlines():
                try:
                    messages.append(json.loads(line).get("msg", line))
                except json.JSONDecodeError:
                    messages.append(line)
            return (messages[-1] if messages else f"rclone: код {process.returncode}")[:2000]
        return None
    finally:
        await stop_process(process)
        await asyncio.gather(communication, return_exceptions=True)


async def run_files(entries, source, root, config, env, emit, discover=None):
    active, locks = {}, {}
    positions = {item["path"]: index for index, item in enumerate(entries)}
    counts = {"downloaded": 0, "present": 0, "deferred": 0, "failed": 0}
    downloaded_bytes = 0
    total_bytes = sum(item["size"] for item in entries)
    current_pass, pass_total = 0, len(entries)
    processed = set()
    previous_bytes, previous_time = 0, time.monotonic()
    discovery_result = {"discovery_complete": discover is None, "discovery_error": None}
    discovering = discover is not None
    last_discovered = time.monotonic()

    def is_current(item):
        return entries[positions[item["path"]]] is item

    def change(item, status, reason=None):
        previous = item["status"]
        if is_current(item) and previous in counts:
            counts[previous] -= 1
        item["status"], item["reason"] = status, reason
        if is_current(item) and status in counts:
            counts[status] += 1
        emit({"state": status, "path": item["path"], "size": item["size"],
              "attempt": item["attempts"], "reason": reason})

    def progress():
        nonlocal previous_bytes, previous_time
        now = time.monotonic()
        volume = downloaded_bytes + sum(item["bytes"] for item in active.values())
        speed = max(0, volume - previous_bytes) / max(.001, now - previous_time)
        previous_bytes, previous_time = volume, now
        emit({"stats": {"bytes": volume, "totalBytes": total_bytes, "speed": speed,
                        "transfers": counts["downloaded"], "totalTransfers": len(entries),
                        "present": counts["present"], "deferred": counts["deferred"],
                        "errors": counts["failed"], "processed": len(processed), "passTotal": pass_total,
                        "pass": current_pass + 1, "passes": config.get("retry_passes", DEFAULTS["retry_passes"]) + 1,
                        "discovering": discovering, "discovery_idle": now - last_discovered, **discovery_result,
                        "eta": None, "listed": len(entries), "transferring": list(active.values())}})

    async def monitor():
        while True:
            progress()
            await asyncio.sleep(.5)

    async def transfer_one(item):
        nonlocal downloaded_bytes
        temporary = None
        try:
            destination = safe_path(root, item["path"])
            if destination.is_file() and destination.stat().st_size == item["size"]:
                change(item, "present")
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".yadisk-", suffix=".part", dir=destination.parent)
            os.close(descriptor)
            temporary = Path(name)
            item["attempts"] += 1
            active[item["path"]] = {"name": item["path"], "bytes": 0, "size": item["size"], "idle": 0, "elapsed": 0}
            change(item, "active")
            reason = await attempt(source, temporary, item, config, env, active[item["path"]])
            if reason is None and temporary.stat().st_size != item["size"]:
                reason = f"Размер не совпал: {temporary.stat().st_size} вместо {item['size']}"
            if reason is None:
                destination = safe_path(root, item["path"])
                os.replace(temporary, destination)
                downloaded_bytes += item["size"]
                change(item, "downloaded")
            else:
                change(item, "deferred", reason)
        except (OSError, ValueError) as error:
            change(item, "failed", f"Локальная ошибка: {error}")
        finally:
            active.pop(item["path"], None)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def worker(queue):
        while True:
            item = await queue.get()
            try:
                lock = locks.setdefault(item["path"], asyncio.Lock())
                async with lock:
                    if is_current(item):
                        await transfer_one(item)
                        if is_current(item):
                            processed.add(item["path"])
            finally:
                queue.task_done()
            await asyncio.sleep(0)

    async def produce(queue):
        nonlocal total_bytes, pass_total, discovering, last_discovered
        if discover is None:
            return
        emit({"msg": "Обход идёт параллельно со скачиванием", "discovery": "running"})
        try:
            async for item in discover:
                last_discovered = time.monotonic()
                path = item["path"]
                if path in positions:
                    old = entries[positions[path]]
                    if old["size"] == item["size"]:
                        await asyncio.sleep(0)
                        continue
                    total_bytes -= old["size"]
                    if old["status"] in counts:
                        counts[old["status"]] -= 1
                    entries[positions[path]] = item
                    processed.discard(path)
                else:
                    positions[path] = len(entries)
                    entries.append(item)
                total_bytes += item["size"]
                pass_total = len(entries)
                queue.put_nowait(item)
                await asyncio.sleep(0)
            discovery_result["discovery_complete"] = True
            emit({"msg": "Обход завершён", "discovery": "complete"})
        except Exception as error:
            discovery_result["discovery_error"] = str(error)
            emit({"msg": f"Список неполный: {error}. Уже найденные файлы остаются в очереди.",
                  "discovery": "failed", "level": "error"})
        finally:
            discovering = False
            close = getattr(discover, "aclose", None)
            if close:
                await close()

    async def run_pass(pending, producer=False):
        queue = asyncio.Queue()
        for item in pending:
            queue.put_nowait(item)
        workers = [asyncio.create_task(worker(queue)) for _ in range(config.get("transfers", 2))]
        production = asyncio.create_task(produce(queue)) if producer else None
        joining = None
        try:
            if production:
                # Ошибка рабочего потока не должна оставлять producer или queue.join висеть.
                done, _ = await asyncio.wait([production, *workers], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
                await production
            joining = asyncio.create_task(queue.join())
            done, _ = await asyncio.wait([joining, *workers], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            await joining
        finally:
            tasks = [*workers, *([production] if production else []), *([joining] if joining else [])]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    monitoring = asyncio.create_task(monitor())
    try:
        emit({"pass_started": 1, "remaining": len(entries)})
        await run_pass(list(entries), producer=True)
        for pass_index in range(1, config.get("retry_passes", DEFAULTS["retry_passes"]) + 1):
            pending = [item for item in entries if item["status"] == "deferred"]
            if not pending:
                break
            current_pass, pass_total = pass_index, len(pending)
            processed.clear()
            emit({"pass_started": current_pass + 1, "remaining": len(pending)})
            await run_pass(pending)
        for item in entries:
            if item["status"] == "deferred":
                change(item, "failed", item["reason"])
        return discovery_result
    finally:
        monitoring.cancel()
        await asyncio.gather(monitoring, return_exceptions=True)
        progress()

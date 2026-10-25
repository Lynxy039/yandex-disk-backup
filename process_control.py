"""Переносимая отмена движка через маркер и управление деревом процессов."""
import asyncio
import os
from pathlib import Path
import signal
import sys

WINDOWS = sys.platform == "win32"


class StopRequested(Exception):
    pass


def spawn_options(windows):
    # CREATE_NEW_PROCESS_GROUP из WinAPI: Ctrl+C родительского TUI не прерывает движок до подтверждения.
    return {"creationflags": 0x00000200} if windows else {"start_new_session": True}


async def run_with_stop(awaitable, stop_file=None):
    if not stop_file:
        return await awaitable

    async def watch():
        while not Path(stop_file).is_file():
            await asyncio.sleep(.1)

    work, watcher = asyncio.create_task(awaitable), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise StopRequested
        return await work
    finally:
        for task in (work, watcher):
            task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


async def kill_tree(process, windows):
    # Только аварийный путь: штатная остановка идёт через маркер/SIGINT с сохранением отчётов.
    try:
        if windows:
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            code = await killer.wait()
            if code and process.returncode is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()

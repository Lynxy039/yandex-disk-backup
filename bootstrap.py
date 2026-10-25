"""Подготовка локального venv и portable rclone без изменения глобального окружения."""
import hashlib
import io
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
from urllib.request import Request, urlopen
import zipfile

RCLONE_VERSION = '1.75.1'


def ensure_environment(root):
    environment = root / '.venv'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('Создаю локальное Python-окружение...', flush=True)
        subprocess.check_call([sys.executable, '-m', 'venv', str(environment)])
    requirements = root / 'requirements.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = environment / '.requirements.sha256'
    probe = [str(python), '-c', 'import textual, cryptography, httpx']
    ready = stamp.is_file() and stamp.read_text() == digest
    if ready:
        ready = subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not ready:
        print('Устанавливаю зависимости (только в .venv)...', flush=True)
        subprocess.check_call([str(python), '-m', 'pip', '--disable-pip-version-check', 'install', '-r', str(requirements)])
        subprocess.check_call(probe)
        stamp.write_text(digest)
    return python


def download(url):
    # CDN может отвергать стандартный User-Agent urllib.
    with urlopen(Request(url, headers={'User-Agent': 'curl/8.0'}), timeout=90) as response:
        return response.read()


def install_rclone(root, system, arch):
    filename = f'rclone-v{RCLONE_VERSION}-{system}-{arch}.zip'
    base = f'https://downloads.rclone.org/v{RCLONE_VERSION}/'
    print(f'Скачиваю portable rclone {RCLONE_VERSION} с официального сайта...', flush=True)
    manifest = download(base + 'SHA256SUMS').decode('utf-8')
    match = re.search(r'^([a-fA-F0-9]{64})\s+\*?' + re.escape(filename) + r'\s*$', manifest, re.MULTILINE)
    if not match:
        raise RuntimeError('Не найдена SHA256-сумма подходящего rclone')
    payload = download(base + filename)
    if hashlib.sha256(payload).hexdigest() != match[1].lower():
        raise RuntimeError('SHA256 архива rclone не совпадает; установка отменена')
    name = 'rclone.exe' if system == 'windows' else 'rclone'
    directory = root / '.tools' / 'rclone'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / '.download.tmp'
    try:
        # Извлекаем только один известный файл, не пути/скрипты из ZIP.
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            temporary.write_bytes(archive.read(filename[:-4] + '/' + name))
        if system != 'windows':
            temporary.chmod(0o755)
        executable = directory / name
        temporary.replace(executable)
        return executable
    finally:
        temporary.unlink(missing_ok=True)


def ensure_rclone(root):
    local = root / '.tools' / 'rclone' / ('rclone.exe' if os.name == 'nt' else 'rclone')
    for candidate in (shutil.which('rclone'), str(local) if local.is_file() else None):
        if not candidate:
            continue
        try:
            result = subprocess.run([candidate, 'version'], capture_output=True, text=True, timeout=10)
            version = re.search(r'rclone v(\d+)\.(\d+)', result.stdout)
            if result.returncode == 0 and version and tuple(map(int, version.groups())) >= (1, 75):
                return Path(candidate)
        except (OSError, subprocess.TimeoutExpired):
            pass
    system = {'win32': 'windows', 'darwin': 'osx', 'linux': 'linux'}.get(sys.platform)
    machine = platform.machine().lower()
    arch = {'amd64': 'amd64', 'x86_64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine)
    if not system or not arch:
        raise RuntimeError('Установите rclone 1.75+ вручную для вашей платформы: https://rclone.org/downloads/')
    return install_rclone(root, system, arch)


def main():
    args = sys.argv[1:]
    if args in (['--help'], ['-h']):
        print('Запуск с автоматической установкой: bootstrap.py [settings.json]\n'
              'CLI: bootstrap.py --cli [settings.json] [параметры]\n'
              'Только подготовка окружения: bootstrap.py --setup-only')
        return 0
    root = Path(__file__).resolve().parent
    try:
        python = ensure_environment(root)
        rclone = ensure_rclone(root)
        if args == ['--setup-only']:
            print('Окружение готово. Запустите лаунчер без --setup-only для открытия TUI.')
            return 0
        cli = bool(args and args[0] == '--cli')
        if cli:
            args = args[1:]
        env = dict(os.environ, PYTHONUTF8='1')
        env['PATH'] = str(rclone.parent) + os.pathsep + str(python.parent) + os.pathsep + env.get('PATH', '')
        process = subprocess.Popen([str(python), str(root / ('download.py' if cli else 'tui.py')), *args], env=env)
        # Ctrl+C обрабатывает TUI/CLI, а не ожидающий процесс установщика.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return process.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, subprocess.CalledProcessError, zipfile.BadZipFile, KeyError) as error:
        print(f'Не удалось подготовить окружение: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

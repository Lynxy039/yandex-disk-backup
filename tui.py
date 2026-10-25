#!/usr/bin/env python3
"""Терминальный интерфейс настройки и выгрузки Яндекс Диска."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, Collapsible, DataTable, Footer, Header, Input,
                             Label, ProgressBar, RichLog, Select, Static, TabbedContent, TabPane)

import download
import passwords
from process_control import WINDOWS, kill_tree, spawn_options


class LogTail:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.pending = b""

    def read(self):
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read(256 * 1024)
                self.offset = stream.tell()
        except FileNotFoundError:
            return []
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return events


def size(value):
    value = float(value or 0)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}"
        value /= 1024


class JobRow(Horizontal):
    def __init__(self, source="/", destination="", **kwargs):
        super().__init__(**kwargs)
        self.source = source
        self.destination = destination

    def compose(self) -> ComposeResult:
        yield Input(self.source, placeholder="/Фото/Отпуск", classes="source")
        yield Input(self.destination, placeholder="~/Pictures/Backup/Отпуск", classes="destination")
        yield Button("Удалить", classes="remove", variant="error")

    @on(Button.Pressed, ".remove")
    async def remove_row(self, event):
        event.stop()
        await self.remove()


class ConfirmQuit(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Static("Выгрузка ещё работает. Остановить её и выйти?")
            yield Static("Скачанное и отчёты сохранятся. Остановка может занять несколько секунд.")
            with Horizontal():
                yield Button("Продолжить работу", id="keep-working", variant="primary")
                yield Button("Остановить и выйти", id="quit-now", variant="error")

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(event.button.id == "quit-now")


def credential_description(backend):
    if backend == "rest":
        label = "OAuth-токен Полигона Яндекс Диска"
        hint = "Получить: https://yandex.ru/dev/disk/poligon/\nТокен того же аккаунта, что указан в логине. Не токен из обычного rclone config."
    else:
        label = "Пароль приложения для Яндекс Диска"
        hint = "Создать: https://id.yandex.ru/security/app-passwords\nНе основной пароль аккаунта."
    return label, hint + "\nБез шифрования сохраняется открытым текстом (600). Если секрет зашифрован, оставьте поле пустым и введите мастер-пароль."


class YandexApp(App):
    TITLE = "Яндекс Диск · резервная копия"
    SUB_TITLE = "Выбранные папки → ваш компьютер"
    BINDINGS = [("ctrl+s", "save", "Сохранить"), ("f5", "start", "Запустить"),
                ("f8", "stop", "Остановить"), ("ctrl+q", "request_quit", "Выход"),
                ("ctrl+c", "request_quit", "Выход")]
    CSS = """
    Screen { background: $surface; }
    TabPane { padding: 1 2; height: 1fr; }
    #settings-scroll, #run-scroll { height: 1fr; }
    .section { margin-top: 1; color: $accent; text-style: bold; }
    .hint { color: $text-muted; height: auto; margin-bottom: 1; }
    #password-hint { margin-top: 1; }
    Input { margin-bottom: 1; }
    JobRow { height: 4; }
    JobRow Input { width: 1fr; margin-right: 1; }
    JobRow Button { min-width: 10; width: 12; }
    #jobs { height: auto; }
    .options { height: 6; }
    .option { width: 1fr; margin-right: 1; }
    .option Label { height: 1; }
    #form-message { height: auto; min-height: 1; color: $warning; }
    #form-actions { height: 3; margin-top: 1; }
    #form-actions Button { margin-right: 1; }
    #run-status { text-style: bold; color: $accent; height: auto; margin-bottom: 1; }
    #job-table { height: 7; margin-bottom: 1; }
    #current { height: auto; max-height: 4; }
    #phase { height: 1; color: $warning; }
    ProgressBar { margin: 1 0; }
    #stats { height: auto; min-height: 2; }
    #active-files { height: auto; max-height: 5; color: $text-muted; }
    #run-log { height: 1fr; min-height: 4; border: round $primary; }
    #report-path { height: auto; max-height: 3; color: $text-muted; }
    #run-actions { height: 3; margin-top: 1; }
    #quit-dialog { width: 70; height: auto; padding: 2; background: $panel; border: thick $error; }
    #quit-dialog Static { margin-bottom: 1; height: auto; }
    #quit-dialog Horizontal { height: 3; }
    #quit-dialog Button { margin-right: 1; }
    ConfirmQuit { align: center middle; }
    .compact TabPane { padding: 0 1; }
    .compact #job-table { height: 4; margin-bottom: 0; }
    .compact #run-status { margin-bottom: 0; }
    .compact ProgressBar { margin: 0; }
    """

    def __init__(self, config_path):
        super().__init__()
        self.config_path = Path(config_path).expanduser().resolve()
        self.settings = {"backend": "webdav", "login": "", "app_password": "", "logs_dir": "logs", **download.transfers.DEFAULTS,
                         "jobs": [{"source": "/", "destination": ""}]}
        self.session_password = ""
        self.load_error = ""
        try:
            if self.config_path.exists():
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or not isinstance(loaded.get("jobs", []), list):
                    raise ValueError("Ожидался объект настроек со списком jobs")
                for job in loaded.get("jobs", []):
                    if not isinstance(job, dict) or any(not isinstance(job.get(key, ""), str)
                                                        for key in ("source", "destination")):
                        raise ValueError("Каждая папка в jobs должна содержать текстовые source и destination")
                passwords.credential_fields(loaded)
                self.settings.update(loaded)
        except (OSError, ValueError) as error:
            self.load_error = f"Не удалось загрузить настройки: {error}"
        self.running = False
        self.stopping = False
        self.quit_after_stop = False
        self.process = None
        self.control_directory = None
        self.stop_file = None
        self.tail = None
        self.phase = "copy"
        self.phase_started = time.monotonic()
        self.job_number = 0
        self.current_row = None
        self.rows = []
        self.run_folder = None
        self.final_event = False
        self.exit_code = 0
        self.incomplete_jobs = False

    def compose(self) -> ComposeResult:
        credential, encrypted_field = passwords.credential_fields(self.settings)
        label, hint = credential_description(self.settings["backend"])
        yield Header()
        with TabbedContent(initial="settings-tab", id="tabs"):
            with TabPane("1 · Настройки", id="settings-tab"):
                with VerticalScroll(id="settings-scroll"):
                    yield Static("АККАУНТ", classes="section")
                    yield Label("Способ подключения")
                    yield Select([("WebDAV · пароль приложения", "webdav"), ("REST API · токен Полигона", "rest")],
                                 value=self.settings["backend"], allow_blank=False, id="backend")
                    yield Label("Логин Яндекса")
                    yield Input(str(self.settings["login"]), id="login", placeholder="login@yandex.ru")
                    yield Label(label, id="credential-label")
                    yield Input(str(self.settings.get(credential, "")), password=True, id="password")
                    yield Static(hint, classes="hint", id="password-hint", markup=False)
                    with Collapsible(title="Мастер-пароль (необязательно)", collapsed=encrypted_field not in self.settings):
                        yield Checkbox("Шифровать пароль/токен мастер-паролем", value=encrypted_field in self.settings,
                                       id="encrypt-password")
                        yield Label("Мастер-пароль (не сохраняется; минимум 12 символов, лучше случайные слова)")
                        yield Input(password=True, id="master-password")
                        yield Label("Повторите мастер-пароль при включении шифрования или замене пароля/токена")
                        yield Input(password=True, id="master-confirm")
                        yield Static("Чтобы отключить шифрование, снимите галочку и сохраните с текущим мастер-паролем.\n"
                                     "Забытый мастер-пароль восстановить нельзя — потребуется новый пароль приложения или токен.",
                                     classes="hint")
                    yield Static("ПАПКИ: НА ДИСКЕ → НА КОМПЬЮТЕРЕ", classes="section")
                    yield Static("Копируется содержимое папки вместе со всеми подпапками. Используйте отдельные папки назначения.",
                                 classes="hint")
                    with Vertical(id="jobs"):
                        for job in self.settings["jobs"]:
                            yield JobRow(job.get("source", "/"), job.get("destination", ""))
                    yield Button("+ Добавить папку", id="add")
                    with Collapsible(title="Таймауты, очередь и папка логов", collapsed=True):
                        yield Label("Папка журналов (относительно файла настроек или абсолютный путь)")
                        yield Input(str(self.settings["logs_dir"]), id="logs")
                        with Horizontal(classes="options"):
                            for key, title in (("transfers", "Потоки"), ("idle_timeout", "Простой, с"),
                                               ("file_timeout", "Лимит фото, с")):
                                with Vertical(classes="option"):
                                    yield Label(title)
                                    yield Input(str(self.settings[key]), type="integer", id=key)
                        with Horizontal(classes="options"):
                            for key, title in (("retry_passes", "Повторных проходов"), ("listing_timeout", "Пауза списка, с")):
                                with Vertical(classes="option"):
                                    yield Label(title)
                                    yield Input(str(self.settings[key]), type="integer", id=key)
                        yield Static("Нет новых данных 8 с → следующий файл. Повторы — только в конце очереди.\n"
                                     "Лимит фото — минимальный общий предел попытки; для больших файлов он увеличивается по размеру.",
                                     classes="hint")
                yield Static(self.load_error or f"Настройки: {self.config_path}", id="form-message", markup=False)
                with Horizontal(id="form-actions"):
                    yield Button("Сохранить", id="save")
                    yield Button("Сохранить и начать", id="start", variant="success")
            with TabPane("2 · Выгрузка", id="run-tab"):
                with VerticalScroll(id="run-scroll"):
                    yield Static("Готов к запуску", id="run-status", markup=False)
                    yield DataTable(id="job-table", cursor_type="row")
                    yield Static("Выберите папки во вкладке «Настройки»", id="current", markup=False)
                    yield Static("Ожидание", id="phase", markup=False)
                    yield ProgressBar(total=None, show_eta=False, id="progress")
                    yield Static("", id="stats", markup=False)
                    yield Static("", id="active-files", markup=False)
                    yield RichLog(id="run-log", max_lines=300, wrap=True, markup=False)
                    yield Static("Список пополняется параллельно. Прогресс — по известным файлам, включая отложенные.",
                                 id="report-path", markup=False)
                with Horizontal(id="run-actions"):
                    yield Button("Остановить", id="stop", variant="error", disabled=True)
        yield Footer()

    def on_resize(self, event):
        self.screen.set_class(event.size.height < 32, "compact")

    def on_mount(self):
        for index, title in enumerate(("№", "Папка на Диске", "Назначение", "Статус")):
            self.query_one("#job-table", DataTable).add_column(title, key=str(index))
        self.set_interval(0.25, self.poll_log)
        self.query_one("#login", Input).focus()

    @on(Select.Changed, "#backend")
    def backend_changed(self, event):
        if event.value not in ("webdav", "rest"):
            return
        credential, encrypted_field = passwords.credential_fields({"backend": event.value})
        label, hint = credential_description(event.value)
        self.query_one("#credential-label", Label).update(label)
        self.query_one("#password-hint", Static).update(hint)
        self.query_one("#password", Input).value = self.settings.get(credential, "")
        self.query_one("#encrypt-password", Checkbox).value = encrypted_field in self.settings
        self.query_one("#master-password", Input).value = ""
        self.query_one("#master-confirm", Input).value = ""

    def collect_settings(self):
        # Не оставляем неактивные учётные данные открытым текстом после смены режима.
        config = {key: value for key, value in self.settings.items()
                  if key not in ("retries", "low_level_retries", "app_password", "app_password_encrypted", "oauth_token", "oauth_token_encrypted")}
        config["backend"] = self.query_one("#backend", Select).value
        credential, encrypted_field = passwords.credential_fields(config)
        config[credential] = self.query_one("#password", Input).value
        for key, identifier in (("login", "login"), ("logs_dir", "logs")):
            config[key] = self.query_one(f"#{identifier}", Input).value
        for key in download.transfers.DEFAULTS:
            try:
                config[key] = int(self.query_one(f"#{key}", Input).value)
            except ValueError:
                raise ValueError(f"{key}: введите целое число") from None
        config["jobs"] = [{"source": row.query_one(".source", Input).value,
                           "destination": row.query_one(".destination", Input).value}
                          for row in self.query(JobRow)]
        master = self.query_one("#master-password", Input).value
        replacing = bool(config[credential])
        if not replacing and encrypted_field in self.settings:
            config[credential] = passwords.decrypt_password(self.settings[encrypted_field], master)
        download.validate(copy.deepcopy(config), self.config_path.parent)
        password = config[credential]
        if self.query_one("#encrypt-password", Checkbox).value:
            if (replacing or encrypted_field not in self.settings) and master != self.query_one("#master-confirm", Input).value:
                raise ValueError("Мастер-пароли не совпадают; настройки не изменены")
            config[encrypted_field] = passwords.encrypt_password(password, master)
            del config[credential]
        return config, password

    def save_settings(self):
        try:
            config, password = self.collect_settings()
            passwords.save_config(self.config_path, config)
            self.settings = config
            self.session_password = password
            if passwords.credential_fields(config)[1] in config:
                self.query_one("#password", Input).value = ""
                self.query_one("#master-confirm", Input).value = ""
            else:
                self.query_one("#password", Input).value = password
                self.query_one("#master-password", Input).value = ""
                self.query_one("#master-confirm", Input).value = ""
            self.query_one("#form-message", Static).update(f"Сохранено: {self.config_path}")
            return True
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.query_one("#form-message", Static).update(f"Ошибка: {error}")
            return False

    def action_save(self):
        if not self.running:
            self.save_settings()

    @on(Button.Pressed, "#save")
    def save_pressed(self):
        self.action_save()

    @on(Button.Pressed, "#add")
    async def add_folder(self):
        row = JobRow()
        await self.query_one("#jobs", Vertical).mount(row)
        row.query_one(".source", Input).focus()
        row.scroll_visible()

    @on(Button.Pressed, "#start")
    def start_pressed(self):
        self.action_start()

    def action_start(self):
        if self.running or not self.save_settings():
            return
        self.running = True
        self.stopping = False
        self.final_event = False
        self.incomplete_jobs = False
        self.job_number = 0
        self.current_row = None
        self.tail = None
        self.run_folder = None
        self.rows = []
        self.query_one("#run-log", RichLog).clear()
        table = self.query_one("#job-table", DataTable)
        table.clear()
        for index, job in enumerate(self.settings["jobs"], 1):
            self.rows.append(table.add_row(str(index), Text(job["source"]), Text(job["destination"]), Text("Ожидание")))
        self.set_form_disabled(True)
        self.query_one("#tabs", TabbedContent).active = "run-tab"
        self.query_one("#run-status", Static).update("Запускаю выгрузку…")
        self.query_one("#stats", Static).update("")
        self.query_one("#active-files", Static).update("")
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        self.launch_download()

    def set_form_disabled(self, disabled):
        for widget in self.query("#settings-tab Input, #settings-tab Button, #settings-tab Checkbox, #settings-tab Select"):
            widget.disabled = disabled
        self.query_one("#stop", Button).disabled = not disabled

    @work(exclusive=True)
    async def launch_download(self):
        try:
            encrypted = passwords.credential_fields(self.settings)[1] in self.settings
            if WINDOWS:
                self.control_directory = tempfile.TemporaryDirectory(prefix="yadisk-control-")
                self.stop_file = Path(self.control_directory.name) / "stop"
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(Path(__file__).with_name("download.py")),
                str(self.config_path), "--events", *(["--password-stdin"] if encrypted else []),
                *(["--stop-file", str(self.stop_file)] if self.stop_file else []),
                stdin=asyncio.subprocess.PIPE if encrypted else None, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=dict(os.environ, PYTHONUTF8="1"), **spawn_options(WINDOWS))
            if encrypted:
                try:
                    self.process.stdin.write((json.dumps(self.session_password) + "\n").encode("utf-8"))
                    await self.process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Ошибку старта движка прочитаем из stdout ниже.
                finally:
                    self.process.stdin.close()
            if self.stopping:
                self.request_backend_stop()
            while line := await self.process.stdout.readline():
                text = line.decode("utf-8", errors="replace").strip()
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    self.append_log(text)
                else:
                    self.handle_event(event)
            self.exit_code = await self.process.wait()
            self.poll_log()
            if not self.final_event:
                self.query_one("#run-status", Static).update(
                    f"Остановлено" if self.stopping else f"Ошибка запуска (код {self.exit_code}). См. журнал ниже.")
        except OSError as error:
            self.exit_code = 1
            self.append_log(f"Не удалось запустить: {error}")
            self.query_one("#run-status", Static).update("Ошибка запуска")
        finally:
            if self.process and self.process.returncode is None:
                try:
                    self.request_backend_stop()
                    await asyncio.wait_for(self.process.wait(), timeout=15)
                except asyncio.TimeoutError:
                    await kill_tree(self.process, WINDOWS)
                except ProcessLookupError:
                    pass
            self.process = None
            if self.control_directory is not None:
                self.control_directory.cleanup()
                self.control_directory = None
            self.stop_file = None
            self.running = False
            self.set_form_disabled(False)
            if self.quit_after_stop:
                self.exit(self.exit_code)

    def append_log(self, text):
        for password in (self.session_password, self.settings.get("app_password", ""), self.settings.get("oauth_token", ""),
                         self.query_one("#master-password", Input).value):
            if password:
                text = text.replace(password, "***")
        self.query_one("#run-log", RichLog).write(Text(text))

    def handle_event(self, event):
        kind = event.get("type")
        if kind == "run_started":
            self.run_folder = event["folder"]
            self.query_one("#report-path", Static).update(f"Отчёты: {self.run_folder}")
        elif kind == "job_started":
            self.poll_log()
            self.tail = None
            self.job_number += 1
            self.current_row = self.rows[self.job_number - 1] if self.job_number <= len(self.rows) else None
            self.update_row("Скачивание")
            self.query_one("#run-status", Static).update(f"Папка {self.job_number} из {len(self.settings['jobs'])}")
            self.query_one("#current", Static).update(f"{event['source']}\n→ {event['destination']}")
            self.append_log(f"Папка: {event['source']} → {event['destination']}")
        elif kind == "phase":
            self.poll_log()
            self.phase = event["phase"]
            self.phase_started = time.monotonic()
            self.tail = LogTail(event["log"])
            label = {"copy": "Обход и скачивание идут параллельно…",
                     "check": "Проверка локальных размеров по списку Диска…"}[self.phase]
            self.query_one("#phase", Static).update(label)
            self.update_row({"copy": "Скачивание", "check": "Проверка"}[self.phase])
            self.query_one("#stats", Static).update("")
            self.query_one("#active-files", Static).update("")
            self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        elif kind == "job_finished":
            self.poll_log()
            status = event["result"]["status"]
            incomplete = status == "failed" and not event["result"].get("manifest_complete", True)
            self.incomplete_jobs = self.incomplete_jobs or incomplete
            label = "Неполный список" if incomplete else {"ok": "Готово", "failed": "Ошибка", "interrupted": "Остановлено"}.get(status, status)
            self.update_row(label)
            self.append_log(f"Результат папки: {label}. Отчёт: {event['folder']}")
        elif kind == "run_finished":
            self.poll_log()
            self.final_event = True
            label = {"ok": "Всё скачано и проверено", "failed": "Завершено с ошибками — проверьте отчёты",
                     "interrupted": "Остановлено. Можно запустить повторно"}.get(event["status"], event["status"])
            if event["status"] == "failed" and self.incomplete_jobs:
                label = "Завершено: список неполный — проверьте отчёты"
            self.query_one("#run-status", Static).update(label)
            self.query_one("#report-path", Static).update(f"Отчёты: {event['folder']}")
            for row in self.rows[self.job_number:]:
                self.query_one("#job-table", DataTable).update_cell(row, "3", Text("Не запущено"))

    def update_row(self, status):
        if self.current_row is not None:
            self.query_one("#job-table", DataTable).update_cell(self.current_row, "3", Text(status))

    def poll_log(self):
        if self.running and not self.final_event and not self.stopping and self.phase == "check":
            elapsed = int(time.monotonic() - self.phase_started)
            label = f"Проверка локальных размеров: {elapsed} с"
            self.query_one("#phase", Static).update(label)
        if self.tail:
            try:
                for event in self.tail.read():
                    self.handle_log(event)
            except OSError as error:
                self.append_log(f"Не удалось прочитать журнал: {error}")
                self.tail = None

    def handle_log(self, event):
        stats = event.get("stats")
        if stats is not None:
            if self.phase == "copy":
                total, progress = stats.get("passTotal", 0), stats.get("processed", 0)
                detail = (f"Скачано: {stats.get('transfers', 0)}   Уже есть: {stats.get('present', 0)}   "
                          f"Отложено: {stats.get('deferred', 0)}   Не удалось: {stats.get('errors', 0)}\n"
                          f"{size(stats.get('bytes'))}   Скорость: {size(stats.get('speed'))}/с   "
                          f"Проход: {stats.get('pass', 1)}/{stats.get('passes', 1)} · обработано {progress}/{total}")
            else:
                total, progress = stats.get("totalChecks", 0), stats.get("checks", 0)
                detail = f"Проверено файлов: {progress}/{total}"
            scanning = self.phase == "copy" and stats.get("discovering", False)
            self.query_one("#progress", ProgressBar).update(total=None if scanning else total or None, progress=progress)
            self.query_one("#stats", Static).update(
                f"{detail}\nИзвестно файлов: {stats.get('listed', 0)}" if self.phase == "copy" else
                f"{detail}\nОшибки проверки: {stats.get('errors', 0)}")
            if scanning:
                label = f"Обход + скачивание · без новых файлов {stats.get('discovery_idle', 0):.0f} с"
            elif self.phase == "copy":
                label = ("Обход завершён · обработка очереди" if stats.get("discovery_complete", True)
                         else "Список неполный · скачиваю известные файлы")
            else:
                label = "Проверка размеров известных файлов"
            self.query_one("#phase", Static).update(label)
            active = stats.get("transferring") or []
            self.query_one("#active-files", Static).update("\n".join(
                f"{item.get('name', '')} · {size(item.get('bytes'))}/{size(item.get('size'))} · "
                f"без новых данных {item.get('idle', 0):.1f} с"
                for item in active[:4]))
        else:
            message = event.get("msg", "")
            name = event.get("object", "")
            self.append_log(f"{event.get('level', 'info').upper()} {name}: {message}" if name else message)

    @on(Button.Pressed, "#stop")
    def stop_pressed(self):
        self.action_stop()

    def request_backend_stop(self):
        if self.stop_file is not None:
            self.stop_file.write_text("stop\n", encoding="ascii")
        elif self.process and self.process.returncode is None:
            self.process.send_signal(signal.SIGINT)

    def action_stop(self):
        if self.running and not self.stopping:
            self.stopping = True
            self.query_one("#run-status", Static).update("Останавливаю и сохраняю отчёты…")
            self.query_one("#stop", Button).disabled = True
            if self.process and self.process.returncode is None:
                try:
                    self.request_backend_stop()
                except ProcessLookupError:
                    pass

    def action_request_quit(self):
        if self.running:
            if not isinstance(self.screen, ConfirmQuit):
                self.push_screen(ConfirmQuit(), self.confirm_quit)
        else:
            self.exit(self.exit_code)

    def confirm_quit(self, confirmed):
        if confirmed:
            self.quit_after_stop = True
            if self.running:
                self.action_stop()
            else:
                self.exit(self.exit_code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default=str(Path(__file__).with_name("settings.json")))
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("TUI нужен интерактивный терминал. Для CLI используйте python3 download.py")
    return YandexApp(args.config).run() or 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# TUI по умолчанию; --cli оставляет неинтерактивную выгрузку.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
if [[ "${1:-}" == "--cli" ]]; then
  shift
  exec "$PYTHON" "$SCRIPT_DIR/download.py" "$@"
fi
if ! "$PYTHON" -c 'import textual' 2>/dev/null; then
  printf 'Для TUI установите зависимости:\n  python3 -m venv "%s/.venv"\n  "%s/.venv/bin/python" -m pip install -r "%s/requirements.txt"\n' "$SCRIPT_DIR" "$SCRIPT_DIR" "$SCRIPT_DIR" >&2
  exit 1
fi
exec "$PYTHON" "$SCRIPT_DIR/tui.py" "$@"

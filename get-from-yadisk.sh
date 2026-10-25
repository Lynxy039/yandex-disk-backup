#!/usr/bin/env bash
# Локальные зависимости готовятся автоматически, --cli запускает движок без TUI.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null; then
  printf 'Установите Python 3.9+ и запустите лаунчер снова. macOS: brew install python\n' >&2
  exit 1
fi
exec python3 "$SCRIPT_DIR/bootstrap.py" "$@"

#!/usr/bin/env bash
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

python_command="${REPOSTBOT_PYTHON:-python3}"
if ! command -v "$python_command" >/dev/null 2>&1; then
    printf 'Python 3.11+ не найден. Установите python3 и python3-venv.\n' >&2
    exit 1
fi
if ! "$python_command" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    printf 'Нужен Python 3.11 или новее.\n' >&2
    exit 1
fi

runtime_dir="$project_dir/.runtime/linux-venv"
if [[ ! -x "$runtime_dir/bin/python" ]]; then
    mkdir -p "$(dirname "$runtime_dir")"
    if ! "$python_command" -m venv "$runtime_dir"; then
        printf 'Не удалось создать окружение. Установите пакет python3-venv и повторите запуск.\n' >&2
        exit 1
    fi
fi
runtime_python="$runtime_dir/bin/python"

# Optional: reposts and saved answers can work without the ChatGPT login modules.
if [[ "${REPOSTBOT_INSTALL_DEPS:-1}" == "1" ]] && ! "$runtime_python" -c 'import jwt, cryptography' >/dev/null 2>&1; then
    printf 'Устанавливаю зависимости входа ChatGPT...\n'
    if ! "$runtime_python" -m pip install --disable-pip-version-check --timeout 10 --retries 1 'PyJWT[crypto]>=2.10.1,<3'; then
        printf 'Не удалось установить поддержку входа ChatGPT. Остальные функции доступны; повторите запуск позже.\n' >&2
    fi
fi

export DATA_DIR="${DATA_DIR:-$project_dir/data}"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true
password_file="$DATA_DIR/admin-password.txt"
if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    if [[ ! -s "$password_file" ]]; then
        "$runtime_python" -c 'import secrets,sys; from pathlib import Path; Path(sys.argv[1]).write_text(secrets.token_urlsafe(24) + "\n", encoding="ascii")' "$password_file"
        chmod 600 "$password_file" 2>/dev/null || true
    fi
    ADMIN_PASSWORD="$(<"$password_file")"
    export ADMIN_PASSWORD
    printf 'Пароль панели: %s\n' "$ADMIN_PASSWORD"
fi

export WEB_HOST="${WEB_HOST:-127.0.0.1}"
export WEB_PORT="${WEB_PORT:-8787}"
export REPOSTBOT_LINUX_LAUNCHER=1
printf 'Панель: http://%s:%s\n' "$WEB_HOST" "$WEB_PORT"
printf 'Для остановки используйте кнопку «Выключить бота» в панели или Ctrl+C.\n'

apply_pending_update() {
    local marker="$DATA_DIR/updates/apply-requested.txt"
    [[ -f "$marker" ]] || return 0
    printf 'Устанавливаю подготовленное обновление...\n'
    "$runtime_python" -m repostbot.update apply "$DATA_DIR"
    chmod 700 "$project_dir/start_linux.sh"
    exec bash "$project_dir/start_linux.sh"
}

apply_pending_update
if "$runtime_python" -m repostbot.app; then
    exit 0
else
    status=$?
fi
if [[ "$status" -eq 42 ]]; then
    apply_pending_update
    printf 'Обновление не было подготовлено.\n' >&2
    exit 1
fi
exit "$status"

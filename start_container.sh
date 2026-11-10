#!/bin/sh
# Container launcher. /app must be a persistent bind mount; data stays in /app/data.
set -eu
umask 077
project_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_dir"
export DATA_DIR="${DATA_DIR:-/data}"
export WEB_HOST="${WEB_HOST:-0.0.0.0}"
export WEB_PORT="${WEB_PORT:-8787}"
export REPOSTBOT_CONTAINER_LAUNCHER=1
export PYTHONPATH="$project_dir:$DATA_DIR/.runtime/python${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true
mkdir -p "$DATA_DIR/.runtime/python"

if [ ! -f "$project_dir/version.json" ] || [ ! -f "$project_dir/repostbot/app.py" ]; then
    printf 'Файлы программы не найдены в %s. Скопируйте содержимое папки RepostBot в каталог, подключённый как /app.\n' "$project_dir" >&2
    exit 1
fi

if [ -z "${ADMIN_PASSWORD:-}" ]; then
    password_file="$DATA_DIR/admin-password.txt"
    if [ ! -s "$password_file" ]; then
        python3 -c 'import secrets,sys; from pathlib import Path; Path(sys.argv[1]).write_text(secrets.token_urlsafe(24) + "\n", encoding="ascii")' "$password_file"
        chmod 600 "$password_file" 2>/dev/null || true
    fi
    ADMIN_PASSWORD="$(cat "$password_file")"
    export ADMIN_PASSWORD
    printf 'Пароль панели: %s\n' "$ADMIN_PASSWORD"
fi

# Optional components can take minutes to download on the first CasaOS/ZimaOS
# launch. Keep the web panel available while they are being installed.
install_optional_components() {
    if [ "${REPOSTBOT_INSTALL_DEPS:-1}" = 1 ] && { ! command -v tesseract >/dev/null 2>&1 || ! tesseract --list-langs 2>/dev/null | grep -q '^rus$'; }; then
        printf 'Устанавливаю локальный OCR Tesseract с русским языком в фоне...\n'
        if ! (apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus); then
            printf 'Локальный OCR пока недоступен. Модели с поддержкой изображений продолжат работать.\n' >&2
        fi
    fi
    # python:3.12-slim has no optional ChatGPT login modules.
    if [ "${REPOSTBOT_INSTALL_DEPS:-1}" = 1 ] && ! python3 -c 'import jwt, cryptography' 2>/dev/null; then
        printf 'Устанавливаю зависимости входа ChatGPT в фоне...\n'
        if ! python3 -m pip install --disable-pip-version-check --no-cache-dir --target "$DATA_DIR/.runtime/python" 'PyJWT[crypto]>=2.10.1,<3'; then
            printf 'Зависимости входа ChatGPT пока недоступны. Повторите запуск позже.\n' >&2
        fi
    fi

    if [ "${REPOSTBOT_INSTALL_DEPS:-1}" = 1 ] && ! python3 -c 'import telethon' 2>/dev/null; then
        printf 'Устанавливаю модуль чтения участников Telegram в фоне...\n'
        if ! python3 -m pip install --disable-pip-version-check --no-cache-dir --target "$DATA_DIR/.runtime/python" 'Telethon>=1.45,<2'; then
            printf 'Telethon пока недоступен; обычная работа бота продолжится. Повторите запуск позже.\n' >&2
        fi
    fi

    if [ "${REPOSTBOT_INSTALL_DEPS:-1}" = 1 ] && ! python3 -c 'import pypdf' 2>/dev/null; then
        printf 'Устанавливаю чтение PDF в фоне...\n'
        if ! python3 -m pip install --disable-pip-version-check --no-cache-dir --target "$DATA_DIR/.runtime/python" 'pypdf>=5,<7'; then
            printf 'Чтение PDF пока недоступно; остальные вложения работают.\n' >&2
        fi
    fi

    if [ "${REPOSTBOT_INSTALL_BROWSER:-1}" = 1 ] && ! python3 -c 'import websocket' 2>/dev/null; then
        printf 'Устанавливаю локальное управление Chrome для Twitch в фоне...\n'
        if ! python3 -m pip install --disable-pip-version-check --no-cache-dir --target "$DATA_DIR/.runtime/python" 'websocket-client>=1.8,<2'; then
            printf 'Управление Chrome пока недоступно; повторите запуск контейнера позже.\n' >&2
        fi
    fi
    if [ "${REPOSTBOT_INSTALL_BROWSER:-1}" = 1 ] && ! command -v Xvfb >/dev/null 2>&1; then
        printf 'Устанавливаю виртуальный экран Twitch-наблюдателя в фоне...\n'
        if ! (apt-get update && apt-get install -y --no-install-recommends xvfb); then
            printf 'Виртуальный экран пока недоступен; повторите запуск контейнера позже.\n' >&2
        fi
    fi

    # VK sometimes returns a JavaScript shell that Chromium can render.
    if [ "${REPOSTBOT_INSTALL_BROWSER:-1}" = 1 ] && ! command -v chromium >/dev/null 2>&1; then
        printf 'Устанавливаю Chromium и curl для публичных страниц VK и Telegram в фоне...\n'
        if ! (apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates fonts-liberation curl); then
            printf 'Chromium пока не установлен; прямой разбор HTML остаётся доступен.\n' >&2
        fi
    fi
    if ! command -v curl >/dev/null 2>&1; then
        printf 'Устанавливаю curl для резервной отправки изображений Telegram в фоне...\n'
        if ! (apt-get update && apt-get install -y --no-install-recommends curl); then
            printf 'curl пока не установлен; резервная отправка изображений недоступна.\n' >&2
        fi
    fi
    # Existing CasaOS installations also receive Chrome without replacing /app
    # or deleting the persistent Twitch profile. Never install an amd64 package
    # on ARM: Chromium remains the fallback there.
    if [ "${REPOSTBOT_INSTALL_BROWSER:-1}" = 1 ] && [ "$(dpkg --print-architecture)" = amd64 ] && ! command -v google-chrome-stable >/dev/null 2>&1 && ! command -v google-chrome >/dev/null 2>&1; then
        printf 'Устанавливаю Google Chrome для входа Twitch в фоне...\n'
        if ! (
            set -eu
            chrome_temp="$(mktemp -d /tmp/repostbot-chrome-XXXXXX)"
            trap 'rm -rf "$chrome_temp"' EXIT
            curl --fail --location --retry 2 --max-time 180 https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o "$chrome_temp/chrome.deb"
            apt-get update
            apt-get install -y --no-install-recommends "$chrome_temp/chrome.deb"
        ); then
            printf 'Google Chrome пока не установлен. Chromium остаётся доступен, но Twitch может отклонить вход. Повторите перезапуск контейнера позже.\n' >&2
        else
            printf 'Google Chrome установлен. Нажмите «Перезапустить браузер» в разделе наблюдателя; сессия сохранится.\n'
        fi
    fi
    if ! command -v ffmpeg >/dev/null 2>&1; then
        printf 'Устанавливаю ffmpeg для первых кадров анимированных стикеров в фоне...\n'
        if ! (apt-get update && apt-get install -y --no-install-recommends ffmpeg); then
            printf 'ffmpeg пока не установлен; для анимаций будет показана заглушка.\n' >&2
        fi
    fi
}

child=''
stop_child() {
    if [ -n "$child" ]; then
        kill "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
trap stop_child TERM INT

apply_pending_update() {
    if [ -f "$DATA_DIR/updates/apply-requested.txt" ]; then
        printf 'Устанавливаю подготовленное обновление...\n'
        python3 -m repostbot.update apply "$DATA_DIR"
    fi
}

apply_pending_update
install_optional_components &
printf 'Панель запускается; дополнительные компоненты устанавливаются в фоне.\n'
while :; do
    python3 -m repostbot.app &
    child=$!
    if wait "$child"; then
        exit 0
    else
        result=$?
    fi
    child=''
    if [ "$result" -ne 42 ]; then
        exit "$result"
    fi
    apply_pending_update
done

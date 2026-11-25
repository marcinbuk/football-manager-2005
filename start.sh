#!/usr/bin/env bash
set -e

mkdir -p /tmp/fm-sessions \
         /tmp/nginx_body \
         /tmp/nginx_proxy \
         /tmp/nginx_fastcgi \
         /tmp/nginx_uwsgi \
         /tmp/nginx_scgi

if [ "${1:-}" = "--daemon" ]; then
    WORK_DIR="${PWD}"
    export GAME_DIR="${WORK_DIR}/game"
    export SAVES_DIR="${WORK_DIR}/saves"
    export REPO_DIR="${WORK_DIR}"

    mkdir -p "${GAME_DIR}" "${SAVES_DIR}"

    if [ ! -f "${GAME_DIR}/fm2005.exe" ]; then
        if [ -f "${WORK_DIR}/game.tar.gz" ]; then
            echo "--> Rozpakowywanie game.tar.gz do ${GAME_DIR}..."
            tar -xzf "${WORK_DIR}/game.tar.gz" -C "${GAME_DIR}" || true
        elif command -v gh >/dev/null 2>&1; then
            echo "--> Pobieranie game.tar.gz z GitHub Releases..."
            if (cd "${WORK_DIR}" && gh release download -p "game.tar.gz" -D /tmp --clobber); then
                tar -xzf /tmp/game.tar.gz -C "${GAME_DIR}" || true
                rm -f /tmp/game.tar.gz
            else
                echo "UWAGA: Nie znaleziono jeszcze game.tar.gz w Releases — serwer WWW wystartuje i pobierze gre przy pierwszym wejsciu."
            fi
        fi
    fi

    if ! pgrep -f "nginx -c /app/nginx.conf" >/dev/null 2>&1; then
        nginx -c /app/nginx.conf -g "daemon off;" >/tmp/nginx-fm2005.log 2>&1 &
    fi

    if ! pgrep -f "/app/session_manager.py" >/dev/null 2>&1; then
        nohup python3 /app/session_manager.py >/tmp/fm2005.log 2>&1 &
    fi

    for _ in $(seq 1 50); do
        if curl -sf http://127.0.0.1:7860/api/health >/dev/null 2>&1; then
            echo "Football Manager 2005 jest gotowy na porcie 7860."
            exit 0
        fi
        sleep 0.2
    done

    echo "Serwer uruchomiony (sprawdz /tmp/fm2005.log w razie problemow)."
    exit 0
fi

export GAME_DIR="${GAME_DIR:-/app/game}"
export SAVES_DIR="${SAVES_DIR:-/data/saves}"

mkdir -p "${GAME_DIR}" "${SAVES_DIR}"

if [ ! -f "${GAME_DIR}/fm2005.exe" ] && [ -f "/app/game.tar.gz" ]; then
    tar -xzf /app/game.tar.gz -C "${GAME_DIR}"
fi

nginx -c /app/nginx.conf -g "daemon off;" &

exec python3 /app/session_manager.py

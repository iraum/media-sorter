#!/usr/bin/env bash
#
# start.sh - Launch the Media Sorter Flask app
#
#   ./start.sh                run in the foreground (banner + Ctrl+C to stop)
#   ./start.sh --background   run detached: no terminal window needed, logs to
#                             media-sorter.log, opens your browser, and keeps
#                             running after the launching window is closed.
#                             (this is what the media-sorter.desktop launcher uses)
#   ./start.sh --stop         stop a running background instance
#
set -euo pipefail

cd "$(dirname "$0")"

# Defaults (override by exporting before running, or editing here)
HOST="${FLASK_HOST:-127.0.0.1}"
PORT="${FLASK_PORT:-5070}"   # not 5060: browsers block it (SIP)
if [ -z "${PYTHON:-}" ] && [ -x ./venv/bin/python ]; then
    PYTHON=./venv/bin/python
fi
PYTHON="${PYTHON:-python3}"

URL="http://${HOST}:${PORT}"
LOGFILE="$(pwd)/media-sorter.log"
PIDFILE="$(pwd)/media-sorter.pid"

# --- Parse arguments ------------------------------------------------------
BACKGROUND=0
STOP=0
for arg in "$@"; do
    case "$arg" in
        -b|--background) BACKGROUND=1 ;;
        -k|--stop)       STOP=1 ;;
    esac
done
[ "${MSORT_BACKGROUND:-0}" = "1" ] && BACKGROUND=1

# Show a desktop notification if one is available (there's no terminal when
# launched by double-click), otherwise just print to stdout/log.
notify() {
    echo "$1"
    command -v notify-send >/dev/null 2>&1 && notify-send "Media Sorter" "$1" || true
}

# --- Stop mode: kill a running background instance ------------------------
if [ "$STOP" = "1" ]; then
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
        pid="$(cat "$PIDFILE")"
        kill "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
        notify "Stopped (PID $pid)."
    else
        rm -f "$PIDFILE" 2>/dev/null || true
        notify "Not running."
    fi
    exit 0
fi

# Open the browser once the server is accepting connections (best effort).
open_browser() {
    command -v xdg-open >/dev/null 2>&1 || return 0
    (
        set +e
        for _ in $(seq 1 40); do
            if (exec 3<>"/dev/tcp/${HOST}/${PORT}") 2>/dev/null; then
                exec 3>&- 3<&-
                break
            fi
            sleep 0.25
        done
        xdg-open "$URL" >/dev/null 2>&1
    ) &
}

# --- Background mode: relaunch detached, then hand control back -----------
# The re-launched copy sets MSORT_DETACHED=1 and falls through to the normal
# foreground path below, but with no controlling terminal and output logged.
if [ "$BACKGROUND" = "1" ] && [ "${MSORT_DETACHED:-0}" != "1" ]; then
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
        notify "Already running (PID $(cat "$PIDFILE")). Opening ${URL}"
        open_browser
        exit 0
    fi
    MSORT_DETACHED=1 PYTHON="$PYTHON" setsid "$0" "$@" </dev/null >>"$LOGFILE" 2>&1 &
    open_browser
    notify "Starting at ${URL}"
    exit 0
fi

cat <<'BANNER'
  __  __          _ _         ____             _
 |  \/  | ___  __| (_) __ _  / ___|  ___  _ __| |_ ___ _ __
 | |\/| |/ _ \/ _` | |/ _` | \___ \ / _ \| '__| __/ _ \ '__|
 | |  | |  __/ (_| | | (_| |  ___) | (_) | |  | ||  __/ |
 |_|  |_|\___|\__,_|_|\__,_| |____/ \___/|_|   \__\___|_|
BANNER

echo
echo "  Media Sorter - browse images & videos, file them with one key"
echo "  -----------------------------------------------------------"
echo "  Host    : ${HOST}"
echo "  Port    : ${PORT}"
echo "  URL     : ${URL}"
echo "  Python  : $(${PYTHON} --version 2>&1)  (${PYTHON})"
echo "  Roots   : ${MEDIA_ROOTS:-/mnt/spielraum:$HOME}"
echo "  Start   : ${MEDIA_START_DIR:-(first root)}"
echo "  Logs    : security.log, moves.log"
echo

if [ -z "${SECRET_KEY:-}" ]; then
    echo "  [!] SECRET_KEY not set - a random key will be generated"
    echo "      (sessions won't persist across restarts)"
    echo
fi

export FLASK_HOST="${HOST}"
export FLASK_PORT="${PORT}"

echo $$ > "$PIDFILE"

if [ "${MSORT_DETACHED:-0}" = "1" ]; then
    echo "  Started in background (PID $$)."
    echo "  Stop it with:  ./start.sh --stop"
    echo
else
    echo "  Starting server... (Ctrl+C to stop)"
    echo
fi

exec "${PYTHON}" app.py

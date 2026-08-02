#!/bin/bash
set -e

LABEL="com.poapkings.elixir"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
CONTROL_LOG="${ELIXIR_CONTROL_LOG:-$PROJECT_DIR/logs/elixir-control.log}"

record_control_action() {
    local action="$1"
    local timestamp revision tty_name

    if ! mkdir -p "$(dirname "$CONTROL_LOG")"; then
        echo "Warning: unable to create control-log directory; continuing without audit record." >&2
        return
    fi
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    revision="$(git rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
    tty_name="$(tty 2>/dev/null || printf 'none')"
    # This is intentionally recorded before a stop.  A later SIGTERM receipt
    # without a nearby line is external to the supported control path.
    if ! printf '%s action=%s uid=%s user=%q pid=%s ppid=%s tty=%q revision=%s reason=%q\n' \
        "$timestamp" "$action" "$(id -u)" "${USER:-unknown}" "$$" "$PPID" \
        "$tty_name" "$revision" "${ELIXIR_RESTART_REASON:-unspecified}" >> "$CONTROL_LOG"; then
        echo "Warning: unable to write control-log record; continuing without audit record." >&2
    fi
}

status() {
    if launchctl list | grep -q "$LABEL"; then
        echo "elixir-bot is running."
    else
        echo "elixir-bot is stopped."
    fi
}

stop_bot() {
    echo "==> Stopping elixir-bot..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    sleep 1
    status
}

start_bot() {
    if [ ! -f "$PLIST" ]; then
        echo "Error: plist not found at $PLIST"
        echo "Run '$0 install' first."
        exit 1
    fi
    echo "==> Starting elixir-bot..."
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    sleep 3
    status
}

restart_bot() {
    backup_db
    stop_bot
    start_bot
}

install_bot() {
    echo "==> Installing launchd plist..."
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/.venv/bin/python</string>
        <string>$PROJECT_DIR/elixir.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/elixir-v5.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/elixir-v5.log</string>
</dict>
</plist>
PLIST
    echo "Installed $PLIST"
}

upgrade_bot() {
    backup_db
    stop_bot

    echo "==> Pulling latest from origin..."
    git pull --ff-only origin main

    echo "==> Updating dependencies..."
    uv sync --locked --no-dev

    start_bot
}

backup_db() {
    echo "==> Backing up the v5.1 operational database..."
    "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/backup_db.py"
}

run_activity() {
    if [ "${2:-}" != "run" ] || [ -z "${3:-}" ]; then
        echo "Usage: $0 activity run <activity-key>"
        exit 1
    fi
    "$PROJECT_DIR/.venv/bin/python" -m runtime.activity_runner run "$3"
}

case "${1:-}" in
    stop)     record_control_action stop; stop_bot ;;
    start)    record_control_action start; start_bot ;;
    restart)  record_control_action restart; restart_bot ;;
    upgrade)  record_control_action upgrade; upgrade_bot ;;
    install)  install_bot ;;
    status)   status ;;
    backup)   backup_db ;;
    activity) run_activity "$@" ;;
    *)
        echo "Usage: $0 {start|stop|restart|upgrade|install|status|backup|activity run <activity-key>}"
        exit 1
        ;;
esac

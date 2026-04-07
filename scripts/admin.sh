#!/bin/bash
set -e

LABEL="com.poapkings.elixir"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

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
        <string>$PROJECT_DIR/venv/bin/python</string>
        <string>$PROJECT_DIR/elixir.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/elixir.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/elixir.err</string>
</dict>
</plist>
PLIST
    echo "Installed $PLIST"
}

upgrade_bot() {
    stop_bot

    echo "==> Pulling latest from origin..."
    git pull origin main

    echo "==> Updating dependencies..."
    source venv/bin/activate
    pip install -q -r requirements.txt

    start_bot
}

backup_db() {
    echo "==> Backing up elixir.db..."
    source "$PROJECT_DIR/venv/bin/activate"
    python "$PROJECT_DIR/scripts/backup_db.py"
}

case "${1:-}" in
    stop)     stop_bot ;;
    start)    start_bot ;;
    restart)  restart_bot ;;
    upgrade)  upgrade_bot ;;
    install)  install_bot ;;
    status)   status ;;
    backup)   backup_db ;;
    *)
        echo "Usage: $0 {start|stop|restart|upgrade|install|status|backup}"
        exit 1
        ;;
esac

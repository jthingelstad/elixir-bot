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
    launchctl unload "$PLIST" 2>/dev/null || true
    sleep 1
    status
}

start_bot() {
    echo "==> Starting elixir-bot..."
    launchctl load -w "$PLIST"
    sleep 3
    status
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

case "${1:-}" in
    stop)     stop_bot ;;
    start)    start_bot ;;
    upgrade)  upgrade_bot ;;
    status)   status ;;
    *)
        echo "Usage: $0 {start|stop|upgrade|status}"
        exit 1
        ;;
esac

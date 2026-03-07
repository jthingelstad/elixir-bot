#!/bin/bash
set -e

LABEL="com.poapkings.elixir"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "==> Stopping elixir-bot..."
launchctl unload "$PLIST" 2>/dev/null || true
sleep 1

echo "==> Pulling latest from origin..."
git pull origin main

echo "==> Updating dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

echo "==> Starting elixir-bot..."
launchctl load "$PLIST"
sleep 3

if launchctl list | grep -q "$LABEL"; then
    echo "==> elixir-bot is running."
else
    echo "==> ERROR: elixir-bot failed to start. Check elixir.log for details."
    exit 1
fi

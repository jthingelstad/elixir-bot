#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate

# Kill any orphaned elixir.py process before starting
pkill -f 'python elixir.py' 2>/dev/null
sleep 1

exec python elixir.py

#!/usr/bin/env bash
set -euo pipefail

# Entrypoint for Chronos container
# 1. Catch up on parsing any unparsed gmail bodies
# 2. Start watcher in background (Gmail polling)
# 3. Start triggers in foreground (main Chronos loop)

echo "[Chronos] Starting up..."

# Parse any existing unparsed bodies
if [ -f /app/logs/gmail_bodies.jsonl ]; then
    echo "[Chronos] Catching up on parsing..."
    python3 /app/parse_tickets.py 2>&1 | sed 's/^/[Parse] /' || true
fi

# Start watcher in background
echo "[Chronos] Starting Gmail watcher..."
python3 -u /app/watcher.py &
WATCHER_PID=$!

# Start triggers in foreground
echo "[Chronos] Starting trigger loop..."
exec python3 -u /app/chronos/triggers.py
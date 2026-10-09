#!/bin/bash
# ============================================================
#  start.sh - PO Token server + Telegram bot
# ============================================================
set -e

echo "=========================================="
echo "  Universal Media Downloader Bot"
echo "=========================================="

SERVER_DIR=/opt/bgutil-ytdlp-pot-provider/server
PO_PORT="${PO_TOKEN_PORT:-4416}"
PO_LOG=/var/log/pot-server.log
export PO_TOKEN_SERVER_URL="http://127.0.0.1:${PO_PORT}"

# ------------------------------------------------------------
# 1. PO Token server - localhost only (it is unauthenticated)
# ------------------------------------------------------------
echo "Starting PO Token server on 127.0.0.1:${PO_PORT}..."
(
    cd "$SERVER_DIR"
    exec deno run \
        --allow-env \
        --allow-net \
        --allow-ffi="$SERVER_DIR/node_modules" \
        --allow-read="$SERVER_DIR/node_modules" \
        src/main.ts --port "$PO_PORT" --host 127.0.0.1
) > "$PO_LOG" 2>&1 &
PO_PID=$!

PO_READY=0
for i in $(seq 1 30); do
    if curl -fsS "$PO_TOKEN_SERVER_URL/ping" > /tmp/pot_ping.json 2>/dev/null; then
        PO_READY=1
        break
    fi
    if ! kill -0 "$PO_PID" 2>/dev/null; then
        echo "ERROR: PO Token server exited during startup."
        echo "----- last lines -----"
        tail -30 "$PO_LOG" || true
        break
    fi
    sleep 1
done

if [ "$PO_READY" = "1" ]; then
    echo "PO Token server ready: $(cat /tmp/pot_ping.json)"
else
    echo "WARNING: PO Token server is NOT ready."
    echo "         The bot still starts, but YouTube downloads that need a"
    echo "         PO token will fall back to other player clients."
fi

# ------------------------------------------------------------
# 2. Telegram bot (becomes PID 1)
# ------------------------------------------------------------
echo "Starting Telegram bot..."
echo "PO_TOKEN_SERVER_URL=$PO_TOKEN_SERVER_URL"
cd /app
exec python bot.py

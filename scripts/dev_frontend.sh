#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

# Start the Vite dev server (frontend) on port 8159 for Playwright E2E.
# PID file + kill $(cat .pid) lifecycle — never pattern-kill.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/frontend_dev.pid"
LOG_FILE="$PROJECT_DIR/frontend_dev.log"
PORT="${1:-8159}"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "前端 dev server 已在运行 (PID: $PID, port: $PORT)"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

cd "$PROJECT_DIR/frontend"

echo "启动前端 dev server (port: $PORT)..."
# 脱离本会话进程组（macOS 无 setsid，用 python start_new_session）——否则
# harness/终端回收进程组时 dev server 被连带杀掉（nohup 只挡 SIGHUP）。
DEV_PID=$(python3 -c "
import subprocess, sys
p = subprocess.Popen(
    ['npx', 'vite', '--host', '127.0.0.1', '--port', '$PORT'],
    stdout=open('$LOG_FILE', 'w'), stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL, start_new_session=True,
)
sys.stdout.write(str(p.pid))
")
echo "$DEV_PID" > "$PID_FILE"

# Wait for the server to accept connections (max 30s).
# 注意：vite dev 是明文 HTTP——曾经用 https:// 探测，恒失败误报「启动超时」。
for _ in $(seq 1 30); do
    if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
        echo "前端 dev server 启动成功 (PID: $DEV_PID, port: $PORT)"
        echo "日志: $LOG_FILE"
        exit 0
    fi
    sleep 1
done

echo "前端 dev server 启动超时，查看日志: $LOG_FILE"
exit 1

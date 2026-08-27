#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/chatllm.pid"
LOG_FILE="$PROJECT_DIR/chatllm.log"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
# Windows venv 布局为 .venv\Scripts\python.exe（Git-Bash 下可用正斜杠访问）
[ -x "$VENV_PYTHON" ] || VENV_PYTHON="$PROJECT_DIR/.venv/Scripts/python.exe"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "错误: 未找到 venv 解释器（$VENV_PYTHON）——请先 python3 -m venv .venv 并 pip install -r backend/requirements.txt"
    exit 1
fi

cd "$PROJECT_DIR/backend"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "服务已在运行 (PID: $PID)"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

PORT=$(awk '/^\[/ { section=$0 } /^[ \t]*port[ \t]*=/ { if (section ~ /\[server\]/) { sub(/.*=[ \t]*/, ""); sub(/[ \t]*$/, ""); gsub(/[ "]/, ""); print } }' config.toml)
HOST=$(awk '/^\[/ { section=$0 } /^[ \t]*host[ \t]*=/ { if (section ~ /\[server\]/) { sub(/.*=[ \t]*/, ""); sub(/[ \t]*$/, ""); gsub(/[ "]/, ""); print } }' config.toml)

if [ -z "$PORT" ]; then
    PORT=8158
fi
if [ -z "$HOST" ]; then
    HOST="0.0.0.0"
fi

echo "启动 Weave Thinker 服务..."
echo "监听地址: $HOST:$PORT"

SSL_OPTS=""
if [ -f "key.pem" ] && [ -f "cert.pem" ]; then
    SSL_OPTS="--ssl-keyfile=key.pem --ssl-certfile=cert.pem"
    echo "SSL: 已启用 (HTTPS)"
else
    echo "SSL: 未启用 (HTTP)"
fi

nohup "$VENV_PYTHON" -m uvicorn main:app --host "$HOST" --port "$PORT" $SSL_OPTS > "$LOG_FILE" 2>&1 &
NEW_PID=$!

echo $NEW_PID > "$PID_FILE"
echo $NEW_PID > "$PROJECT_DIR/chatllm.pid"

sleep 1

if ! kill -0 "$NEW_PID" 2>/dev/null; then
    echo "服务启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
echo "服务启动成功 (PID: $NEW_PID)"
# MSYS/Git-Bash（Windows）下 $! 常是 nohup 包装 shell 的 PID，uvicorn 才是真正
# 持有端口的进程；取监听进程的 PID 修正，否则 stop.sh 杀不到服务
# （2026-08-27 Win Server 2019 部署实证：pgrep -f/ps args 在 MSYS 均不可靠，
# 从 netstat 的 LISTENING pid 反查最稳）
case "$(uname -s 2>/dev/null)" in
    *MINGW*|*MSYS*|*CYGWIN*)
        LISTEN_PID=""
        for _ in 1 2 3; do
            sleep 2
            LISTEN_PID=$(netstat -ano 2>/dev/null | awk -v p=":$PORT" '$4 == "LISTENING" && $2 ~ (p "$") {print $5; exit}')
            [ -n "$LISTEN_PID" ] && break
        done
        if [ -n "$LISTEN_PID" ]; then
            # python 归属校验：防止端口冲突时把无关进程的 pid 写进 PID 文件
            if tasklist //FI "PID eq $LISTEN_PID" 2>/dev/null | grep -qi "python"; then
                echo $LISTEN_PID > "$PID_FILE"
                echo $LISTEN_PID > "$PROJECT_DIR/chatllm.pid"
                echo "Windows: PID 已修正为实际监听进程 ($LISTEN_PID)"
            else
                echo "警告: 端口 $PORT 的监听进程不是 python (pid $LISTEN_PID)，疑似端口冲突；PID 文件保留 $NEW_PID，请检查 $LOG_FILE 与 netstat"
            fi
        else
            echo "警告: 端口 $PORT 未探到监听（可能仍在绑定或启动失败）；PID 文件记录 $NEW_PID，稍后用 scripts/status.sh 复核"
        fi
        ;;
esac
echo "日志文件: $LOG_FILE"

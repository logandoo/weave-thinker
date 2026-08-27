#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/chatllm.pid"
LOG_FILE="$PROJECT_DIR/chatllm.log"

if [ ! -f "$PID_FILE" ]; then
    echo "状态: 已停止 (无 PID 文件)"
    exit 0
fi

PID=$(cat "$PID_FILE")
if [ -z "$PID" ]; then
    echo "状态: 已停止 (PID 文件为空)"
    exit 0
fi

# Windows 原生进程（python.exe）无法用 kill -0 探活（MSYS 只认自家进程 pid），
# Git-Bash 下回退到 tasklist 精确 pid 匹配（2026-08-27 实证）
pid_alive() {
    if kill -0 "$1" 2>/dev/null; then
        return 0
    fi
    case "$(uname -s 2>/dev/null)" in
        *MINGW*|*MSYS*|*CYGWIN*)
            tasklist //FI "PID eq $1" 2>/dev/null | grep -q " $1 "
            return $?
            ;;
    esac
    return 1
}

if pid_alive "$PID"; then
    echo "状态: 运行中"
    echo "PID: $PID"

    PORT=$(awk '/^\[/ { section=$0 } /^[ \t]*port[ \t]*=/ { if (section ~ /\[server\]/) { sub(/.*=[ \t]*/, ""); sub(/[ \t]*$/, ""); gsub(/[ "]/, ""); print } }' "$PROJECT_DIR/backend/config.toml")
    if [ -z "$PORT" ]; then
        PORT=8158
    fi

    SCHEME="http"
    CURL_OPTS="-s --connect-timeout 2"
    if [ -f "$PROJECT_DIR/backend/key.pem" ] && [ -f "$PROJECT_DIR/backend/cert.pem" ]; then
        SCHEME="https"
        CURL_OPTS="-s -k --connect-timeout 2"
    fi

    if command -v curl > /dev/null 2>&1; then
        if curl $CURL_OPTS "$SCHEME://localhost:$PORT/docs" > /dev/null 2>&1; then
            echo "API: 正常 ($SCHEME://localhost:$PORT)"
        else
            echo "API: 无响应"
        fi
    fi
else
    echo "状态: 已停止 (PID 文件过期)"
fi

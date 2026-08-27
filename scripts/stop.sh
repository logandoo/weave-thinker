#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/chatllm.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "服务未运行 (无 PID 文件)"
    exit 0
fi

PID=$(cat "$PID_FILE")
if [ -z "$PID" ]; then
    echo "服务未运行 (PID 文件为空)"
    rm -f "$PID_FILE"
    exit 0
fi

# Windows 原生进程（python.exe）无法用 kill 信号终止（MSYS 只认自家进程 pid），
# Git-Bash 下回退到 taskkill 按 pid 精确终结（不用模式杀，符合宿主安全约定）
# （2026-08-27 Win Server 2019 部署实证）
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
    echo "停止服务 (PID: $PID)..."
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            echo "强制终止..."
            kill -9 "$PID"
        fi
    else
        case "$(uname -s 2>/dev/null)" in
            *MINGW*|*MSYS*|*CYGWIN*)
                taskkill //PID "$PID" //T 2>/dev/null || {
                    echo "强制终止..."
                    taskkill //F //PID "$PID" //T 2>/dev/null
                }
                ;;
        esac
    fi
    sleep 1
    if pid_alive "$PID"; then
        echo "警告: 进程未退出，请手动 taskkill //F //PID $PID"
    else
        echo "服务已停止"
    fi
else
    echo "服务未运行 (PID 文件过期)"
fi

rm -f "$PID_FILE"

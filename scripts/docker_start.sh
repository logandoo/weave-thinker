#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

#
# 启动 Docker 部署栈（构建 + 后台拉起 + 健康等待）。
# 用法：./scripts/docker_start.sh [docker compose up 附加参数]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "错误: 未找到 docker CLI（请先安装 Docker / colima 等运行时）"
    exit 1
fi
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "错误: docker compose 不可用（Docker Desktop / compose 插件需已安装）"
    exit 1
fi

if [ ! -f backend/config.toml ]; then
    echo "错误: backend/config.toml 不存在（或是目录——挂载源缺失时 Docker 会建目录）。请执行："
    echo "  rm -rf backend/config.toml && cp backend/config.toml.example backend/config.toml"
    echo "  并把其中 [database] host 改为 \"db\"、填写 jwt_secret_key。"
    exit 1
fi
if [ ! -f backend/config_model.toml ]; then
    echo "错误: backend/config_model.toml 不存在（或是目录）。请执行："
    echo "  rm -rf backend/config_model.toml && cp backend/config_model.toml.example backend/config_model.toml"
    echo "  并至少填写一个 LLM 端点（base_url / api_key / model_name）。"
    exit 1
fi

APP_PORT="${APP_PORT:-}"
if [ -z "$APP_PORT" ] && [ -f .env ]; then
    ENV_PORT="$(grep -E '^APP_PORT=' .env | tail -1 | cut -d= -f2- || true)"
    if [ -n "${ENV_PORT:-}" ]; then
        APP_PORT="$ENV_PORT"
    fi
fi
APP_PORT="${APP_PORT:-8158}"

echo "启动 Docker 栈（构建可能需数分钟）..."
"${COMPOSE[@]}" up -d --build "$@"

echo "等待应用就绪（最长 180s）..."
for _ in $(seq 1 60); do
    CODE="$(curl -k -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${APP_PORT}/app/frontend/login" || true)"
    if [ "$CODE" != "200" ]; then
        CODE="$(curl -k -s -o /dev/null -w '%{http_code}' "https://127.0.0.1:${APP_PORT}/app/frontend/login" || true)"
    fi
    if [ "$CODE" = "200" ]; then
        echo "启动成功: http://127.0.0.1:${APP_PORT}/app/frontend/"
        echo "查看日志: docker compose logs -f app"
        exit 0
    fi
    sleep 3
done

echo "启动超时，最近日志："
docker compose logs --tail 80 app || true
exit 1

#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

#
# 停止 Docker 部署栈（默认保留数据卷；./scripts/docker_stop.sh --volumes 连卷删除）。
# 用法：./scripts/docker_stop.sh [docker compose down 附加参数]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "错误: 未找到 docker CLI"
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "错误: docker compose 不可用"
    exit 1
fi

"${COMPOSE[@]}" down "$@"
echo "已停止。数据卷默认保留（如需清除：./scripts/docker_stop.sh --volumes）"

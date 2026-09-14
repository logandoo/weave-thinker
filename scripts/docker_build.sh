#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

#
# 构建 Weave Thinker 一体化镜像（上下文 = 仓库根）。
# 用法：
#   ./scripts/docker_build.sh                       # 默认 tag weave-thinker:local
#   ./scripts/docker_build.sh weave-thinker:v1      # 自定义 tag
#   ./scripts/docker_build.sh weave-thinker:local --build-arg WITH_BROWSER=1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "错误: 未找到 docker CLI（请先安装 Docker / colima 等运行时）"
    exit 1
fi

TAG="${1:-weave-thinker:local}"
if [ "$#" -gt 0 ]; then
    shift
fi

echo "构建镜像: ${TAG} （上下文: ${PROJECT_DIR}）"
docker build --tag "$TAG" "$@" .
echo "构建完成: $TAG"

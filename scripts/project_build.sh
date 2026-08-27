#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="$PROJECT_DIR/frontend"
BACKEND_STATIC="$PROJECT_DIR/backend/static"

echo "========================================="
echo "  Weave Thinker 前端构建脚本"
echo "========================================="

if [ ! -d "$FRONTEND_DIR" ]; then
    echo "错误: 前端目录不存在: $FRONTEND_DIR"
    exit 1
fi

echo ""
echo "[1/4] 安装前端依赖..."
cd "$FRONTEND_DIR"
npm install

if [ $? -ne 0 ]; then
    echo "错误: npm install 失败"
    exit 1
fi
echo "依赖安装完成"

echo ""
echo "[2/4] 构建前端项目..."
npm run build

if [ $? -ne 0 ]; then
    echo "错误: 前端构建失败"
    exit 1
fi
echo "前端构建完成"

echo ""
echo "[3/4] 清理旧文件..."
rm -rf "$BACKEND_STATIC"
mkdir -p "$BACKEND_STATIC"

echo ""
echo "[4/4] 移动构建产物到后端目录..."
cp -r "$FRONTEND_DIR/dist/"* "$BACKEND_STATIC/"

if [ -f "$BACKEND_STATIC/index.html" ]; then
    echo "构建产物已移动到: $BACKEND_STATIC"
else
    echo "错误: index.html 未找到"
    exit 1
fi

echo ""
echo "========================================="
echo "  构建完成!"
echo "========================================="
echo ""
echo "前端静态文件位置: $BACKEND_STATIC"
echo "访问地址: http://localhost:8158/"
echo ""

#!/usr/bin/env bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

# weave-thinker-deployment / 环境预检
# 用法: bash scripts/preflight.sh [--port 8158]
# 输出 PASS/WARN/FAIL 逐项结果与汇总；存在 FAIL 时退出码 1。
set -u

PORT=8158
while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            if [ $# -lt 2 ]; then echo "错误: --port 需要一个端口号" >&2; exit 2; fi
            PORT="$2"; shift 2 ;;
        -h|--help) echo "用法: bash scripts/preflight.sh [--port 8158]"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done
case "$PORT" in ''|*[!0-9]*) echo "错误: 非法端口 '$PORT'（应为数字）" >&2; exit 2 ;; esac

PASS=0; WARN=0; FAIL=0
ok()   { PASS=$((PASS + 1)); printf '[OK]   %s\n' "$1"; }
warn() { WARN=$((WARN + 1)); printf '[WARN] %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '[FAIL] %s\n' "$1"; }

echo "== weave-thinker preflight =="

# 1. Docker CLI
if command -v docker >/dev/null 2>&1; then
    ok "docker CLI: $(docker --version 2>/dev/null | head -1)"
else
    bad "未找到 docker CLI —— 安装 Docker Engine / Docker Desktop / colima 后重试"
fi

# 2. Docker daemon
if docker info >/dev/null 2>&1; then
    ok "docker daemon 可用"
else
    bad "docker daemon 不可用 —— 启动 docker 服务（systemctl start docker / colima start）"
fi

# 3. Compose
if docker compose version >/dev/null 2>&1; then
    ok "compose: $(docker compose version 2>/dev/null | head -1)"
elif command -v docker-compose >/dev/null 2>&1; then
    ok "compose: $(docker-compose version 2>/dev/null | head -1)"
else
    bad "docker compose / docker-compose 不可用 —— 安装 compose 插件"
fi

# 4. data-root 与剩余空间
if docker info >/dev/null 2>&1; then
    DROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
    [ -n "$DROOT" ] && ok "data-root: $DROOT"
    FREE_KB="$(df -Pk "$DROOT" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [ -n "${FREE_KB:-}" ]; then
        FREE_GB=$((FREE_KB / 1024 / 1024))
        if [ "$FREE_GB" -lt 5 ]; then
            bad "data-root 所在文件系统剩余 ${FREE_GB}G（构建需 ≥5G，默认镜像含 LibreOffice 约 +0.4G；把 data-root 迁到大盘，见 troubleshooting）"
        else
            ok "data-root 剩余 ${FREE_GB}G"
        fi
    fi
    DRIVER="$(docker info --format '{{.Driver}}' 2>/dev/null)"
    if [ -n "$DRIVER" ] && [ "$DRIVER" != "overlay2" ]; then
        warn "storage driver=${DRIVER}（SELinux 主机建议 overlay2；macOS/colima 显示 overlayfs 属正常）"
    fi
fi

# 5. SELinux
if command -v getenforce >/dev/null 2>&1; then
    MODE="$(getenforce 2>/dev/null)"
    if [ "$MODE" = "Enforcing" ]; then
        warn "SELinux Enforcing —— bind mount 配置需 .env 设 WT_MOUNT_OPTS=ro,z（改后重建容器）"
    else
        ok "SELinux: ${MODE:-unknown}"
    fi
else
    ok "SELinux: 未启用或不可用"
fi

# 6. 端口占用
OCC=""
if command -v ss >/dev/null 2>&1; then
    OCC="$(ss -ltn 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p"$" {print; exit}')"
elif command -v lsof >/dev/null 2>&1; then
    OCC="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1)"
fi
if [ -n "$OCC" ]; then
    warn "端口 ${PORT} 已被占用 —— 改 .env 的 APP_PORT，或释放该端口（不要停无关服务）: $(echo "$OCC" | tr -s ' ' | cut -c1-90)"
elif command -v ss >/dev/null 2>&1 || command -v lsof >/dev/null 2>&1; then
    ok "端口 ${PORT} 空闲"
else
    warn "无法检测端口 ${PORT} 占用（缺少 ss/lsof 工具）"
fi

# 7. 资源
if command -v nproc >/dev/null 2>&1; then
    ok "CPU 核数: $(nproc)"
fi
if command -v free >/dev/null 2>&1; then
    MEMG="$(free -g 2>/dev/null | awk 'NR==2 {print $7}')"
    if [ -n "${MEMG:-}" ]; then
        if [ "$MEMG" -lt 4 ]; then warn "可用内存 ${MEMG}G（建议 ≥4G）"; else ok "可用内存 ${MEMG}G"; fi
    fi
elif command -v sysctl >/dev/null 2>&1; then
    MEMB="$(sysctl -n hw.memsize 2>/dev/null)"
    [ -n "${MEMB:-}" ] && ok "内存: $((MEMB / 1073741824))G"
fi

# 8. registry 可达性（镜像源）
for u in https://ghcr.io/v2/ https://registry-1.docker.io/v2/ https://docker.m.daocloud.io/v2/; do
    code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$u" 2>/dev/null)" || code=000
    [ -n "$code" ] || code=000
    case "$code" in
        000) warn "registry 不可达: $u —— 配置 /etc/docker/daemon.json 的 registry-mirrors" ;;
        *)   ok "registry 可达: $u (HTTP $code)" ;;
    esac
done

# 9. LibreOffice（手动部署的 Office 服务端预览依赖；Docker 镜像默认内置，无需本机安装）
if command -v soffice >/dev/null 2>&1; then
    ok "LibreOffice: $(soffice --version 2>/dev/null | head -1 | cut -c1-48)（Office 服务端预览可用）"
else
    warn "未检测到 LibreOffice —— 手动部署时 Office 预览回退浏览器端渲染（Docker 镜像默认内置，无需本机安装；需要服务端高保真预览见 troubleshooting 第 9 节）"
fi

echo "== 汇总: OK=$PASS WARN=$WARN FAIL=$FAIL =="
if [ "$FAIL" -gt 0 ]; then
    echo "存在 FAIL 项，先解决再继续部署。"
    exit 1
fi
echo "预检通过（WARN 项按提示处理）。"

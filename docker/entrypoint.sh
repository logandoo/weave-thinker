#!/bin/bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

#
# Weave Thinker 容器入口：
#   1. 配置引导（挂载缺失时从镜像内模板生成；挂载源是目录则给出明确指引并退出）
#   2. 从 config.toml 读 [server] host/port（WT_SERVER_HOST/WT_SERVER_PORT 可覆盖）
#   3. 自动检测 TLS 证书（backend/key.pem+cert.pem 或 backend/certs/，也可用
#      WT_SSL_KEYFILE/WT_SSL_CERTFILE 显式指定）
#   4. exec 启动 uvicorn（exec 保证 PID 1 信号直通、优雅停机）
set -euo pipefail

APP_DIR=/app/backend
cd "$APP_DIR"

log() { echo "[entrypoint] $*"; }

# ── 1. 配置引导 ────────────────────────────────────────────────────────
if [ -d config.toml ]; then
    log "错误: backend/config.toml 是一个目录——compose 挂载源文件不存在时 Docker 会建目录。"
    log "请执行: rm -rf backend/config.toml && cp backend/config.toml.example backend/config.toml"
    log "并把其中 [database] host 设为 \"db\"（compose 服务名），再重启。"
    exit 1
fi
if [ ! -f config.toml ]; then
    if [ -f config.toml.example ]; then
        log "未发现 config.toml——从模板 + 环境变量生成（仅存活于本容器；重建即丢，生产请挂载配置文件）"
        python /app/docker/bootstrap_config.py \
            --from config.toml.example --out config.toml \
            --db-host "${WT_DB_HOST:-db}" \
            --db-port "${WT_DB_PORT:-5432}" \
            --db-user "${WT_DB_USER:-${POSTGRES_USER:-weavethinker}}" \
            --db-pass "${WT_DB_PASS:-${POSTGRES_PASSWORD:-weavethinker}}" \
            --db-name "${WT_DB_NAME:-${POSTGRES_DB:-weavethinker}}"
    else
        log "错误: config.toml 缺失，且镜像内无 config.toml.example 模板可生成。"
        log "请挂载配置文件后重试。"
        exit 1
    fi
fi

if [ -d config_model.toml ]; then
    log "错误: backend/config_model.toml 是一个目录——compose 挂载源文件不存在时 Docker 会建目录。"
    log "请执行: rm -rf backend/config_model.toml && cp backend/config_model.toml.example backend/config_model.toml 再重启。"
    exit 1
fi
if [ ! -f config_model.toml ] && [ -f config_model.toml.example ]; then
    cp config_model.toml.example config_model.toml
    log "警告: 未发现 config_model.toml——已复制模板。未填写 LLM 端点前界面可登录、对话不可用。"
fi

# ── 2. 读 [server] host/port（env 优先） ───────────────────────────────
read -r CFG_HOST CFG_PORT <<<"$(python - <<'PY'
import tomllib
cfg = tomllib.load(open("config.toml", "rb"))
srv = cfg.get("server", {})
print(srv.get("host", "0.0.0.0"), srv.get("port", 8158))
PY
)"
HOST="${WT_SERVER_HOST:-$CFG_HOST}"
PORT="${WT_SERVER_PORT:-$CFG_PORT}"

# ── 3. TLS 自动检测 ────────────────────────────────────────────────────
SSL_OPTS=()
KEY="${WT_SSL_KEYFILE:-}"
CERT="${WT_SSL_CERTFILE:-}"
if [ -z "$KEY" ] && [ -f /app/backend/key.pem ] && [ -f /app/backend/cert.pem ]; then
    KEY=/app/backend/key.pem
    CERT=/app/backend/cert.pem
fi
if [ -z "$KEY" ] && [ -f /app/backend/certs/key.pem ] && [ -f /app/backend/certs/cert.pem ]; then
    KEY=/app/backend/certs/key.pem
    CERT=/app/backend/certs/cert.pem
fi
if [ -n "$KEY" ] && [ -n "$CERT" ] && [ -f "$KEY" ] && [ -f "$CERT" ]; then
    SSL_OPTS=(--ssl-keyfile "$KEY" --ssl-certfile "$CERT")
    log "TLS: 已启用 (HTTPS)"
else
    log "TLS: 未启用 (HTTP)——生产建议由反向代理终结 TLS，或挂载 key.pem/cert.pem"
fi

# ── 4. 启动 ────────────────────────────────────────────────────────────
log "启动 uvicorn: ${HOST}:${PORT}"
exec python -m uvicorn main:app \
    --host "$HOST" --port "$PORT" \
    --proxy-headers --forwarded-allow-ips "${WT_FORWARDED_ALLOW_IPS:-*}" \
    "${SSL_OPTS[@]}"

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
        # Auto-generated config = fresh self-host deployment: enforce the
        # production guard by default.  A mounted config (local/dev) keeps
        # its own WT_ENV choice; explicit WT_ENV still wins.
        export WT_ENV="${WT_ENV:-production}"
        log "未发现 config.toml——从模板 + 环境变量生成（WT_ENV=${WT_ENV}；生产请挂载配置文件）"
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

# 存在但不可读（典型：SELinux 系统 bind mount 未重打标签）→ 明确报错并给出处置，
# 避免后续读取时抛 Python traceback
for f in config.toml config_model.toml; do
    if [ -e "$f" ] && [ ! -r "$f" ]; then
        log "错误: $f 存在但容器内不可读 (permission denied)。"
        log "常见原因：SELinux（Fedora/RHEL）的 bind mount 未重打标签。"
        log "处置：compose 挂载选项加 :z（.env 设 WT_MOUNT_OPTS=ro,z，改后需重建容器：./scripts/docker_start.sh），"
        log "或在宿主机执行 chcon -t container_file_t <宿主机上的该配置文件路径>。"
        exit 1
    fi
done

# ── 1.5 图片 blob 根（2026-11-09 零损失内存改造，docs/IMAGE_BLOBS.md）──────
# 必须在 uvicorn 之前存在且 0700：写入点建目录、物化读要进目录。named volume
# `image_blobs_data` 首次挂载时会继承镜像内 /app/data/image_blobs 的属主/权限，
# 但卷被外部重建（或 SELinux 重打标签）时这里再兜一次。卷**必须可写**——
# 只读挂载会让落盘失败并退回内联，等于把 OOM 向量原样请回来。
BLOB_ROOT_DIR="/app/data/image_blobs"
if ! mkdir -p "$BLOB_ROOT_DIR" 2>/dev/null; then
    log "错误: 无法创建图片 blob 目录 $BLOB_ROOT_DIR（named volume 是否被挂成只读？SELinux 标签？）"
    exit 1
fi
chmod 700 "$BLOB_ROOT_DIR" 2>/dev/null || log "WARN: $BLOB_ROOT_DIR chmod 700 失败（属主不是当前用户？），继续启动"
if ! touch "$BLOB_ROOT_DIR/.write-probe" 2>/dev/null; then
    log "WARN: $BLOB_ROOT_DIR 不可写——图片将退回内联 base64（内存上界回归），请检查卷挂载选项"
else
    rm -f "$BLOB_ROOT_DIR/.write-probe"
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
# 反向代理信任面（2026-11-11 评审 B Critical）：`--forwarded-allow-ips=*` 让 uvicorn
# 无条件采信客户端自填的 X-Forwarded-For，`request.client.host` 于是变成攻击者可控值
# ——按 IP 的限流可被一个请求头绕过，也能被伪造成任意受害者 IP 做定向锁定。
# 默认**不**启用 proxy headers；确有反代时用 WT_FORWARDED_ALLOW_IPS 显式列出代理地址
# （逗号分隔），不要写 *。
PROXY_OPTS=()
if [ -n "${WT_FORWARDED_ALLOW_IPS:-}" ]; then
    case "${WT_FORWARDED_ALLOW_IPS}" in
        *\**)
            # fail-closed：`*` 等于信任任意来源的伪造头（限流可被绕过、也能被伪造成
            # 别人的 IP 做定向锁定）。与 registration_open 同口径——不安全的写法
            # 落到安全方向，而不是只打一行日志然后照做。
            log "ERROR: WT_FORWARDED_ALLOW_IPS 含 '*'，已忽略（不启用 proxy headers）。" \
                "请填反向代理的实际地址；确需多值用逗号分隔，不要用 *"
            ;;
        *)
            PROXY_OPTS=(--proxy-headers --forwarded-allow-ips "${WT_FORWARDED_ALLOW_IPS}")
            log "代理信任面: --proxy-headers（forwarded-allow-ips=${WT_FORWARDED_ALLOW_IPS}）"
            ;;
    esac
fi
if [ ${#PROXY_OPTS[@]} -eq 0 ]; then
    log "代理信任面: 未启用 --proxy-headers（客户端地址取 socket 对端；有反代时请设 WT_FORWARDED_ALLOW_IPS）"
fi

log "启动 uvicorn: ${HOST}:${PORT}"
# `${arr[@]+"${arr[@]}"}`：bash 3.2（macOS 自带）在 set -u 下展开空数组会报 unbound
exec python -m uvicorn main:app \
    --host "$HOST" --port "$PORT" \
    ${PROXY_OPTS[@]+"${PROXY_OPTS[@]}"} \
    ${SSL_OPTS[@]+"${SSL_OPTS[@]}"}

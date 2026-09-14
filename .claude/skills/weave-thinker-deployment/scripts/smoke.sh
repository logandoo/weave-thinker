#!/usr/bin/env bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

# weave-thinker-deployment / 部署冒烟
# 用法:
#   bash scripts/smoke.sh [BASE_URL]            # /docs 与登录页可达性
#   bash scripts/smoke.sh [BASE_URL] --chat     # 加注册→登录→真实对话（需模型端点已配置）
# BASE_URL 默认 http://127.0.0.1:8158；HTTPS 部署用 https://<host>:8158（curl -k 自动跳过自签校验）。
set -u

BASE="http://127.0.0.1:8158"
CHAT=0
for arg in "$@"; do
    case "$arg" in
        --chat) CHAT=1 ;;
        http://*|https://*) BASE="$arg" ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done
CURL=(curl -sk)

code() { local c; c="$("${CURL[@]}" -o /dev/null -m 10 -w '%{http_code}' "$1" 2>/dev/null)" || c=000; [ -n "$c" ] || c=000; printf '%s' "$c"; }

echo "== weave-thinker smoke: $BASE =="

DOCS="$(code "$BASE/docs")"
if [ "$DOCS" = "200" ]; then echo "[OK]   /docs -> 200"; else echo "[FAIL] /docs -> ${DOCS}（服务未起或端口/证书不对）"; exit 1; fi

LOGIN="$(code "$BASE/app/frontend/login")"
if [ "$LOGIN" = "200" ]; then echo "[OK]   /app/frontend/login -> 200"; else echo "[FAIL] /app/frontend/login -> $LOGIN"; exit 1; fi

if [ "$CHAT" -ne 1 ]; then
    echo "（跳过对话检查；加 --chat 启用）"
    exit 0
fi

USER_NAME="smoke_$(date +%s)"
REG="$("${CURL[@]}" -o /dev/null -m 15 -w '%{http_code}' -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' -d "{\"username\":\"$USER_NAME\",\"password\":\"123456\"}")"
case "$REG" in 200|201) echo "[OK]   register -> $REG ($USER_NAME)";; *) echo "[FAIL] register -> $REG"; exit 1;; esac

TOKEN="$("${CURL[@]}" -m 15 -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"$USER_NAME\",\"password\":\"123456\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
if [ -n "$TOKEN" ]; then echo "[OK]   login -> token"; else echo "[FAIL] login 未取得 token"; exit 1; fi

AID="$("${CURL[@]}" -m 15 "$BASE/api/assistants" -H "Authorization: Bearer $TOKEN" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])' 2>/dev/null)"
if [ -n "$AID" ]; then echo "[OK]   assistants -> $AID"; else echo "[FAIL] assistants 列表为空"; exit 1; fi

OUT="$(mktemp)"
"${CURL[@]}" -N -m 240 -X POST "$BASE/api/chat/stream" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"assistant_id\":\"$AID\",\"messages\":[{\"role\":\"user\",\"content\":\"只回答两个字：收到\"}],\"enable_reasoning\":false}" > "$OUT" 2>/dev/null
if grep -q '"done": true' "$OUT" || grep -q '"done":true' "$OUT"; then
    echo "[OK]   chat -> done:true（响应 $(wc -c < "$OUT") 字节；原始流: ${OUT}）"
else
    echo "[FAIL] chat 未完成（原始流: ${OUT}；常见原因：config_model.toml 未配 LLM 端点或端点不可达）"
    exit 1
fi

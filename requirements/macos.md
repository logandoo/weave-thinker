<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# macOS 部署指南

适用：macOS 13+（Apple Silicon / Intel 皆可），Homebrew 2.x。

## 1. 系统依赖（Homebrew）

```bash
# 数据库 + 运行时 + 证书工具
brew install postgresql@16 python@3.13 node@22 openssl

# 可选：
#   ffmpeg（语音音轨处理/导出）        brew install ffmpeg
#   PDF 导出（weasyprint 系统库）      brew install pango cairo gdk-pixbuf
#   （CJK 字体 macOS 系统自带 PingFang，无需另装；本发行不含 backend/Fonts/）
#   Android SDK（构建 webview-app APK） Xcode Command Line Tools +
#   Android Studio（sdkmanager 装 platform-tools & build-tools）

brew services start postgresql@16        # 本机开发：开机自启
pg_config --version                       # 验证：≥ 16
```

> `python@3.13` / `node@22` 是 keg-only，若 `python3`/`node` 指向系统版本，临时追加：
> `export PATH="$(brew --prefix python@3.13)/bin:$(brew --prefix node@22)/bin:$PATH"`

## 2. 数据库初始化

> 复用已有远端 PostgreSQL 时跳过本节，`[database]` 填远端 host/port/账号即可。

```bash
psql -U postgres -h 127.0.0.1 <<'SQL'
CREATE USER weavethinker WITH PASSWORD 'CHANGE_ME_strong_password';
CREATE DATABASE weavethinker OWNER weavethinker ENCODING 'UTF8';
SQL
# 可选：向量扩展（记忆 v2）
psql -U postgres -h 127.0.0.1 -d weavethinker -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

> Homebrew 的 postgres 不接受 `sudo -u postgres`；直接用
> `psql -U postgres -h 127.0.0.1`（本机 trust）。**上线前把 CHANGE_ME 改强口令**。

## 3. 代码与 Python 环境（pip 依赖）

```bash
git clone <your-fork-url> weave-thinker && cd weave-thinker

python3.13 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r backend/requirements.txt     # 31 条直接声明（31 个包，2 个带 extras），完整表见 dependencies.md
```

> 网络受限时可换国内镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend/requirements.txt`
> 依赖画像为纯 permissive（详见 ../docs/LICENSE-COMPLIANCE.md）。

## 4. 配置

```bash
cp backend/config.toml.example backend/config.toml
cp backend/config_model.toml.example backend/config_model.toml
```

`backend/config.toml` 必改：

```toml
[server]
host = "0.0.0.0"
port = 8158

[security]
jwt_secret_key = "<openssl rand -hex 32 生成的长串>"

[database]
host = "127.0.0.1"
port = 5432
username = "weavethinker"
password = "<上面的强口令>"
name = "weavethinker"
```

`backend/config_model.toml` 至少填一个 LLM provider（OpenAI 兼容格式）：

```toml
[providers.your-llm]
type = "openai"
base_url = "https://<api-host>/v1"          # 云端 API 或本地 vLLM/Ollama 的 /v1
api_key = "<YOUR-SECRET>"
model_name = "<model>"
priority = "1"
```

（语音 ASR/TTS、embedding/rerank 等可选段，不填只影响对应功能。）

## 5. TLS 证书（自签）

```bash
cd backend
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=weavethinker.local" \
  -addext "subjectAltName=DNS:localhost,DNS:weavethinker.local,IP:127.0.0.1"
cd ..
```

> `./scripts/start.sh` 检测到 `backend/key.pem` + `cert.pem` 即自动走 HTTPS；
> 内网/多机访问时把 SAN 改成你的域名或服务器 IP，客户端（浏览器/Android 壳）信任该证书。

## 6. 构建前端（npm 依赖）并启动

```bash
./scripts/project_build.sh    # 内部 = cd frontend && npm install && npm run build → backend/static/ (锁文件随包, 与 ubuntu/windows 及脚本实际一致)
./scripts/start.sh            # nohup + PID 文件；日志 chatllm.log
./scripts/status.sh
curl -k https://127.0.0.1:8158/docs      # 200 = OK
```

浏览器打开 `https://127.0.0.1:8158/app/frontend/`（自签证书点「继续访问」），
注册第一个账号（首用户即管理员；E2E 约定使用 `test`/`123456` 账号）→
新建助手（选你的 provider）→ 开始对话。

## 7. 日常运维

```bash
./scripts/stop.sh && ./scripts/restart.sh
tail -f chatllm.log
pg_dump -U postgres -h 127.0.0.1 -Fc weavethinker > backup_$(date +%F).dump
```

## 8. 可选：Android 壳

```bash
# 需要 Android SDK + JDK 11+
ANDROID_HOME=~/Library/Android/sdk ./scripts/apk_generate.sh \
  "https://<你的局域网 IP 或域名>:8158"
# 产物 apk/weave-thinker.apk（自签证书在壳内全局信任）
```

## 9. 可选：E2E 自验

```bash
cd frontend
npx playwright install chromium
npx playwright test e2e/chat.spec.ts --config playwright.prod8158.config.ts
# 服务端 agent 浏览器工具（Python 侧；与 npm 侧同为 1.60.0、chromium revision 一致，装一次即可）：
.venv/bin/python -m playwright install chromium
```

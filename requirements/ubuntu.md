<!-- Copyright (c) 2026 Weave Thinker Contributors -->

<!-- SPDX-License-Identifier: Apache-2.0 -->

# Ubuntu / Debian 部署指南（生产推荐）

适用：Ubuntu 22.04 / 24.04 LTS、Debian 12。以 `sudo` 用户操作。

## 1. 系统依赖（apt）

```bash
sudo apt update
# 经典仓库版本即可（PG14/16、Python3.10/3.12、Node18/20）；
# 22.04 的 Python 3.10 / Node 18 满足最低要求。
# ⚠ 复用远端 PostgreSQL 时：从下列移除 postgresql，并跳过其后
#   `sudo service postgresql start` / `pg_lsclusters`（第 2 节整节跳过）。
sudo apt install -y postgresql python3 python3-venv python3-pip \
                    nodejs npm openssl build-essential
# Ubuntu 22.04 若需更高版本的 Node（18→20/22）：
# ⚠ 若 `node -v` 仍是 12.x（第三方 apt 镜像源所致），必须走下面的升级，
#   前端构建需 Node ≥18：
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node -v                                     # 验收 ≥ v18

# 可选：
#   ffmpeg（语音/导出）   sudo apt install -y ffmpeg
#   pgvector（24.04: postgresql-16-pgvector；22.04: 需 pgdg apt 源）
#   PDF 导出（weasyprint 系统库）   sudo apt install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0
#     （PDF 需内嵌图片时加：libgdk-pixbuf2.0-0【22.04】/ libgdk-pixbuf-2.0-0【24.04】）
#   CJK 字体（本发行不含 backend/Fonts/，Linux 上 PDF/代码输出中文防方块）
#     sudo apt install -y fonts-noto-cjk
sudo service postgresql start
pg_lsclusters                               # 确认集群 online
```

> 更高 PG 版本：PostgreSQL 官方 apt 源（apt.postgresql.org）。
> 服务器公网部署时请同时配置云厂商安全组：只放行 8158（TCP）并终结于 TLS；
> 5432 不要对公网开放（本机用法：`-h 127.0.0.1`）。
>
> **apt Node 坑（第三方源镜像）**：若箱内 apt 源是第三方镜像，`apt install nodejs`
> 可能装到 12.x（前端构建需 ≥18）——务必按上面的 NodeSource 方式核对
> `node -v`。NodeSource 安装若报 `dpkg-deb: ... paste subprocess was killed (Broken pipe)` / 文件冲突（`libnode-dev` 等旧 universe node 包残留），先
> `apt-get remove -y libnode-dev node-stdlib node-gyp` 再装，或
> `dpkg -i --force-overwrite` 单包修复。
>
> **端口"可通"评估口径**：telnet/nc 握手成 ≠ 数据面可通——部分云厂商边缘对
> 未映射端口做 TCP 被动应答（握手成、零数据）。部署前可用 telnet 快速筛端口，
> 但部署后必须以真实 HTTP 响应验收：`curl -k https://<公网IP>:8158/docs` 返回
> 200（本手册各步骤的验收点同理）。若该端口被云边吞数据而既有 80/443 等端口
> 正常，走下文第 7 层 Nginx 反代（注意云边通常只转发"既有映射"端口，新建监听
> 端口大概率同样不通）。
>
> **排查阶梯（部署实测）**：
>
> 1. 箱内自检：`ss -ltn | grep 8158` 有监听 +
>    `curl -k https://127.0.0.1:8158/docs` 200 → 应用层就绪，问题在边缘。
> 2. WAN 实测：`curl -k https://<公网IP>:8158/docs`；同时用一个**非 TLS**
>    请求（`curl http://<公网IP>:8158/`）区分"静默吞包"（000）与"连接被
>    拒绝/重置"（W/400）。对照机同环境 8158 立即可达 → 边缘行为是**逐箱/逐端口**的映射配置，
>    不是通用规律。
> 3. 云控制台放行安全组后复测：若 >5 分钟仍 000，
>    说明生效层不在安全组——依次怀疑：EIP/NAT 的 DNAT/端口映射表（很多
>    VPS 供应商把"端口开放"放在映射层而不是安全组）、规则方向/协议/端口
>    写错、或多层防火墙（云安全组 + 供应商网关）。与供应商确认"哪个面板
>    控制该公网 IP 的入站端口"，直接要一个 8158 的映射。
> 4. 兜底：供应商只给既定映射端口时，选一个空闲映射端口（先按第 1-2 步
>    确认其数据面），nginx 反代到本机 8158（第 7 节模板），并记录进运维
>    备注。
>
> **nginx 反代 + IP 直访坑（部署实测）**：浏览器/curl 对**纯 IP**
> 目标不发 TLS SNI → nginx 的 443 虚拟主机按 SNI 路由对 IP 访问**永远
> 落到 default_server**，`server_name=<箱IP>` 的 443 块不会命中。要保
> 既有域名站点又不改 443 default_server，只能给反代**独立监听端口**
> （该端口必须本身有 WAN 数据面，见排查阶梯）；或接受成为 default
> （影响既有 IP 直访者，谨慎）。80 端口按 Host 头路由无此坑。

## 2. 数据库初始化

> 复用远端 PostgreSQL 时跳过本节，`[database]` 填远端 host/port/账号即可
> （部署前先用 `nc -zv <db-host> 5432` 确认服务器到 DB 的网络可达）。

```bash
sudo -u postgres psql <<'SQL'
CREATE USER weavethinker WITH PASSWORD 'CHANGE_ME_strong_password';
CREATE DATABASE weavethinker OWNER weavethinker ENCODING 'UTF8';
SQL
sudo -u postgres psql -d weavethinker -c "CREATE EXTENSION IF NOT EXISTS vector;"  # 可选
psql "postgresql://weavethinker:CHANGE_ME_strong_password@127.0.0.1:5432/weavethinker" -c "select 1"
```

## 3. 代码与 Python 环境（pip 依赖）

建议部署目录 `/opt/weave-thinker`（下文沿用 `<ROOT>`）：

> 生产建议独立账号（下文 useradd 流程）；验证/小部署以 root 直接跑全流程
> 亦可行（部署实例即以 root 执行，脚本行为一致），代价是
> 配置与运行时目录权限放宽。

```bash
sudo useradd -r -m -d /opt/weave-thinker weavethinker 2>/dev/null || true
sudo -u weavethinker git clone <your-fork-url> /opt/weave-thinker
cd /opt/weave-thinker
sudo -u weavethinker python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r backend/requirements.txt     # 31 条直接声明（31 个包，2 个带 extras），完整表见 dependencies.md
```

> 依赖画像为纯 permissive（详见 ../docs/LICENSE-COMPLIANCE.md）。

## 4. 配置

```bash
cp backend/config.toml.example backend/config.toml
cp backend/config_model.toml.example backend/config_model.toml
```

`backend/config.toml`（权限 600，属主 weavethinker）：

```toml
[server]
host = "0.0.0.0"
port = 8158

[security]
jwt_secret_key = "<openssl rand -hex 32>"

[database]
host = "127.0.0.1"
port = 5432
username = "weavethinker"
password = "<强口令>"
name = "weavethinker"
```

`backend/config_model.toml`：至少一个 LLM provider（格式同
[macos.md](macos.md) 第 4 节；语音/embedding 可选）。

```bash
chmod 600 backend/config.toml backend/config_model.toml
chown -R weavethinker:weavethinker backend/config.toml backend/config_model.toml
```

## 5. TLS 证书

```bash
cd backend
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=<your-domain-or-ip>" \
  -addext "subjectAltName=IP:<server-ip>,DNS:<your-domain>"
```

有域名建议直接上 Let's Encrypt（certbot），自签证书仅适合内网。
公网 IP 直访场景：CN 与 `subjectAltName` 用公网 IP（本文模板的
`IP:<server-ip>` 已含），浏览器/Android 壳按提示信任自签证书；自动化
验证器用 `curl -k` / Playwright `ignoreHTTPSErrors`。

## 6. 构建前端（npm 依赖）

```bash
./scripts/project_build.sh     # 内部 = cd frontend && npm install && npm run build → backend/static/
                               # 小内存机器（<8GB）跑 npm 构建前先设
                               # NODE_OPTIONS=--max-old-space-size=2048
                               # （Node 堆受 commit 上限约束，小内存机实测）
```

## 7. 启动

### 方式 A：项目自带脚本（nohup + PID 文件，适合验证/小部署）

```bash
./scripts/start.sh && ./scripts/status.sh
```

### 方式 B：systemd（生产推荐）

```ini
# /etc/systemd/system/weavethinker.service
[Unit]
Description=Weave Thinker
After=network.target postgresql.service

[Service]
User=weavethinker
WorkingDirectory=/opt/weave-thinker/backend
ExecStart=/opt/weave-thinker/.venv/bin/python -m uvicorn main:app \
  --host 127.0.0.1 --port 8158 \
  --ssl-keyfile /opt/weave-thinker/backend/key.pem \
  --ssl-certfile /opt/weave-thinker/backend/cert.pem
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now weavethinker
journalctl -u weavethinker -f
curl -k https://127.0.0.1:8158/docs    # 200 = OK
```

公网访问建议 Nginx 反代终结 TLS：

```nginx
server {
    listen 443 ssl;
    server_name wt.example.com;
    ssl_certificate     /etc/letsencrypt/live/wt.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/wt.example.com/privkey.pem;
    location / {
        proxy_pass https://127.0.0.1:8158;
        proxy_ssl_verify off;            # 后端自签证书
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;     # SSE/WebSocket
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;                    # 长任务 SSE
        proxy_buffering off;                          # SSE 必须关缓冲
    }
}
```

## 8. 数据与备份

- 数据库：`pg_dump -Fc`（全量）+ 每日 cron；`weavethinker` 角色建议仅库内权限
- 运行时文件：`user_workspaces/`（用户工作区/上传）、`backend/agent_memories/`
  （文件记忆）、`backend/output_files/`、`backend/audio_files/` —— 备份时与 dump 一并打包
- 密钥：`backend/config*.toml`（600）、`key.pem`、JWT secret —— 走密钥管理，不进普通备份/版本库

## 9. E2E / 服务端浏览器工具（可选）

```bash
# 前端 E2E（npm 侧 playwright，版本随 package.json/package-lock 精确锁定的 @playwright/test）：
cd frontend && npx playwright install chromium --with-deps   # headless 服务器加 --with-deps 装系统库
npx playwright test e2e/chat.spec.ts --config playwright.prod8158.config.ts

# 服务端 agent「网页深读/浏览器 10 件套」（Python 侧 playwright，版本随 requirements.txt；
# 与 npm 侧同为 1.60.0、chromium revision 一致，浏览器缓存共享，装一次即可双侧复用）：
.venv/bin/python -m playwright install chromium --with-deps
```

> 两侧 playwright 精确锁定同一版本（npm `1.60.0` / pip `==1.60.0`），chromium 构建 revision
> 相同、共用同一浏览器缓存目录——任一侧 `install chromium` 一次即同时满足 E2E 与服务端
> 浏览器工具。都不装则仅 agent 浏览器能力族与 E2E 不可用，站内其余
> 功能正常（未安装时服务与页面验证不受影响）。

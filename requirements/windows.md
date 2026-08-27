<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Windows 部署指南

> **生产/长期运行推荐 Linux（见 [ubuntu.md](ubuntu.md)）或 WSL2**——后端脚本
> （`scripts/*.sh`）是 bash 脚本，在 Windows 上依赖 **Git for Windows / WSL /
> MSYS2 bash**（推荐 WSL2：Windows 上跑完整 Linux 体验，WSL2 内按 ubuntu.md
> 操作即可，本文只列原生 Windows 的差异项）。

适用：Windows 10/11 本地开发 / 小规模局域网部署；**Windows Server 2019/2022
生产部署**可用本文原生流程（无 WSL2 环境时），差异项见下文各节标注。

**Server 版两个已知环境特性（2026-08-27 部署实证）：**

- **无 winget**：Server 2019 不带 App Installer → 下面第 1 节按"直装 MSI/EXE"
  分支操作（官方安装包或国内 npmmirror 二进制源均可）。
- **Windows Update + Defender 会阶段性吃满 commit**（4GB 小内存尤甚）：期间
  PowerShell 报 `Thread failed to start` / `0x800705af`、WinRM 报
  `paging file is too small` 属暂时态——等更新完成自动恢复，或授权维护窗口
  重启（本次 4GB Server 2019 更新期 npm/V8 连续 OOM，重启后即恢复）；此期间
  一切操作改走 **cmd 通道**（`cmd /c` 的 8191 字符行缓冲是硬上限，批量传
  数据按 <8000 字符分块；`python -c "带引号代码"` 的 WSMan-cmd 组合引用
  不可靠，数据经 `append.py` 式 argv 文件小工具传递更稳）。
- **远程管理通道（无桌面 Server）**：OpenSSH Server 默认未装；RDP/SMB 常被
  云边拦截。**WinRM 5986(https) 是默认可用的远程命令通道**——注意 5985
  (http) 监听存在但可能因客户端认证配置而 401，https 端点通常可用
  （pywinrm `transport="ssl"` / PowerShell `New-PSSession` 同理）；5985/5986
  之外的公网端口若可放行，用本文 8158 流程。

## 1. 系统依赖（winget / 直装）

```powershell
winget install PostgreSQL.PostgreSQL.16        # 安装时勾选默认组件，记住 superuser 口令
winget install Python.Python.3.13              # 勾选 Add to PATH
winget install OpenJS.NodeJS.LTS               # 18+（LTS 20/22 推荐）
winget install Git.Git
# 可选:  BtbN.FFmpeg (ffmpeg)
# PDF 导出：原生 Windows 需 weasyprint 系统 DLL（pango/harfbuzz/gdk-pixbuf，
# 见 weasyprint 官方文档；WSL2 直接按 ubuntu.md 的 apt 包安装）
node -v; python --version; psql --version      # 三个版本号都验证一下
```

> **Server 2019 / 无 winget 分支（直装）**：
>
> ```powershell
> # Node 22 MSI（国内网络用 npmmirror 二进制源，官方源把域名换成
> # https://nodejs.org/dist/v22.x.x/node-v22.x.x-x64.msi）：
> curl.exe -L -o C:\wt\node.msi https://registry.npmmirror.com/-/binary/node/v22.17.0/node-v22.17.0-x64.msi
> msiexec /i C:\wt\node.msi /qn /norestart
> # Git for Windows（Inno 安装器，静默参数 /VERYSILENT；自带 bash.exe 与
> # C:\Program Files\Git\usr\bin\openssl.exe（3.x，支持 -addext）——
> # 不要装 GnuWin32.OpenSSL（1.0.x 已 EOL）：
> curl.exe -L -o C:\wt\git.exe https://registry.npmmirror.com/-/binary/git-for-windows/v2.47.1.windows.1/Git-2.47.1-64-bit.exe
> C:\wt\git.exe /VERYSILENT /NORESTART /NOCHECK
> # 装完刷新当前会话 PATH 再验证（新开窗口/会话自动生效）：
> set "PATH=C:\Program Files\nodejs;C:\Program Files\Git\cmd;C:\Program Files\Git\usr\bin;%PATH%"
> node -v & git --version & openssl version
> ```

PostgreSQL 安装后默认监听 `127.0.0.1:5432`，Windows 服务自启（`services.msc`
里 `postgresql-x64-16`）。需要 pgvector 时，从官方
<https://github.com/pgvector/pgvector> Releases 解压到 PG 安装目录
（`bin`/`lib`/`share/postgresql/extension`）后 `CREATE EXTENSION vector;`。

## 2. 数据库初始化

> 复用远端 PostgreSQL 时跳过本节，`[database]` 填远端 host/port/账号即可
> （部署前先在服务器上 `Test-NetConnection <db-host> -Port 5432` 确认可达）。

```powershell
psql -U postgres -h 127.0.0.1   # 输安装时的 superuser 口令
```
```sql
CREATE USER weavethinker WITH PASSWORD 'CHANGE_ME_strong_password';
CREATE DATABASE weavethinker OWNER weavethinker ENCODING 'UTF8';
-- 可选：
CREATE EXTENSION IF NOT EXISTS vector;
```

## 3. 代码与 Python 环境（pip 依赖）

```powershell
git clone <your-fork-url> D:\tools\weave-thinker
cd D:\tools\weave-thinker
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install -r backend\requirements.txt
```

> 国内网络：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt`
> 个别 wheel 需要 MSVC 运行时（winget 的 Python 一般自带分发即可）。
>
> 部署实例三条实证（2026-08-27 Server 2019 远程部署）：
> 1. **Server 镜像常只有 C: 盘**——没有 D 盘时全文改 `C:\tools\weave-thinker`
>    （本文示例的 D:\ 主要面向桌面 Win10/11）。
> 2. 云厂商/托管镜像**可能已预装 Python**（3.10+ 即可复用，实例复用预装
>    3.11，跳过 winget Python）。
> 3. 机器出网受限或无 git 仓库时，先按 README 第 0 步的"源码包分发"把源码
>    落到部署目录（实例经远程管理通道分块推送 tar 包后 `tar` 解包），再执行
>    本节第 4 步之后。

## 4. 配置

```powershell
copy backend\config.toml.example backend\config.toml
copy backend\config_model.toml.example backend\config_model.toml
```

编辑 `backend\config.toml`：

```toml
[server]
host = "0.0.0.0"
port = 8158

[security]
jwt_secret_key = "<openssl rand -hex 32 的输出>"

[database]
host = "127.0.0.1"
port = 5432
username = "weavethinker"
password = "<强口令>"
name = "weavethinker"
```

`backend\config_model.toml`：至少一个 LLM provider（格式见
[macos.md](macos.md) 第 4 节）。

> Windows 防火墙：有桌面的机器首次监听 8158 会弹提示，选"网络共享及协作"
> （Private networks）；**无头 Server（WinRM 远程部署）不会弹窗，必须显式加规则**：
>
> ```powershell
> netsh advfirewall firewall add rule name="WeaveThinker-8158" dir=in action=allow protocol=TCP localport=8158
> ```
>
> 公网部署不建议直接暴露 Windows 主机（如需，加 Nginx/反代终结 TLS 同 ubuntu.md）。
> 部署前端口评估同 ubuntu.md 口径：telnet/nc 握手成 ≠ 数据面可通（云边可能
> 被动应答），上线后以 `curl -k https://<host>:8158/docs` 返回 200 验收。

## 5. TLS 证书（自签）

> openssl 来源：§1 直装分支的 Git for Windows 自带
> `C:\Program Files\Git\usr\bin\openssl.exe`（3.x，支持 `-addext`），确保其在 PATH。

```powershell
cd backend
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem `
  -days 3650 -nodes -subj "/CN=<your-domain-or-ip>" `
  -addext "subjectAltName=IP:<server-ip>,DNS:<your-domain>"
```

## 6. 构建前端（npm 依赖）并启动

`scripts/*.sh` 需要 bash（Git for Windows 自带 `bash.exe`；或装 WSL/Git-Bash）：

```bash
bash scripts/project_build.sh     # 内部 = cd frontend && npm install && npm run build → backend/static/
bash scripts/start.sh             # nohup + PID（chatllm.pid / chatllm.log）
bash scripts/status.sh
bash scripts/stop.sh
```

> 小内存机器（<8GB，如 4GB 的 Server 2019）`npm install`/vite 构建易吃满
> commit 报 V8 `JavaScript heap out of memory`——构建前
> `set "NODE_OPTIONS=--max-old-space-size=2048"`（bash 内为
> `export NODE_OPTIONS=...`），并留意 Windows Update/Defender 扫描期
> commit 被阶段性占用（此时也不能并发重进程）。

纯 PowerShell 启动（等效于 start.sh，不需要 bash）：

```powershell
cd backend
Start-Process -NoNewWindow -FilePath "..\.venv\Scripts\python.exe" -ArgumentList `
  "-m","uvicorn","main:app","--host","0.0.0.0","--port","8158",
  "--ssl-keyfile","key.pem","--ssl-certfile","cert.pem"
# 停止：建议用 bash scripts/stop.sh（PID 文件精确停止，不要模式杀进程）
```

浏览器打开 `https://<host>:8158/app/frontend/`（自签证书点"高级→继续"）。

## 7. 开机自启（可选，Task Scheduler）

"任务计划程序" → 创建基本任务 → 触发器"启动时" → 操作
`<bash 路径> -l -c "d:/tools/weave-thinker/scripts/start.sh"`（Git-Bash 路径按实际）。
生产请优先考虑迁移到 Linux / WSL2。

## 8. 备份

```powershell
pg_dump -U postgres -Fc weavethinker -f backup_$(Get-Date -Format yyyyMMdd).dump
# 另需包含：user_workspaces\、backend\agent_memories\、backend\config*.toml、key.pem
```

## 9. E2E

```powershell
cd frontend
npx playwright install chromium
npx playwright test e2e\chat.spec.ts --config playwright.prod8158.config.ts
cd ..
.venv\Scripts\python -m playwright install chromium   # 服务端 agent 浏览器工具（Python 侧）
```

---
name: weave-thinker-deployment
description: "部署、升级与排障 Weave Thinker 自托管实例。Deploy, upgrade, and troubleshoot a self-hosted Weave Thinker instance with Docker or bare metal. TRIGGER when：用户要求部署/安装/启动/升级 weave-thinker（Docker 或手动）；Docker 构建失败或极慢；容器起不来或重启循环；SELinux permission denied / 容器读不到挂载配置；国内网络 GitHub / registry / apt / pip / npm 不可达或极慢；数据库或模型端点配置问题；Office 预览不可用或图表不完整（LibreOffice 缺失/被关闭）；部署后的冒烟验证。Covers preflight environment checks, mirror-accelerated builds, SELinux mount handling, config bootstrap, smoke tests, and a troubleshooting index. NOT for：开发功能或修改代码（走仓库常规开发流程）、纯前端调试。"
license: Apache-2.0
compatibility: Requires Docker (or compatible runtime) and network access to container registries; Linux/macOS/WSL2. Helper scripts need bash, curl, and python3.
---

# Weave Thinker 部署（weave-thinker-deployment）

把 weave-thinker 部署到一台机器上：先预检环境，再走官方 Docker 路径（或手动路径），配置两份 TOML，最后跑冒烟验证。全程优先使用仓库自带脚本。

## 何时用

- 在一台新机器上部署 weave-thinker（Docker 或手动）。
- 已有部署需要升级或排障（构建慢、容器起不来、配置读不到、网络受限）。
- 需要给部署做一次可复现的验收（冒烟）。

## 步骤 0 — 环境预检（必做）

```bash
bash scripts/preflight.sh              # 默认检查 8158 端口
bash scripts/preflight.sh --port 18158 # 端口冲突时改
```

FAIL 必须先解决（缺 Docker、daemon 不可用、compose 缺失）；WARN 按提示处理（磁盘、SELinux、registry 可达性）。预检覆盖：Docker CLI/daemon/compose、data-root 与剩余空间、SELinux 模式、端口占用、CPU/内存、registry 可达性。

## 步骤 1 — 取得代码

- 能访问 GitHub：`git clone <仓库地址> weave-thinker && cd weave-thinker`
- 不能访问（国内常见）：源码包分发。在有网机器克隆后打包，传到目标机解压，并核对关键文件校验值：

```bash
export COPYFILE_DISABLE=1          # macOS：避免 AppleDouble ._* 污染
tar -czf wt-src.tgz .
# → 传输 → mkdir weave-thinker && tar xzf wt-src.tgz -C weave-thinker && cd weave-thinker
```

第三方 GitHub 代理可用性不稳定且内容不可信，不要用它替代校验。

## 步骤 2 — 配置（两份 TOML + .env）

```bash
cp backend/config.toml.example       backend/config.toml
cp backend/config_model.toml.example backend/config_model.toml
cp docker/.env.example               .env
```

> 路径以发行树为准：`config.toml.example` 由发行构建生成（主仓开发树可能没有），`requirements/` 在发行树根；主仓开发树对应 `tools/openextras/requirements/`。

必改：

- `backend/config.toml`：`[database]` 的 host（Docker 部署填 `db`）/username/password/name 与 `.env` 的 `POSTGRES_*` 一致；`[security] jwt_secret_key` 填随机长串（`openssl rand -hex 32`）。
- `backend/config_model.toml`：至少一个 LLM 端点（`[endpoints.main]` 的 base_url/api_key/model_name）；自托管推理服务填其可被容器访问的地址（宿主机服务用宿主 LAN IP，不要用 127.0.0.1）。
- `.env`：`POSTGRES_PASSWORD`（与 config.toml 一致）；端口冲突改 `APP_PORT`。

Office 服务端预览：Docker 镜像默认内置 LibreOffice（Excel/Word/PPT 转 PDF 渲染，表格与图表完整）；不需要可在 `.env` 设 `WITH_LIBREOFFICE=0` 重建精简（此时 Office 预览自动回退浏览器端渲染，复杂图表可能不完整）。

受限网络构建加速（可选，留空 = 官方源；改后重建）：

```bash
NPM_REGISTRY=https://registry.npmmirror.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
APT_MIRROR=mirrors.tuna.tsinghua.edu.cn      # 只填主机名，不带 http(s)://
```

SELinux 系统（Fedora/RHEL/CentOS）：`.env` 设 `WT_MOUNT_OPTS=ro,z`。挂载选项在容器创建时生效，改后必须重建容器（`docker restart` 不生效）。

## 步骤 3 — 部署

Docker（推荐）：

```bash
./scripts/docker_start.sh     # 构建 + 后台启动 + 等待健康
./scripts/docker_stop.sh      # 停止（--volumes 连数据卷删除）
./scripts/docker_build.sh     # 只构建镜像（可透传 --build-arg）
```

手动部署：分平台文档 `requirements/{macos,ubuntu,windows}.md`（发行树；主仓在 `tools/openextras/requirements/`）与 `docs/USER_MANUAL.md` §1.2；构建 `./scripts/project_build.sh`，启停 `./scripts/start.sh` / `stop.sh` / `restart.sh`。Office 服务端预览为可选依赖：按平台文档安装 LibreOffice（不装则自动回退浏览器端渲染）。

## 步骤 4 — 冒烟验证（必做）

```bash
bash scripts/smoke.sh http://127.0.0.1:8158           # 文档与登录页可达性
bash scripts/smoke.sh http://127.0.0.1:8158 --chat    # 加注册→登录→真实对话
```

预期：`/docs` 200、`/app/frontend/login` 200；`--chat` 模式拿到模型回复且 `done:true`。失败先看 `docker compose logs app`（或手动部署的 `chatllm.log`），再查排障索引。

## 步骤 5 — 排障

按症状查 [references/troubleshooting.md](references/troubleshooting.md)：构建极慢或失败、容器重启循环、配置不可读（SELinux）、磁盘打满（data-root / containerd）、端口冲突、模型端点不通、记忆子系统降级（embedding 占位）、Office 预览不可用或图表不完整（LibreOffice 缺失/被关闭）。

## 升级 / 备份 / 回滚

- 升级：先备份 → 拉新代码 → `./scripts/docker_start.sh`（启动迁移幂等）。
- 备份：`docker compose exec -T db pg_dump -U <user> -d <db> -Fc > backup.dump`，加上命名卷数据（工作区、记忆）。
- 回滚：`docker compose down` 后切回旧版本重建；全新部署的回滚即 `docker compose down -v`（连数据清空）。

## 约束

- 不动目标机上既有的其他服务；端口冲突改 `APP_PORT`，不要停别人的进程。
- 密钥（`config*.toml` / `.env`）不写入镜像、不提交仓库；生产建议由反向代理终结 TLS。
- 升级或重建前先备份数据库与运行时目录。

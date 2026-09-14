<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 排障索引（troubleshooting）

按症状查找。每条给「现象 → 根因 → 处置」。

## 1. 构建极慢或失败

**现象**：`docker build` / `docker_start.sh` 构建 30 分钟以上，或 apt/pip/npm 超时。
**根因**：上游源（deb.debian.org、PyPI、npmjs）在受限网络下延迟极高或不可达。
**处置**：`.env` 设构建期镜像后重建（留空 = 官方源）：

```bash
NPM_REGISTRY=https://registry.npmmirror.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
APT_MIRROR=mirrors.tuna.tsinghua.edu.cn      # 只填主机名，不带 http(s)://
```

分步定位（看构建日志里各 step 的耗时）：`npm ci` 慢 → 换 NPM_REGISTRY；apt 慢 → 换 APT_MIRROR；pip 慢 → 换 PIP_INDEX_URL（阿里云 PyPI 不一定更快，瓶颈可能是本机出口带宽——可换镜像对比测速）。基础镜像拉取慢 → 配 registry 加速（见第 5 节）。

## 2. 容器起不来 / 重启循环

**现象**：`docker compose ps` 显示 `Restarting`；`docker compose logs app` 有报错。
**处置**：先读日志首条错误，再对号入座：

- `config.toml 是一个目录`：compose 挂载源文件不存在时 Docker 会建目录。执行
  `rm -rf backend/config.toml && cp backend/config.toml.example backend/config.toml`（model 文件同理）后重启。
- `存在但容器内不可读 (permission denied)`：SELinux 未重打标签，见第 3 节。
- 数据库连接失败：`.env` 的 `POSTGRES_*` 与 `backend/config.toml [database]` 的 username/password/name 不一致，或 host 没填 `db`。
- `config_model.toml` 未挂载：入口会复制模板并警告；未配 LLM 端点时界面可登录、对话不可用。

## 3. SELinux（Fedora / RHEL / CentOS）

**现象 A**：容器内 `exec permission denied`，连 `python -c` 都跑不了。
**根因**：containerd 快照存储与 SELinux 组合未正确打标签。
**处置**：切回经典 overlay2 存储（`/etc/docker/daemon.json`）：

```json
{ "features": { "containerd-snapshotter": false } }
```

重启 docker；确认 `docker info | grep "Storage Driver"` 为 `overlay2`。

**现象 B**：容器读不到挂载的配置文件（`permission denied` 或日志显示文件缺失后生成模板又失败）。
**根因**：home 目录文件是 `user_home_t`，容器进程无权限。
**处置**：`.env` 设 `WT_MOUNT_OPTS=ro,z` 并**重建容器**（`docker restart` 不应用挂载选项）；或在宿主机执行
`chcon -t container_file_t <配置文件路径>`。注意：`:z` 的重打标签是持久的，`restorecon` 不会覆盖它；
要复现原始失败需 `chcon -t user_home_t <文件>` 手动复位。

## 4. 磁盘打满（ENOSPC）

**现象**：构建报 `no space left on device`；系统盘（如 15G）被打满。
**根因**：Docker 默认把镜像/容器数据放系统盘；Docker 29 默认 containerd 镜像存储把快照写
`/var/lib/containerd`（也在系统盘）。
**处置**：把存储迁到大盘：

```json
// /etc/docker/daemon.json
{ "data-root": "/data/docker" }
```

containerd 镜像存储（若使用）还需 `/etc/containerd/config.toml` 的 `root = "/data/containerd"`。
停 docker → 迁移/清理旧数据 → 起 docker；迁移后如遇 `parent snapshot does not exist`，删除
`<data-root>/buildkit` 后冷重建。

## 5. 网络：GitHub / 镜像仓库不可达

**现象**：`git clone` 超时；`docker pull` 报 `dial tcp ... connection refused` 或超时。
**处置**：

- GitHub 不可达 → 源码包分发（克隆后 tar → 传输 → 解压），核对关键文件校验值；不要依赖第三方代理。
- docker.io 不可达 → `/etc/docker/daemon.json` 配 `registry-mirrors`（如 `https://docker.m.daocloud.io`、
  `https://docker.1ms.run`）后重启 docker。注意不同 registry 可达性可能不同（如 ghcr.io 可达而 docker.io 不通）。

## 6. 端口冲突

**现象**：`docker_start.sh` 起不来或健康等待超时；宿主机 8158 已被其他服务占用。
**处置**：`.env` 改 `APP_PORT=18158` 等空闲端口；不要停无关进程（如本机推理服务）。预检脚本会自动提示占用。

## 7. 模型端点不通（界面能开、对话报错）

**现象**：对话报缺配或连接失败；日志显示端点探测失败。
**根因**：`config_model.toml` 未配端点，或地址在容器内不可达。
**处置**：至少配置 `[endpoints.main]`（base_url/api_key/model_name）；容器内 `127.0.0.1` 指向容器自身——
宿主上的推理服务（llama.cpp / vLLM 等）要用宿主 LAN IP，或给 compose 加
`extra_hosts: ["host.docker.internal:host-gateway"]` 后用该主机名。确认推理服务监听 `0.0.0.0`。

## 8. 记忆子系统降级（预期行为）

**现象**：日志有 `embedding provider probe failed` 之类 ERROR，但服务正常。
**根因**：未配置 `[endpoints.embedding]`（模板占位地址）。
**处置**：不配置即降级运行（v2 记忆向量检索不可用）；需要时配置 embedding 端点后重启。

## 冒烟失败时的定位顺序

1. `bash scripts/smoke.sh <BASE_URL>` 先确认 `/docs` 与登录页（200 才算服务在）。
2. `docker compose logs --tail 100 app`（手动部署看 `chatllm.log`）找首条错误。
3. `docker compose ps` 看健康状态；`docker compose exec db pg_isready` 看数据库。
4. `--chat` 失败单独看第 7 节（模型端点），与页面可达性是两个问题。

<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Weave Thinker — 分平台部署与依赖

| 平台 | 文档 |
|---|---|
| macOS | [macos.md](macos.md) |
| Ubuntu / Debian | [ubuntu.md](ubuntu.md) |
| Windows（含 Server 2019/2022）/ WSL2 | [windows.md](windows.md) |
| 依赖清单（系统/pip/npm + license 摘要） | [dependencies.md](dependencies.md) |

通用最简集：**Python ≥ 3.10（推荐 3.12/3.13）· Node.js ≥ 18（推荐 20/22）·
PostgreSQL ≥ 14**，可选 Android SDK（APK 壳）、Playwright Chromium（E2E + 服务端浏览器工具，
npm/Python 双侧各装一次）、ffmpeg。
依赖 license 合规详见根目录 [LICENSE-COMPLIANCE.md](../docs/LICENSE-COMPLIANCE.md)。

每个平台文档均含：系统依赖安装 → 数据库初始化 → 配置（两个 TOML 模板）→
自签证书 → 前端构建（`./scripts/project_build.sh`）→ 启动（`./scripts/start.sh`）
→ 验证（`/docs` + 浏览器 `https://<host>:8158/app/frontend/`）。

两条跨平台铁律（各平台文档内的注记是同一事实的分平台展开）：
- **数据面验收**：公网/VPS 场景 telnet 握手成 ≠ 可访问（云边可能被动
  应答）——验收必须 `curl -k https://<host>:8158/docs` 返回 200，
  不通时按 ubuntu.md「排查阶梯」（安全组 → NAT/端口映射层 → 供应商）
  逐级定位，勿在错误层反复操作。
- **复用远端 PostgreSQL**：各平台「数据库初始化」节均可整节跳过
  （含其 apt/winget 安装项），`[database]` 指向远端即可；共享非空库的
  两条注意事项见根 README「部署流程」末段。

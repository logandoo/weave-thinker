<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# CONTRIBUTING.md — 贡献指南

欢迎给 Weave Thinker 贡献代码、文档与测试。本文档是**完整的 DCO/CLA 流程**：
个人贡献者按 DCO sign-off 提交即可；公司/企业贡献需另行签署 CCLA（联系 **罗淦
<logandoo@126.com>**）。

**本项目设计上可被 fork，欢迎企业内部分支定制。** 你的分支不需要合回主干，
我们只要求上游贡献走下面的流程。

---

## 1. 我能贡献什么？

- 新功能 / 缺陷修复（后端 `backend/`、前端 `frontend/`、Android 壳 `webview-app/`）
- 文档（`docs/API.md`、`requirements/` 分平台部署指南）
- 测试（`frontend/e2e/` Playwright E2E）
- 新系统技能（`backend/skills/<name>/SKILL.md`，见 README「架构速览」）
- 依赖/安全问题的 issue 与 PR

## 2. 开发环境

按 [requirements/](requirements/) 下对应平台文档搭建（macOS / Ubuntu / Windows-WSL2），
关键点：Python ≥ 3.10（推荐 3.12/3.13）+ Node ≥ 18（推荐 20/22）+ PostgreSQL ≥ 14，
构建与启停一律走 `scripts/`（`project_build.sh` / `start.sh` / `stop.sh` / `restart.sh`），
不要裸跑 `npm run build` 或 `uvicorn`。

## 3. 提交约定（所有贡献者必读）

### 3.1 DCO — Developer Certificate of Origin

Weave Thinker 采用 **DCO（开发者来源证书）** 机制确认贡献许可，不要求个人贡献者
签署单独的 CLA。DCO 声明全文如下：

> By making a contribution to this project, you certify that:
>
> a) The contribution was created in whole or in part by you and you have the
>    right to submit it under the open source license indicated in the file; or
> b) The contribution is based upon previous work that, to the best of your
>    knowledge, is covered under an appropriate open source license and you
>    have the right under that license to submit that work with modifications,
>    whether in original form or modified; or
> c) The contribution was provided directly to you by some other person who
>    certified (a), (b) or (c) and you have not modified it.
> d) You understand and agree that this project and the contribution are
>    public and that a record of the contribution (including all personal
>    information you submit with it, e.g. full name or email address) is
>    maintained indefinitely and may be redistributed consistent with this
>    project or the license(s) involved.
>
> （中文：通过向本项目作出贡献，您声明：(a) 该贡献由您全部或部分创建，且您有权
> 按文件中标明的开源许可证提交；或 (b) 该贡献基于您知悉的、受适当开源许可覆盖
> 的既有工作，且您有权按该许可提交（含修改）；或 (c) 该贡献由其他已作上述
> 声明者直接提供给您，且您未作修改。您理解并同意本项目与贡献均为公开，贡献
> 记录（包括您提交的姓名/邮箱等个人信息）将被永久保留并按本项目或相关许可
> 重新分发。）

### 3.2 个人贡献：如何 sign-off

每个 commit 都要带 `Signed-off-by` 行。使用 `git commit -s`（或 `--signoff`）
自动追加，`-s` 会用你的 `user.name` / `user.email`：

```bash
# 一次性配置（建议 email 使用你的公开邮箱/GitHub noreply 邮箱）
git config --global user.name  "你的名字"
git config --global user.email "you@example.com"

# 提交时签名
git commit -s -m "fix: 修复 xxx"

# 生成的提交信息形如：
# fix: 修复 xxx
#
# Signed-off-by: 你的名字 <you@example.com>
```

也可以让常用命令自动带 sign-off（任选其一）：

```bash
git config --global format.signoff true          # git format-patch 场景
# 或 shell 别名：  alias gc='git commit -s'
```

PR 中**每一个** commit 都必须有合法的 sign-off（文本中 `Signed-off-by:` 的姓名/邮箱
与该 commit 作者一致）。漏签的 commit 可用 `git rebase -i --signoff <base>` 批量补签，
然后 force-push 自己的 PR 分支（仅限本人 PR，勿做共享/主干操作）。

### 3.3 公司/企业贡献：CCLA

以公司名义贡献（代码由员工在公司环境下产出、版权归公司所有）时：

1. **先联系 罗淦 <logandoo@126.com>** 索取 CCLA（Corporate Contributor License
   Agreement，公司贡献者许可协议）文本；
2. 由**公司法定代表人/授权代表**签署 CCLA 并回传（扫描件 + 签署人姓名/职务/
   公司全称 + 授权范围记录，我们会建档）；
3. CCLA 覆盖生效后，该公司员工对本项目的贡献只需按 3.2 做 DCO sign-off，
   无需逐人再签；
4. 尚未签署 CCLA 的公司代码**请勿直接提交**到 PR——维护者会先核对许可链，
   缺失许可的 commit 无法被合并。

> 为什么公司走 CCLA 而个人走 DCO？DCO 是对"提交者有权按 Apache-2.0 提交"的
> 个人声明，成本低；公司代码的既有权归属更复杂（雇主条款/多个作者/历史库），
> 需要一纸具名协议把授权链条闭合。

## 4. PR 流程

1. fork 本仓库（或切企业内部分支）→ 建特性分支（`feat/xxx`、`fix/xxx`、`docs/xxx`）
2. 保持小步提交；commit message 用祈使句，说清楚**为什么**（bug 附复现路径）
3. 每个 commit 带 DCO sign-off（见 3.2）
4. 本地自验（下列"测试要求"全过）后再开 PR，PR 描述里贴测试证据（日志/截图/
   验证命令输出）
5. 维护者审查 → 反馈 → 修订（rebase 到主干，勿 merge 主干入特性分支）→ 合并

### 测试要求（NO TEST, NO MERGE）

- **后端改动**：本发行不含后端单测集，PR 需附等价的可运行验证脚本与输出
  （`scripts/restart.sh` 后 curl 相关 API 做端到端验证，或跑前端 E2E 覆盖）；
  涉及 API 面的改动同步重新生成 `docs/API.md`（字段表由 OpenAPI 生成，见「生成说明」）
- **前端改动**：涉及 UI 的行为变更用 Playwright 对 **8158 生产构建**验证
  （`./scripts/project_build.sh` 重建后 `./scripts/restart.sh`，然后
  `cd frontend && npx playwright test e2e/<your-spec>.ts --config playwright.prod8158.config.ts`），
  不要在代码里留下绕过验证的措施
- **新增依赖**：必须说明理由（PR 描述）；临时引入后请运行依赖合规自查
  （`pip-licenses --from=mixed --fail-on="GNU AFFERO GPL"` 与 npm license 核查，
  方法见 docs/LICENSE-COMPLIANCE.md「维护建议」），🔴 强 copyleft 依赖默认不合入

## 5. 代码与风格约定

- 后端：FastAPI 路由在 `app/api/`、业务在 `app/services/`、Pydantic schema 在
  `app/schemas/`；配置一律来自两个 TOML（`config.toml` / `config_model.toml`），
  **禁止在代码里硬编码密钥/主机/端口**；数据库 schema 变更走
  `app/db/migrations.py` 的 `STARTUP_MIGRATIONS`（幂等 `ALTER ... IF NOT EXISTS`）
- 前端：Vue 3 Composition API + `<script setup lang="ts">`；UI 颜色一律走皮肤
  设计令牌（`docs/SKINS.md`），**不要引入硬编码 hex**；文本为中文（与现有 UI
  一致）
- 文件格式：新增源码文件请带统一 license 头（下节）
- 提交前自查：`git diff --stat` 里不应出现 `backend/config.toml`、`*.pem`、
  `user_workspaces/`、运行时目录等——它们已 gitignore，若误改请还原

## 6. 版本管理

- **语义化版本（SemVer）**：当前 `v0.0.1` = 首个公开版。0.x 阶段配置/API
  允许破坏性变更、不承诺升级平滑；进入 1.0 时冻结配置与 API（破坏性变更
  → MAJOR）。
- **版本号三处字面量（发布前必须一致）**：
  - `frontend/package.json` 的 `version`，与 `frontend/package-lock.json`
    根节点（顶部 `""` 条目）两处同步
  - `backend/main.py` 的 `FastAPI(title="Weave Thinker API", version=…)`
    ——`/docs` 与 `/openapi.json` 直接展示给部署者
  - `webview-app/app/build.gradle` 的 `versionName`
- **验证命令**（发布前执行，三处输出须同号）：
  ```bash
  grep '"version"' frontend/package.json frontend/package-lock.json | head -2
  grep 'version="' backend/main.py
  grep versionName webview-app/app/build.gradle
  ```
- **发布动作**：tag 一律 `v<X.Y.Z>`；Release notes 直接采用
  `backend/agent_memories/func.md` 第 6 章「版本更新记录」的产品视角增量
  （该章按「与上一版差异」维护，即现成素材）。
- 不维护独立 CHANGELOG.md（避免与 Release body 双账本）；UI 不显示版本号
  （支持排障以 `/docs` 展示值为准）。

## 7. License 头（所有新增/修改的源码文件）

本仓库统一使用 Apache-2.0 短许可头（SPDX 风格，两行）；完整许可文本见根
[LICENSE](LICENSE)。

```
# <语言注释符> Copyright (c) 2026 Weave Thinker Contributors
# <语言注释符> SPDX-License-Identifier: Apache-2.0
```

- Python/Shell/TOML/CSS(块注释除外)/配置 用 `#`（shebang 开头的脚本，头置于 shebang 之后）
- JS/TS/Java/Gradle 用 `//`
- Vue SFC / HTML / XML / Markdown 用 `<!-- ... -->`
- JSON 不支持注释，无需加头（由根 LICENSE 覆盖）
- 从外部项目复制代码：保留原版权与许可声明，并在源文件头注明出处；
  🔴 强 copyleft（GPL/AGPL）代码不得直接复制进本仓库

## 8. 行为准则

- 对事不对人；技术分歧以代码/证据/可运行演示说话
- 不提供任何形式的歧视、骚扰或人身攻击
- 上报安全漏洞请**直接发邮件**给 罗淦 <logandoo@126.com>，不要开公开 issue

## 9. 联系表

| 事项 | 联系人 |
|---|---|
| 公司 CCLA 签署 / 企业定制合作 | 罗淦 <logandoo@126.com> |
| 安全漏洞 | 罗淦 <logandoo@126.com>（邮件） |
| 一般贡献咨询 | 仓库 issue（中文/英文均可） |

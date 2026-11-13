<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# UPSTREAM — vibeweaver 系统技能溯源

- 来源：<https://github.com/logandoo/vibeweaver>（MIT，见 LICENSE / NOTICE）。
- 本波上游锚：`<upstream-commit>`
  （2026-11-07 10:24:52 +08:00，`chore(audit): 收口波 — 证据重封（231 files）+ 完工门记录（assert 35/35）`）；
  镜像源 `the upstream vibeweaver repository`。本波以旧锚
  `<upstream-commit>` 做三方归并，统一技能文本与 canonical 脚本版本。
- 本目录为**系统内置技能**（`[agent.skills] force_inject` 注入；资源经
  `Config.resolve_resource` 解析），对 `skill_view` / `skill_run_script` 只读；
  `{VW_DIR}` 占位由装载层替换为本目录绝对路径。

## 包含

- `SKILL.md`（上游 + 宿主 COV-0：完成仪式只写当前 `tests/verification_log.md`
  任务块，不进入用户可见最终回答）+ 13 伴读：
  COMPLETION_GATE / AUDIT_PLUGIN / TESTING_PROTOCOLS / REFERENCE /
  WORKFLOWS_EXTENDED / VERIFICATION_UPGRADES / MULTI_AGENT / ENGINEERING_STD /
  CODING_PRINCIPLES / APPENDIX / MEMORY_RULES / MEMORY_TEMPLATES /
  SCRIPT_TEMPLATES。`APPENDIX.md` 的 §A2 已随上游改为指向
  `SCRIPT_TEMPLATES.md`，不保留旧的双份脚本模板。
- `bootstrap.md` 与 `profiles/core/SKILL.md`：随包提供短上下文入口，**不自动启用**；
  D9 off|auto|always 与 full force-inject 语义不变。
- 产品运行时所需的 canonical scripts：checker + sha256 pin、mm_probe、
  scan_secrets、evidence_lock/seal、ui_fidelity、backlog_check、
  record_evidence、make_review_package、repair_checker、state_sanitize、
  contract/token checks，以及 W25 控制面 `run_control.py`、`integrate.py`、
  `resource_allocator.py`；它们与 MULTI_AGENT 的 RunContext/lease/integration/resource
  合约随包提供，而非仅有不可执行的规格文本。
- `scripts/probe_vision.png`（保留宿主既有路径）与 `scripts/tests/` 上游探针夹具。

## COV-0 宿主差异（唯一的行为性文档差异）

上游 COV-0 允许在 profile 声明 `final_gate_line: yes` 时把 gate line 放进最终
回答；宿主禁止任何工程仪式进入用户可见最终回答。为保持可校验性，宿主把
`[Verification Gate]`、完成表和 Convergence/Coverage/Memory Gate 固定写入
当前 `tests/verification_log.md` 任务块，产品 audit 从该块读取；自然语言最终
回答只给结果与证据摘要。详见 `the maintainer vibeweaver guide`。

## 不进入产品 runtime 的上游资产

- OpenCode 插件（`vibeweaver-{gate,audit}.js`）、installer、loader/兼容测试：
  由 `the upstream vibeweaver repository` 独立安装和管理。本仓不复制、
  不同步、不修改 `~/.config/opencode`；产品侧由 `app/services/vibeweaver_*.py`
  执法。双方只共享 checker/规格语义，不共享安装生命周期。
- 上游 `scripts/audit_selftest.mjs`、`mutation_sweep.mjs`、`perf_budget.py`、
  `vibeweaver-audit-core.js`、`scripts/ab/*` 等 dev/eval lab 文件**已从宿主包移除**：
  它们依赖上游根部的插件/installer 或 `tests/gate_trust` 夹具，不是 Weave Thinker
  产品运行时；需要运行时应回源仓，不能把残缺副本冒充产品 smoke。
- release/claims 清单、eval 实验室、上游 tests/memory/docs/state、`.github`
  与生成日志不属于运行时 payload；上游自检直接在源仓库执行，避免把发布 lab
  变成第二套产品机制。`README.md`、`CHANGELOG.md`、`script/linux/*` 也不随包。
- `NOTICE` 中提到的 `scripts/license_check.py` 属于上游 release 资产；本嵌入
  包未包含它，不应把 NOTICE 当作本仓 license 扫描已执行的证明。

## 校验与升级

<!-- checker-pin-machine-readable:begin (the maintainer sync script rewrites these two lines; they are the machine truth for the pins) -->
checker-pin-bundled: e16cb0692dd940321ed0dcc4026b198238450abefe2d244147615a7f47695be7
checker-pin-repo-root: e16cb0692dd940321ed0dcc4026b198238450abefe2d244147615a7f47695be7
<!-- checker-pin-machine-readable:end -->
- **checker pin 三口径（W38 handoff T6 澄清，2026-11-06）**：
  | 口径 | 值 | 含义 |
  |---|---|---|
  | 上游源仓锚点（历史波） | `<upstream-commit>` | 技能文本锚 `<upstream-commit>`（2026-10-29 W22）当时的上游 checker digest，仅作溯源，不再具有约束力 |
  | 随包实际（**运行时真值**） | `e16cb069…` | `backend/skills/vibeweaver/scripts/assert_artifacts.py` + `scripts/assert_artifacts.sha256`；产品门禁经 `resolve_resource(bundled_first=True)` 永远执行这份（防 workspace 篡改，见 `the maintainer vibeweaver guide`） |
  | 仓根开发副本 | `e16cb069…` | 仓根 `tests/assert_artifacts.py`，与随包副本必须逐字节一致 |
  三者关系与消解：随包实际 ≡ 仓根副本 ≡ `the maintainer regression suite::CHECKER_SHA256`，由该测试强制单裁判；历史锚 `<upstream-commit>` 与已废弃的中间副本 `<upstream-commit>` 只出现在本表与 git 历史。升级通道：`bash the maintainer sync script`（单一真源生成器，见下）。
- **代际注记（2026-11-08）**：技能文本与运行时 payload 已同步至 `<upstream-commit>`；checker 仍为 canonical `e16cb069…`。保留的宿主差异为：① `SKILL.md` / `COMPLETION_GATE.md` / `AUDIT_PLUGIN.md` / `profiles/core/SKILL.md` 的 COV-0 完成仪式落盘语义（COV-9 同样不得切回用户回答）；② `VERIFICATION_UPGRADES.md` 明确 A/B lab 仅在上游源仓、不随包；③ 下述 `contract_lint.py` 52KB 上限 patch；④ `repair_checker.sh` 优先定位随包 canonical，而非默认写向 OpenCode；⑤ `NOTICE` 的宿主未携带 license_check 注记。W23 graduated gate、任务快照代际与 W25 run/attempt/candidate 证据绑定随上游更新，`backlog_check.py` 的 lease/outbox/control-db 面也已同步。
- **宿主安全修复（需回上游，不可被下次同步覆盖）**：`evidence_seal.py --check` 在提供 RunContext/SHA map 时逐一比对 run/attempt/spec/candidate/final 绑定；`evidence_lock.py --verify-archive` 复用同一 run-binding 拒绝规则。另含 W25 控制面修复：普通 claim 拒绝 TTL-only/未隔离接管；ResourceAllocator 以 task_id + 当前 attempt/epoch 做跨 attempt fencing；IntegrationQueue 遇 placeholder review 强制 ok=False 且不可 publish。全部由 `the maintainer regression suite` 的行为回归钉住；根目录 evidence 脚本副本保持逐字节一致。
- **宿主 patch（同步时必须重放，生成器自动处理）**：`scripts/contract_lint.py` 的 SKILL.md 上限 49000→52000（W38 handoff T7 / D-20261106-w38-t7，宿主 COV-0 段占用体积，2 字节余量是假红陷阱）；对应测试断言在 `test_vibeweaver_update_20261031.py` 同步为 52_000。
- `scripts/scan_secrets.py` 路径继续保留以维持 `skill_view` 与打包契约；
  上游已恢复并强化该脚本，旧注记“上游已移出”作废。
- 升级时先三方比较宿主 COV-0 / UPSTREAM / probe_vision.png，再同步上述文件；
  禁止覆盖本地门控、工作区边界、持久 pendingResume 与安全改写。

<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# UPSTREAM — vibeweaver 系统技能溯源

- 来源：<https://github.com/logandoo/vibeweaver>（MIT，见 LICENSE / NOTICE）。
- 本波上游锚：`3568be61a55a7c2b228a6313024a0e80950b8ba6`
  （2026-10-29 20:40:13 +08:00，`fix(W22): writeGateResult compact JSON`）；
  镜像源 `~/Documents/DEV/SKILLS/vibeweaver`。此前为 2026-10-08 起的
  混合快照，本波统一合约与 canonical 脚本版本。
- 本目录为**系统内置技能**（`[agent.skills] force_inject` 注入；资源经
  `Config.resolve_resource` 解析），对 `skill_view` / `skill_run_script` 只读；
  `{VW_DIR}` 占位由装载层替换为本目录绝对路径。

## 包含

- `SKILL.md`（上游 + 宿主 COV-0：完成仪式只写当前 `tests/verification_log.md`
  任务块，不进入用户可见最终回答）+ 11 伴读：
  COMPLETION_GATE / AUDIT_PLUGIN / TESTING_PROTOCOLS / REFERENCE /
  WORKFLOWS_EXTENDED / VERIFICATION_UPGRADES / ENGINEERING_STD /
  CODING_PRINCIPLES / APPENDIX / MEMORY_RULES / MEMORY_TEMPLATES。
- `bootstrap.md` 与 `profiles/core/SKILL.md`：随包提供短上下文入口，**不自动启用**；
  D9 off|auto|always 与 full force-inject 语义不变。
- 产品运行时所需的 canonical scripts：checker + sha256 pin、mm_probe、
  scan_secrets、evidence_lock/seal、ui_fidelity、backlog_check、
  record_evidence、make_review_package、repair_checker、state_sanitize、
  contract/token checks。
- `scripts/probe_vision.png`（保留宿主既有路径）与 `scripts/tests/` 上游探针夹具。

## COV-0 宿主差异（唯一的行为性文档差异）

上游 COV-0 允许在 profile 声明 `final_gate_line: yes` 时把 gate line 放进最终
回答；宿主禁止任何工程仪式进入用户可见最终回答。为保持可校验性，宿主把
`[Verification Gate]`、完成表和 Convergence/Coverage/Memory Gate 固定写入
当前 `tests/verification_log.md` 任务块，产品 audit 从该块读取；自然语言最终
回答只给结果与证据摘要。详见 `docs/VIBEAWEAVER.md`。

## 不进入产品 runtime 的上游资产

- OpenCode 插件（`vibeweaver-{gate,audit}.js`）、installer、loader/兼容测试：
  由 `~/Documents/DEV/SKILLS/vibeweaver` 独立安装和管理。本仓不复制、
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

- checker pin：`5001f8165412ba1ec5110608c9afa0a3dcf8f15f6a953e83e2d9518806ac5fe3`。
- `scripts/scan_secrets.py` 路径继续保留以维持 `skill_view` 与打包契约；
  上游已恢复并强化该脚本，旧注记“上游已移出”作废。
- 升级时先三方比较宿主 COV-0 / UPSTREAM / probe_vision.png，再同步上述文件；
  禁止覆盖本地门控、工作区边界、持久 pendingResume 与安全改写。

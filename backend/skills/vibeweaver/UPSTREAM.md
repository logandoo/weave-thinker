<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# UPSTREAM — vibeweaver 系统技能溯源

- 来源：<https://github.com/logandoo/vibeweaver>（MIT，见 LICENSE）
- 镜像自本机 `~/.config/opencode/skills/vibeweaver` / `~/Documents/DEV/SKILLS/vibeweaver`，
  **2026-10-08 波**（SKILL.md sha256 `2434bb8d…`；含 COV-13 / §V11 任务分级与
  文档渲染门禁、W7–W11 运行经验条目）。本目录为**系统内置技能**
  （`[agent.skills] force_inject` 注入；资源经 `Config.resolve_resource` 解析）。
- 包含：SKILL.md + 10 伴读（COMPLETION_GATE / TESTING_PROTOCOLS / REFERENCE /
  WORKFLOWS_EXTENDED / VERIFICATION_UPGRADES / ENGINEERING_STD / CODING_PRINCIPLES /
  APPENDIX / MEMORY_RULES / MEMORY_TEMPLATES）+ scripts/{assert_artifacts,mm_probe,
  scan_secrets}.py + scripts/probe_vision.png（COV-5 探针样图）。
- **不**包含（宿主为 OpenCode 专属或运行态）：vibeweaver-gate.js / vibeweaver-audit.js /
  vibeweaver-audit-core.js / audit_selftest.mjs / mutation_sweep.mjs（OpenCode 插件与其
  自测架；Weave Thinker 侧由 app/services/vibeweaver_gate_service.py /
  vibeweaver_audit_service.py 移植其语义，行为以 scripts/assert_artifacts.py 与共用
  fixture 对齐）、install.sh/install.bat、memory/、tests/。
- `scripts/scan_secrets.py` 为历史镜像件（上游已移出该脚本；保留以维持既有
  测试引用 `skill_view path=scripts/scan_secrets.py` 与打包契约）。
- 升级：从上游重新镜像上述文件并更新本注记；`skill_view`/`skill_run_script`
  对本目录只读（系统技能不可被 skill_manage 改写）。
- 文本内 `{VW_DIR}` 占位 = 本目录绝对路径，由 skill_view / force-inject 注入时替换。

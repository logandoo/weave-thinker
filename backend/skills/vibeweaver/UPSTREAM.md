<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# UPSTREAM — vibeweaver 系统技能溯源

- 来源：<https://github.com/logandoo/vibeweaver>（MIT，见 LICENSE）
- 镜像自本机 `~/.config/opencode/skills/vibeweaver`，2026-10-01 波次（SKILL.md
  尺寸预算 <49KB 世代，含 COV-13 / §V11 任务分级与文档渲染门禁）。
- 包含：SKILL.md + 10 伴读（COMPLETION_GATE / TESTING_PROTOCOLS / REFERENCE /
  WORKFLOWS_EXTENDED / VERIFICATION_UPGRADES / ENGINEERING_STD / CODING_PRINCIPLES /
  APPENDIX / MEMORY_RULES / MEMORY_TEMPLATES）+ scripts/{assert_artifacts,mm_probe,
  scan_secrets}.py + scripts/probe_vision.png（COV-5 探针样图）。
- **不**包含（宿主为 OpenCode 专属或运行态）：vibeweaver-gate.js /
  vibeweaver-audit.js（OpenCode 插件；Weave Thinker 侧由
  app/services/vibeweaver_gate_service.py / vibeweaver_audit_service.py
  移植其语义，行为以 scripts/assert_artifacts.py 与共用 fixture 对齐）、
  install.sh/install.bat、memory/（skill 仓运行态）、tests/（skill 自测架）。
- 升级：从上游重新镜像上述文件并更新本注记；`skill_view`/`skill_run_script`
  对本目录只读（系统技能不可被 skill_manage 改写）。
- 文本内 `{VW_DIR}` 占位 = 本目录绝对路径，由 skill_view / force-inject 注入时替换。

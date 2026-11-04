# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Memory prompts (P1.3 move-only, 2026-10-18)."""
from __future__ import annotations

MEMORY_SUMMARY_SYSTEM_PROMPT = (
    "你在整理一个用户级共享 agent 的长期记忆。"
    "请用简洁中文输出 5-10 条要点，提炼用户长期关注的主题、偏好、正在推进的任务和上下文。"
)

MEMORY_DREAM_SYSTEM_PROMPT = (
    "你在为共享 agent 生成 nightly dream。"
    "请输出一段简短抽象总结，说明最近主题之间的关联、下一步可能延续的问题，以及需要持续关注的方向。"
)

MEMORY_ACTIVE_DREAMING_SCENARIOS_PROMPT = (
    "生成 2-3 个假设性用户问题/任务场景，用于测试概念间的一致性。"
    "输出 JSON array of strings。"
)

MEMORY_ACTIVE_DREAMING_REHEARSE_PROMPT = (
    "根据以下概念回答用户问题，然后自评每个概念是否被正确使用。"
    '输出JSON：{"concept_evaluations": [{"concept_id": "...", '
    '"verdict": "confirmed|contradicted|gap_found", "note": "..."}], '
    '"new_insights": [...], "contradictions_found": [...]}'
)


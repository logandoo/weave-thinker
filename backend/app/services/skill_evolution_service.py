# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills"


class SkillEvolutionService:
    def __init__(self):
        self._skills_dir = _SKILL_DIR

    def should_suggest_skill(self, tool_call_count: int, iteration_count: int) -> bool:
        cfg = config.agent_skill_evolution
        if not cfg.get("enabled", True):
            return False
        threshold = int(cfg.get("auto_suggest_threshold", 5))
        return tool_call_count >= threshold

    def build_skill_suggestion_prompt(self, task_summary: str, tool_calls: List[str]) -> str:
        tool_list = ", ".join(tool_calls) if tool_calls else "N/A"
        return (
            f"你刚刚完成了一个较复杂的任务（使用了 {len(tool_calls)} 次工具调用：{tool_list}）。\n"
            f"任务摘要: {task_summary}\n\n"
            f"如果你认为这个任务模式值得保存为可复用的技能，请调用 skill_manage(action='create', ...) 来创建技能。\n"
            f"技能应包含：名称、适用场景描述、执行步骤和注意事项。"
        )

    async def assess_skill_quality(self, skill_content: str) -> Dict[str, Any]:
        """LLM rubric assessment of skill content quality (agentic principle
        — the former keyword scoring could not judge arbitrary content).
        LLM failure → documented fallback: needs_improvement with empty
        specifics (the repair budget is bounded by config)."""
        from app.services.agentic_judge import judge_json

        parsed = await judge_json(
            "你是技能质量评估员。评估一份技能的 Markdown 内容，从以下维度打分（0-100）：\n"
            "1. 完整性：是否包含名称、适用场景、执行步骤、注意事项\n"
            "2. 可执行性：步骤是否具体可执行、有无歧义\n"
            "3. 规范性：结构清晰、格式一致\n"
            "输出JSON：\n"
            '{"score": 0-100, "level": "excellent|good|needs_improvement|poor", '
            '"issues": ["具体问题1"], "suggestions": ["改进建议1"]}\n'
            "score>=80 → excellent；>=60 → good；>=40 → needs_improvement；否则 poor。\n"
            "只输出JSON。",
            f"技能内容：\n{(skill_content or '')[:4000]}\n\n只输出JSON。",
            task="skill_assess",
            default=None,

            timeout=25.0,
        )
        if not isinstance(parsed, dict):
            logger.info("skill quality assessment LLM unavailable — needs_improvement fallback")
            return {
                "score": 40,
                "level": "needs_improvement",
                "issues": [],
                "suggestions": [],
            }
        try:
            score = max(0, min(100, int(parsed.get("score", 40))))
        except (TypeError, ValueError):
            score = 40
        level = str(parsed.get("level") or "").strip()
        if level not in ("excellent", "good", "needs_improvement", "poor"):
            level = (
                "excellent" if score >= 80
                else "good" if score >= 60
                else "needs_improvement" if score >= 40
                else "poor"
            )
        issues = parsed.get("issues")
        suggestions = parsed.get("suggestions")
        return {
            "score": score,
            "level": level,
            "issues": [str(i) for i in issues][:6] if isinstance(issues, list) else [],
            "suggestions": [str(s) for s in suggestions][:6] if isinstance(suggestions, list) else [],
        }

    async def try_auto_repair_skill(self, skill_name: str, skill_content: str, failure_reason: str) -> Optional[str]:
        cfg = config.agent_skill_evolution
        if not cfg.get("enabled", True):
            return None

        quality = await self.assess_skill_quality(skill_content)
        if quality["level"] in ("poor", "needs_improvement"):
            logger.info("Skill '%s' quality assessment: %s (score=%d), needs repair",
                        skill_name, quality["level"], quality["score"])

            repair_prompt = (
                f"技能 '{skill_name}' 在执行时遇到问题：{failure_reason}\n\n"
                f"当前技能内容:\n{skill_content}\n\n"
                f"质量问题: {json.dumps(quality['issues'], ensure_ascii=False)}\n"
                f"改进建议: {json.dumps(quality['suggestions'], ensure_ascii=False)}\n\n"
                f"请生成改进后的技能内容。"
            )
            return repair_prompt

        return None

    def get_skill_stats(self) -> Dict[str, Any]:
        skills = []
        if self._skills_dir.exists():
            for p in self._skills_dir.rglob("*.md"):
                rel = p.relative_to(self._skills_dir)
                skills.append({
                    "name": str(rel.with_suffix("")),
                    "size": p.stat().st_size,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                })
        return {
            "total_skills": len(skills),
            "skills": skills,
        }

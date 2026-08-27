# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""System prompt token audit — runnable per Phase to compare against baseline.

Usage: cd backend && python3 -m scripts.audit_system_prompt [--label phaseN] [--with-memory]
Output: JSON + human summary written to tests/prompt_audit_<label>.log
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from app.services.agent_service import AgentService
from app.services.memory_service import AgentSharedContext
from app.services.context_compressor import estimate_system_prompt_breakdown, estimate_text_tokens_rough
from app.tools.skill_tools import build_skills_system_prompt
from app.tools.registry import registry


def _memory_ctx(with_memory: bool):
    if not with_memory:
        return AgentSharedContext(agent_state=None, memory_summary="", dream_summary="", memory_entries=[])
    mem_sum = "记" * 2000
    dream = "梦" * 2000
    entries = [
        SimpleNamespace(title=f"m{i}", source_type="daily-summary", content="内" * 300)
        for i in range(12)
    ]
    return AgentSharedContext(agent_state=None, memory_summary=mem_sum, dream_summary=dream, memory_entries=entries)


def _chat_tools(with_lazy: bool) -> list:
    """Mirror chat.py main-path tool resolution: full registry minus lazy
    browser interaction sub-tools (二期)."""
    defs = registry.get_definitions()
    tools = defs if isinstance(defs, list) else list(defs.values())
    if not with_lazy:
        return tools
    lazy = {
        n for n in registry.get_tool_names_for_toolset("web")
        if n not in ("browser", "pdf_export", "web_search")
    }
    return [t for t in tools if (t.get("function") or {}).get("name") not in lazy]


async def main():
    label = "phaseN"
    with_memory = False
    deathmatch = False
    file_ref = False
    with_lazy = False
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--label" and i + 1 < len(args):
            label = args[i + 1]
        elif a == "--with-memory":
            with_memory = True
        elif a == "--deathmatch":
            deathmatch = True
        elif a == "--file-ref":
            file_ref = True
        elif a == "--lazy":
            with_lazy = True

    svc = AgentService()
    ws = SimpleNamespace(root_path="/tmp/ws")
    skills_prompt = await build_skills_system_prompt(None)

    user_skill_content = None
    if file_ref:
        file_parsing = Path("skills/file_parsing/SKILL.md")
        if file_parsing.exists():
            user_skill_content = file_parsing.read_text(encoding="utf-8")

    sp = await svc._build_system_prompt(
        assistant=None,
        shared_context=_memory_ctx(with_memory),
        workspace=ws,
        user=None,
        user_skill_content=user_skill_content,
        skills_system_prompt=skills_prompt,
        skill_files=None,
        identity_context=None,
        conversation_id=None,
        deathmatch_mode=deathmatch,
    )

    defs = registry.get_definitions()
    tools = defs if isinstance(defs, list) else list(defs.values())
    if with_lazy:
        lazy = {
            n for n in registry.get_tool_names_for_toolset("web")
            if n not in ("browser", "pdf_export", "web_search")
        }
        tools = [t for t in tools if (t.get("function") or {}).get("name") not in lazy]

    breakdown = estimate_system_prompt_breakdown(sp, tools=tools)
    log_path = Path(f"../tests/prompt_audit_{label}.log")
    log_path.parent.mkdir(exist_ok=True)
    out = {
        "label": label,
        "with_memory": with_memory,
        "deathmatch": deathmatch,
        "file_ref": file_ref,
        "with_lazy": with_lazy,
        "system_prompt_chars": len(sp),
        "breakdown": breakdown,
    }
    log_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nwritten: {log_path}")


asyncio.run(main())

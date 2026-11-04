# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompt catalog: every production prompt gets an id/version/owner (L0).

Text lives in the domain modules (``system_agent``, ``fragments`` and the
service modules during migration).  This catalog is the single registration
point: it carries ownership, scope, output contracts and call-site coverage
metadata for the AST inventory.  Importing it is idempotent.
"""
from __future__ import annotations

from typing import Dict, List

from app.prompts.audit import (
    AUDITOR_SYSTEM_TEMPLATE,
    CLAIM_VERIFY_SYSTEM,
)
from app.prompts.coordinator import (
    COORDINATOR_CLASSIFY_TEMPLATE,
    SEARCH_PLANNER_TEMPLATE,
)
from app.prompts.deathmatch import (
    BIBLE_EVOLUTION_PROMPT,
    BIBLE_GENERATION_PROMPT,
    CONTINUATION_PROMPT_TEMPLATE,
    CRITERIA_SYSTEM_PROMPT,
    GRILLING_FACT_RESEARCH_PROMPT,
    GRILLING_QUESTION_GENERATION_PROMPT,
    GRILLING_ROUND_SYNTHESIS_PROMPT,
    GRILLING_SYNTHESIS_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    JUDGE_USER_PROMPT_TEMPLATE,
    LOCAL_PATCH_SYSTEM_PROMPT,
    PLAN_AUDIT_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    REPETITION_DETECTED_PROMPT,
    REPLANNER_SYSTEM_PROMPT,
    STEP_CONTINUATION_PROMPT_TEMPLATE,
    VERIFIER_SYSTEM_PROMPT,
)
from app.prompts.fragments import (
    CROSS_TURN_CITATION_NOTE,
    MEMORY_INSTRUCTION_BOUNDARY,
    MEMORY_RELEVANCE_BOUNDARY,
    TOOL_AVAILABILITY_NOTE,
)
from app.prompts.memory import (
    MEMORY_ACTIVE_DREAMING_REHEARSE_PROMPT,
    MEMORY_ACTIVE_DREAMING_SCENARIOS_PROMPT,
    MEMORY_DREAM_SYSTEM_PROMPT,
    MEMORY_SUMMARY_SYSTEM_PROMPT,
)
from app.prompts.registry import register
from app.prompts.spec import PromptSection, PromptSpec
from app.prompts.tools import (
    _CLASSIFY_SYSTEM_PROMPT,
    _CODE_GENERATION_PROMPT,
    _CODE_GENERATION_PROMPT_PTC,
    _FALLBACK_TITLE_PROMPT,
    _SCHEDULE_EXTRACT_PROMPT,
    _TITLE_REPAIR_PROMPT,
    _TITLE_SYSTEM_PROMPT,
)
from app.prompts.voice import (
    VOICE_BARGE_IN_SYSTEM,
    VOICE_EOT_SYSTEM,
    VOICE_INTENT_SYSTEM,
    VOICE_INTERJECTION_SYSTEM,
)

VERSION = "1.0.0"


def _section(section_id: str, text: str, tier: str = "platform",
             rule_ids: tuple[str, ...] = (),
             budget_tokens: int | None = None) -> PromptSection:
    return PromptSection(
        id=section_id, text=text, tier=tier, stability="stable",
        rule_ids=rule_ids, budget_tokens=budget_tokens,
    )


def _source_hash(relpath: str) -> str:
    import hashlib
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / relpath
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ── Shared static fragments ─────────────────────────────────────────────────
register(PromptSpec(
    id="memory.boundary",
    version=VERSION,
    owner="memory",
    sections=(
        _section("memory.instruction", MEMORY_INSTRUCTION_BOUNDARY,
                 tier="safety_core", rule_ids=("SEC-MEM-01",)),
        _section("memory.relevance", MEMORY_RELEVANCE_BOUNDARY,
                 tier="safety_core", rule_ids=("SEC-MEM-02",)),
    ),
    scope={"mode": "*", "locale": "zh"},
    flags={"default_on": True, "kind": "static"},
    used_by=(
        "app/services/agent_service.py:_build_system_prompt",
        "app/services/memory_security.py",
        "app/services/memory_retrieval_service.py",
    ),
))

register(PromptSpec(
    id="tool.availability",
    version=VERSION,
    owner="agent-core",
    sections=(_section("tool.availability", TOOL_AVAILABILITY_NOTE,
                       tier="safety_core", rule_ids=("TOOL-01",)),),
    scope={"mode": "*", "locale": "zh"},
    flags={"kind": "static"},
    used_by=("app/services/agent_service.py:_build_system_prompt",),
))

register(PromptSpec(
    id="citation.cross_turn",
    version=VERSION,
    owner="citation",
    sections=(_section("citation.cross_turn", CROSS_TURN_CITATION_NOTE,
                       tier="platform", rule_ids=("CIT-01",)),),
    scope={"mode": "*", "locale": "zh"},
    flags={"kind": "static"},
    used_by=("app/services/agent_loop_citation.py:_refresh_citation_manifest",),
))


# ── Builders / inline production prompts ────────────────────────────────────
# ``kind=builder|inline`` means the text is assembled or still lives at the
# call site; the runtime hash is computed from the actual wire messages by
# ``llm_service`` and stamped with this id/version.
_INLINE_SPECS = (
    # id, owner, output_schema, used_by, locale, mode
    ("agent.system", "agent-core", None,
     ("app/api/chat.py:chat_stream", "app/prompts/system_agent.py"), "zh", "chat"),
    ("audit.scout", "audit", "audit.scout",
     ("app/services/agent_loop.py:_scout_missing_evidence",), "zh", "chat"),
    ("voice.duplex", "voice", None,
     ("app/services/voice_session_setup.py:_system_messages", "config.toml"), "zh", "voice"),
    ("memory.extraction", "memory", "memory.extraction",
     ("app/services/memory_concept_service.py",
      "app/services/memory_subconscious_service.py"), "zh", "worker"),
    ("memory.consolidation", "memory", None,
     ("app/services/memory_consolidation_service.py",), "zh", "worker"),
    ("memory.clarification", "memory", None,
     ("app/services/memory_clarification_service.py",), "zh", "worker"),
    ("memory.retrieval", "memory", None,
     ("app/services/memory_retrieval_service.py",), "zh", "worker"),
    ("memory.profile", "memory", None,
     ("app/services/memory_profile_service.py",), "zh", "worker"),
    ("memory.subconscious", "memory", None,
     ("app/services/memory_subconscious_service.py",), "zh", "worker"),
    ("memory.migration", "memory", None,
     ("app/services/memory_migration_service.py",), "zh", "worker"),
    ("memory.multimodal", "memory", None,
     ("app/services/memory_multimodal_service.py",), "zh", "worker"),
    ("memory.backfill", "memory", None,
     ("app/services/memory_backfill_service.py",), "zh", "worker"),
    ("memory.listwise_verify", "memory", None,
     ("app/services/memory_listwise_verifier.py",), "zh", "worker"),
    ("schedule.execute", "tools", None,
     ("app/services/agent_scheduler.py", "config.toml"), "zh", "worker"),
    ("background.worker", "tools", None,
     ("app/services/agent_worker.py", "config.toml"), "zh", "worker"),
    ("tool.digest", "tools", None,
     ("app/services/tool_result_digest.py",), "zh", "worker"),
    ("moa.aggregate", "tools", None,
     ("app/services/moa_service.py",), "en", "worker"),
    ("context.compressor", "tools", None,
     ("app/services/context_compressor.py",), "en", "worker"),
    ("citation.disambiguate", "citation", None,
     ("app/services/citation_ledger.py",), "zh", "chat"),
    ("agentic.judge", "tools", None,
     ("app/services/agentic_judge.py",), "zh", "worker"),
    ("proactive.learning", "memory", None,
     ("app/services/proactive_learning_service.py",), "zh", "worker"),
    ("subagent.child", "agent-core", None,
     ("app/tools/delegate_task.py",), "en", "subagent"),
    ("generic.system", "agent-core", None,
     ("app/services/agent_loop.py",), "zh", "chat"),
)

register(PromptSpec(
    id="coordinator.route",
    version=VERSION,
    owner="agent-core",
    sections=(_section("coordinator.route", COORDINATOR_CLASSIFY_TEMPLATE,
                       tier="mode", rule_ids=("ROUTE-01",),
                       budget_tokens=3500),),
    scope={"mode": "chat", "locale": "zh"},
    output_schema="coordinator.route",
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/agent_loop_coordinator.py:_coordinate",),
))

register(PromptSpec(
    id="audit.response",
    version=VERSION,
    owner="audit",
    sections=(_section("audit.response", AUDITOR_SYSTEM_TEMPLATE,
                       tier="task", rule_ids=("AUDIT-01",),
                       budget_tokens=6000),),
    scope={"mode": "chat", "locale": "zh"},
    output_schema="audit.verdict",
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/agent_loop_audit.py:_audit_response",
             "app/services/agent_loop.py:_chunked_full_audit"),
))

register(PromptSpec(
    id="audit.claim_verify",
    version=VERSION,
    owner="audit",
    sections=(_section("audit.claim_verify", CLAIM_VERIFY_SYSTEM,
                       tier="task", rule_ids=("AUDIT-02",)),),
    scope={"mode": "chat", "locale": "zh"},
    output_schema="claim.verify",
    flags={"kind": "text"},
    used_by=("app/services/agent_loop.py:_segmented_claim_verify",),
))

register(PromptSpec(
    id="search.plan",
    version=VERSION,
    owner="agent-core",
    sections=(_section("search.plan", SEARCH_PLANNER_TEMPLATE,
                       tier="task", rule_ids=("SEARCH-01",)),),
    scope={"mode": "chat", "locale": "zh"},
    output_schema="search.plan",
    flags={"kind": "text"},
    used_by=("app/services/agent_loop.py:AgentLoop._plan_search_queries",),
))

def _dm_section(section_id: str, text: str,
                budget_tokens: int | None = None) -> PromptSection:
    return _section(section_id, text, tier="task", budget_tokens=budget_tokens)


register(PromptSpec(
    id="deathmatch.judge",
    version=VERSION,
    owner="deathmatch",
    sections=(
        _dm_section("judge.system", JUDGE_SYSTEM_PROMPT, budget_tokens=1750),
        _dm_section("judge.user_template", JUDGE_USER_PROMPT_TEMPLATE),
    ),
    scope={"mode": "deathmatch", "locale": "zh"},
    output_schema="deathmatch.judge",
    flags={"kind": "text"},
    used_by=("app/services/deathmatch_service.py:_call_judge_llm",),
))

register(PromptSpec(
    id="deathmatch.grilling",
    version=VERSION,
    owner="deathmatch",
    sections=(
        _dm_section("grilling.questions", GRILLING_QUESTION_GENERATION_PROMPT,
                    budget_tokens=1900),
        _dm_section("grilling.round_synthesis", GRILLING_ROUND_SYNTHESIS_PROMPT),
        _dm_section("grilling.synthesis", GRILLING_SYNTHESIS_PROMPT),
        _dm_section("grilling.fact_research", GRILLING_FACT_RESEARCH_PROMPT),
    ),
    scope={"mode": "deathmatch", "locale": "zh"},
    output_schema="deathmatch.grilling",
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/deathmatch_service.py",),
))

register(PromptSpec(
    id="deathmatch.plan",
    version=VERSION,
    owner="deathmatch",
    sections=(
        _dm_section("plan.system", PLAN_SYSTEM_PROMPT, budget_tokens=2500),
        _dm_section("plan.audit", PLAN_AUDIT_SYSTEM_PROMPT),
        _dm_section("plan.replanner", REPLANNER_SYSTEM_PROMPT),
        _dm_section("plan.criteria", CRITERIA_SYSTEM_PROMPT),
        _dm_section("plan.local_patch", LOCAL_PATCH_SYSTEM_PROMPT),
    ),
    scope={"mode": "deathmatch", "locale": "zh"},
    output_schema="deathmatch.plan",
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/deathmatch_service.py",),
))

register(PromptSpec(
    id="deathmatch.verifier",
    version=VERSION,
    owner="deathmatch",
    sections=(_dm_section("verifier.system", VERIFIER_SYSTEM_PROMPT),),
    scope={"mode": "deathmatch", "locale": "zh"},
    output_schema="deathmatch.verifier",
    flags={"kind": "text"},
    used_by=("app/services/deathmatch_service.py",),
))

register(PromptSpec(
    id="deathmatch.continuation",
    version=VERSION,
    owner="deathmatch",
    sections=(
        _dm_section("continuation.turn", CONTINUATION_PROMPT_TEMPLATE),
        _dm_section("continuation.step", STEP_CONTINUATION_PROMPT_TEMPLATE),
        _dm_section("continuation.repetition", REPETITION_DETECTED_PROMPT),
    ),
    scope={"mode": "deathmatch", "locale": "zh"},
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/deathmatch_service.py",),
))

register(PromptSpec(
    id="deathmatch.bible",
    version=VERSION,
    owner="deathmatch",
    sections=(
        _dm_section("bible.generate", BIBLE_GENERATION_PROMPT),
        _dm_section("bible.evolve", BIBLE_EVOLUTION_PROMPT),
    ),
    scope={"mode": "deathmatch", "locale": "zh"},
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/deathmatch_service.py",),
))

register(PromptSpec(
    id="title.generation", version=VERSION, owner="tools",
    sections=(_section("title.system", _TITLE_SYSTEM_PROMPT),
              _section("title.repair", _TITLE_REPAIR_PROMPT),
              _section("title.fallback", _FALLBACK_TITLE_PROMPT)),
    scope={"mode": "worker", "locale": "zh"}, output_schema="title.generation",
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/title_generator.py",),
))

register(PromptSpec(
    id="schedule.parse", version=VERSION, owner="tools",
    sections=(_section("schedule.parse", _SCHEDULE_EXTRACT_PROMPT),),
    scope={"mode": "worker", "locale": "zh"}, output_schema="schedule.parse",
    flags={"kind": "text"},
    used_by=("app/services/schedule_parser.py",),
))

register(PromptSpec(
    id="error.classify", version=VERSION, owner="tools",
    sections=(_section("error.classify", _CLASSIFY_SYSTEM_PROMPT),),
    scope={"mode": "worker", "locale": "en"}, output_schema="error.classify",
    flags={"kind": "text"},
    used_by=("app/services/error_classifier.py",),
))

register(PromptSpec(
    id="codegen.python", version=VERSION, owner="tools",
    sections=(_section("codegen.standard", _CODE_GENERATION_PROMPT, budget_tokens=1800),
              _section("codegen.ptc", _CODE_GENERATION_PROMPT_PTC, budget_tokens=1800)),
    scope={"mode": "worker", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/tools/code_execution.py",),
))

register(PromptSpec(
    id="memory.summary", version=VERSION, owner="memory",
    sections=(_section("memory.summary", MEMORY_SUMMARY_SYSTEM_PROMPT),),
    scope={"mode": "worker", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/services/memory_service.py",),
))

register(PromptSpec(
    id="memory.dream", version=VERSION, owner="memory",
    sections=(_section("memory.dream", MEMORY_DREAM_SYSTEM_PROMPT),),
    scope={"mode": "worker", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/services/memory_service.py",),
))

register(PromptSpec(
    id="memory.active_dreaming", version=VERSION, owner="memory",
    sections=(_section("memory.active_dreaming.scenarios", MEMORY_ACTIVE_DREAMING_SCENARIOS_PROMPT),
              _section("memory.active_dreaming.rehearse", MEMORY_ACTIVE_DREAMING_REHEARSE_PROMPT)),
    scope={"mode": "worker", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/services/memory_dreaming_service.py",),
))

register(PromptSpec(
    id="voice.barge_in", version=VERSION, owner="voice",
    sections=(_section("voice.barge_in", VOICE_BARGE_IN_SYSTEM, budget_tokens=2000),),
    scope={"mode": "voice", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/services/voice_classifiers.py:_classify_barge_in",),
))

register(PromptSpec(
    id="voice.intent", version=VERSION, owner="voice",
    sections=(_section("voice.intent", VOICE_INTENT_SYSTEM, budget_tokens=1700),),
    scope={"mode": "voice", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/services/voice_classifiers.py:_classify_intent",),
))

register(PromptSpec(
    id="voice.interjection", version=VERSION, owner="voice",
    sections=(_section("voice.interjection", VOICE_INTERJECTION_SYSTEM),),
    scope={"mode": "voice", "locale": "zh"},
    flags={"kind": "text", "allow_placeholders": True},
    used_by=("app/services/voice_classifiers.py:_classify_interjection",),
))

register(PromptSpec(
    id="voice.endpointing", version=VERSION, owner="voice",
    sections=(_section("voice.endpointing", VOICE_EOT_SYSTEM),),
    scope={"mode": "voice", "locale": "zh"},
    flags={"kind": "text"},
    used_by=("app/services/voice_classifiers.py:_classify_eot",),
))

_CONFIG_PROMPT_KEYS = {
    "schedule.execute": "agent.scheduled_task_system_prompt",
    "background.worker": "agent.background_task_system_prompt",
    "voice.duplex": "voice.system_prompt",
}

for _id, _owner, _schema, _used_by, _locale, _mode in _INLINE_SPECS:
    _flags = {"kind": "builder" if _id == "agent.system" else "inline"}
    if _id == "agent.system":
        _flags["builder_source"] = "app/prompts/system_agent.py"
        _flags["builder_hash"] = _source_hash("app/prompts/system_agent.py")
    if _id in _CONFIG_PROMPT_KEYS:
        _flags["config_key"] = _CONFIG_PROMPT_KEYS[_id]
    register(PromptSpec(
        id=_id,
        version=("1.1.0" if _id == "agent.system" else VERSION),
        owner=_owner,
        sections=(),
        scope={"mode": _mode, "locale": _locale},
        output_schema=_schema,
        flags=_flags,
        used_by=_used_by,
    ))


# ── Call-site coverage ──────────────────────────────────────────────────────
# File-level mapping is deliberate: line numbers move, ownership does not.
SITE_PROMPT_BY_FILE: Dict[str, str] = {
    "app/api/chat.py": "agent.system",
    "app/services/agent_loop.py": "audit.response",
    "app/services/agent_scheduler.py": "schedule.execute",
    "app/services/agent_worker.py": "background.worker",
    "app/services/agentic_judge.py": "agentic.judge",
    "app/services/deathmatch_service.py": "deathmatch.judge",
    "app/services/memory_backfill_service.py": "memory.backfill",
    "app/services/memory_clarification_service.py": "memory.clarification",
    "app/services/memory_consolidation_service.py": "memory.consolidation",
    "app/services/memory_dreaming_service.py": "memory.active_dreaming",
    "app/services/memory_listwise_verifier.py": "memory.listwise_verify",
    "app/services/memory_migration_service.py": "memory.migration",
    "app/services/memory_multimodal_service.py": "memory.multimodal",
    "app/services/memory_profile_service.py": "memory.profile",
    "app/services/memory_retrieval_service.py": "memory.retrieval",
    "app/services/memory_service.py": "memory.summary",
    "app/services/memory_subconscious_service.py": "memory.subconscious",
    "app/services/moa_service.py": "moa.aggregate",
    "app/services/title_generator.py": "title.generation",
    "app/services/tool_result_digest.py": "tool.digest",
    "app/services/voice_service.py": "voice.duplex",
    "app/tools/code_execution.py": "codegen.python",
    "app/tools/delegate_task.py": "subagent.child",
}


def prompt_id_for_file(relpath: str) -> str:
    return SITE_PROMPT_BY_FILE.get(relpath, "generic.system")


def registered_ids() -> List[str]:
    from app.prompts.registry import all_specs

    return [s.id for s in all_specs()]

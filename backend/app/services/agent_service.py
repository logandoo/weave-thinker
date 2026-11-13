# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import json
import re as _re
import time as _time
from typing import Any, Dict, List, Optional

from app.core.config import get_config
from app.prompts.system_agent import (
    _TEMPORAL_AWARENESS_RULE,  # re-export (P1 move-only)
    _memory_injection_sections,  # re-export (P1 move-only)
)
from app.db.database import Assistant, UserWorkspace
from app.services.llm_service import LLMService
from app.services.memory_service import AgentSharedContext
from app.tools.memory import _get_memory_path, _read_entries

config = get_config()
logger = logging.getLogger(__name__)


# PHASE 2A: Strip stale ephemeral agent-state markers from persisted assistant
# messages before they re-enter the LLM context. Old conversations may contain
# the "[正在准备工具...]" marker that was previously yielded as content; if
# replayed verbatim it primes the model to dump similar prose instead of
# calling tools (see hermes-agent PR #3528 — "automatic stripping of stale
# budget warnings from conversation history" — same class of bug).
_EPHEMERAL_NUDGE_RE = _re.compile(
    r'\n*\*?\[(?:正在准备工具|tool[_\- ]?nudge)[.…]*\]\*?\n*',
    _re.IGNORECASE,
)


# 时间轴判断规则（检索与引用）— static section, appended to every system
# prompt (2026-09-11 wave-3). The current time itself is injected in the
# dynamic tier (`当前时间: … (北京时间, …)`); this rule tells the model how to
# use publish dates when retrieved sources conflict or differ subtly, and to
# turn on provider-side time filtering for time-sensitive queries.


def _sanitize_history_content(text: Optional[str]) -> str:
    if not text:
        return text or ""
    return _EPHEMERAL_NUDGE_RE.sub("", text)


_IDENTITY_MEMORY_CACHE: Dict[tuple[str, bool], tuple[float, str]] = {}

_IDENTITY_EXTRACT_PROMPT = (
    "你是身份信息抽取器。从下面的记忆条目中，筛选出与用户/助手的身份、名字、昵称、"
    "称呼方式相关的条目（如'用户叫张三'、'助手名叫小悟'、'称呼我为XX'）。\n"
    "任务细节、偏好、项目信息等不属于身份信息，不要包含。\n"
    "输出JSON：{\"identity_entries\": [\"筛选出的条目原文1\", ...]}\n"
    "若没有身份相关条目，输出 {\"identity_entries\": []}。\n"
    "只输出JSON，不要输出其他内容。"
)


async def _load_identity_memory_context(user_id: Optional[Any]) -> str:
    """Read file-based memory and extract identity/name entries for prompt injection.

    The relevance judgment is LLM-based (agentic principle — the former
    keyword filter could not generalize to arbitrary entry phrasings).
    Cached 60s per user. LLM failure → empty context (the identity facts
    remain available through the main memory pipeline).
    """
    if not user_id:
        return ""
    uid = str(user_id)
    now = _time.monotonic()
    from app.core.config import get_config
    strict = bool(get_config().memory_provenance_strict)
    cache_key = (uid, strict)

    cached = _IDENTITY_MEMORY_CACHE.get(cache_key)
    if cached:
        cached_time, cached_value = cached
        if now - cached_time < 60:
            return cached_value

    from app.services.agentic_judge import judge_json

    all_entries: list[tuple[str, str]] = []  # (label, entry)
    for target in ("agent", "user"):
        try:
            path = _get_memory_path(target, str(user_id), ensure_dir=False)
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            entries = _read_entries(text)
            if strict:
                # The identity loader is a second reader of the same file
                # memory: strict quarantine must apply here too, otherwise a
                # quarantined entry still reaches the system prompt.
                from app.services.memory_provenance import verified_entries
                try:
                    entries = verified_entries(path, str(user_id), target, entries)
                except Exception:
                    logger.warning(
                        "identity memory provenance check failed for %s/%s; "
                        "dropping entries under strict mode", target, user_id,
                        exc_info=True,
                    )
                    entries = []
            label = "助手记忆" if target == "agent" else "用户记忆"
            all_entries.extend((label, e) for e in entries)
        except Exception:
            logger.debug("identity memory load failed for %s/%s", target, user_id, exc_info=True)

    result = ""
    if all_entries:
        numbered = "\n".join(f"{i}. [{label}] {e[:200]}" for i, (label, e) in enumerate(all_entries))
        parsed = await judge_json(
            _IDENTITY_EXTRACT_PROMPT,
            f"记忆条目：\n{numbered[:6000]}\n\n只输出JSON。",
            task="identity_facts",
            default=None,

            timeout=120.0,
        )
        if isinstance(parsed, dict) and isinstance(parsed.get("identity_entries"), list):
            chosen_texts = {str(e) for e in parsed["identity_entries"]}
            chosen = [(label, e) for label, e in all_entries if e in chosen_texts][:8]
            if chosen:
                sections = []
                for label in ("助手记忆", "用户记忆"):
                    entries = [e for lbl, e in chosen if lbl == label]
                    if entries:
                        sections.append(
                            f"## {label} 中关于身份/名字的信息：\n" + "\n".join(entries)
                        )
                result = "\n\n".join(sections)
    _IDENTITY_MEMORY_CACHE[cache_key] = (now, result)
    return result


def should_use_custom_model(assistant: Optional[Assistant]) -> bool:
    """Determine whether to use the assistant's custom model settings.

    Returns True when:
    - provider_type is "custom" (always uses custom settings), OR
    - provider_type is "qwen3.8_vllm" (address is assistant-configured per the
      modelscope vLLM deployment guide; falls back to server config inside
      create_llm_service when the field is empty), OR
    - use_custom_model is True (for built-in providers with overrides), OR
    - model_alias selects a non-default endpoint (model_gateway era: an
      explicit alias choice = the assistant's own model setting, P0 rule —
      all of the assistant's LLM behavior follows it).
    """
    if not assistant:
        return False
    alias = str(getattr(assistant, "model_alias", None) or "")
    if alias:
        try:
            from app.model_gateway.registry import get_model_registry
            if alias != get_model_registry().default_alias():
                return True
        except Exception:
            if alias != "deepseek":
                return True
    pt = getattr(assistant, "provider_type", "deepseek")
    if pt in ("custom", "qwen3.8_vllm"):
        return True
    return bool(assistant.use_custom_model)



class AgentService:
    _llm_cache: Dict[tuple, LLMService] = {}

    def __init__(self):
        pass

    def resolve_aux_model_context(self, assistant: Optional[Assistant], main_llm: Optional[LLMService]) -> Optional[LLMService]:
        """P0 (2026-08-21, user requirement): the aux/validator LLM client for
        an assistant's turn.

        Custom-model assistants (provider_type custom/qwen3.8_vllm or
        use_custom_model) route coordinator/audit/aux calls through their
        MAIN client — the user's model setting governs every LLM behavior of
        the assistant unless explicitly configured per-assistant
        (subtask_custom_*). Non-custom assistants keep the operator's global
        aux keys (coordinator model from [agent.auxiliary]); None means "let
        AgentLoop fall back to the main client".
        """
        if assistant is not None and should_use_custom_model(assistant):
            return main_llm
        coord_model = config.agent_auxiliary_coordinator_model
        if coord_model:
            from app.model_gateway import factory
            from app.model_gateway.registry import get_model_registry
            return factory.build_llm_service(get_model_registry().resolve("coordinator"))
        return None

    def title_generator_kwargs(self, assistant: Optional[Assistant], main_llm: Optional[LLMService]) -> dict:
        """P0: fields for TitleGeneratorService. Custom-model assistants
        (incl. provider-config qwen3.8_vllm with an empty row-level URL)
        resolve through the MAIN client; non-custom keep the legacy None
        (global [api]) behavior."""
        if assistant is not None and should_use_custom_model(assistant) and main_llm is not None:
            return {
                "custom_api_url": str(main_llm.client.base_url),
                "custom_api_key": main_llm.client.api_key or None,
                "custom_model_name": main_llm.custom_model_name or None,
            }
        return {
            "custom_api_url": (assistant.custom_api_url if should_use_custom_model(assistant) else None) if assistant else None,
            "custom_api_key": (assistant.custom_api_key if should_use_custom_model(assistant) else None) if assistant else None,
            "custom_model_name": (assistant.custom_model_name if should_use_custom_model(assistant) else None) if assistant else None,
        }

    def create_llm_service(self, assistant: Optional[Assistant]) -> LLMService:
        """Create LLM service for the assistant.

        model_gateway 收口（2026-08-30 解耦重构）：端点解析统一走
        registry.endpoint_for_assistant（model_alias 优先，legacy
        provider_type/custom_* 行级字段语义完全一致），客户端构造走
        factory.build_llm_service。进程级缓存键不变（url, key hash, model,
        preserve）——P0-4 的 TCP+TLS 复用不受影响。
        """
        import hashlib

        from app.model_gateway import factory
        from app.model_gateway.registry import get_model_registry

        provider_type = getattr(assistant, "provider_type", "deepseek") or "deepseek" if assistant else "deepseek"
        ep = get_model_registry().endpoint_for_assistant(assistant)

        # A2 (2026-08-21): preserve-thinking providers keep the current
        # turn's assistant reasoning_content on the wire (qwen3.8_vllm
        # chat_template_kwargs.preserve_thinking / deepseek round-trip
        # contract / mimo thinking chain). Without this flag the reasoning
        # attached by _rejected_append would be stripped at the wire — the
        # A2 mechanism is dead code (A4.9 Critical-1 fix).
        # 模型网关：以解析端点的 provider_type 为准（model_alias 助手的
        # DB 旧列与新模型无关；legacy inline 端点携带旧列值）。
        # wave-7：preserve 语义由 thinking profile 承载（profiles 收口）。
        from app.model_gateway.profiles import get_thinking_profile
        _preserve = get_thinking_profile(ep.provider_type or provider_type).preserve_thinking

        cache_key = (
            (ep.base_url or ""),
            hashlib.sha256((ep.api_key or "").encode()).hexdigest()[:16],
            (ep.model_name or ""),
            _preserve,
            # A4.9 复审 R2 Minor：同凭据不同格式的端点不得共享缓存实例
            # （endpoint 元数据随首个构建者，provider_type 必须入键）
            (ep.provider_type or ""),
            # 2026-09-13 A4.9 Critical 修复：params/extra.user_params/is_custom 入键。
            # 否则"仅覆盖 model/params"的用户端点与系统端点同键——系统实例先建则
            # 用户参数被静默忽略；用户实例先建则后续无覆盖用户复用其采样参数
            # （跨用户污染）。缓存实例持有首个构建者的 endpoint 元数据，必须按
            # 全部行为相关字段区分。
            json.dumps(ep.params or {}, sort_keys=True, default=str),
            json.dumps((ep.extra or {}).get("user_params") or {}, sort_keys=True, default=str),
            bool(ep.is_custom),
        )
        if cache_key not in AgentService._llm_cache:
            AgentService._llm_cache[cache_key] = factory.build_llm_service(
                ep, preserve_reasoning=_preserve
            )
            # A4.9 Minor 修复（2026-09-13）：容量上限（用户级覆盖会引入更多
            # 端点组合；FIFO 淘汰最旧实例，防进程生命周期内无界增长）。
            while len(AgentService._llm_cache) > 64:
                AgentService._llm_cache.pop(next(iter(AgentService._llm_cache)), None)
        return AgentService._llm_cache[cache_key]

    def create_iteration_llm_service(
        self, assistant: Optional[Assistant], main_llm: Optional[LLMService] = None
    ) -> tuple[LLMService, str]:
        """PHASE 3: build the LLM client used for tool-calling iterations.

        Returns ``(iteration_llm, iteration_provider_type)``. When the
        assistant has populated ``subtask_custom_*`` fields the iterations
        run through a separate (typically cheaper / non-thinking) client;
        otherwise the main client is reused. ``AgentLoop`` decides per-call
        whether thinking is enabled for iterations: live-thinking mode
        (``agent.tool_loop.live_thinking``, default on) enables+streams it,
        but ONLY when the main client serves iterations — a dedicated
        subtask client keeps thinking disabled, since operators configure
        it as a cheap/non-thinking model that would 400 on thinking params
        (A4.9 review I1).

        P0-4: ``main_llm`` can be passed to avoid creating a duplicate client
        when the caller already has one (e.g. chat.py's llm_service).
        """
        main_llm = main_llm or self.create_llm_service(assistant)
        if assistant is None:
            return main_llm, "deepseek"

        from app.model_gateway import factory
        from app.model_gateway.registry import get_model_registry

        # 模型网关：迭代 wire provider_type 以主端点为准（A4.9 复审 Critical-1）。
        try:
            main_provider = factory.wire_provider_type(
                main_llm, getattr(assistant, "provider_type", "deepseek") or "deepseek"
            )
        except Exception:
            main_provider = getattr(assistant, "provider_type", "deepseek") or "deepseek"

        # model_gateway 收口：subtask_model_alias（新）或 legacy subtask_custom_*
        # 行级字段 → 独立迭代客户端；否则复用主客户端（live-thinking 仅在主
        # 客户端迭代时开启的既有语义不变）。
        has_subtask = bool(getattr(assistant, "subtask_model_alias", None)) or bool(
            getattr(assistant, "use_subtask_model", False)
        )
        if not has_subtask:
            return main_llm, main_provider

        try:
            registry = get_model_registry()
            main_ep = registry.endpoint_for_assistant(assistant)
            sub_ep = registry.endpoint_for_subtask(assistant, main_ep)
            if sub_ep == main_ep:
                return main_llm, main_provider
            sub_llm = factory.build_llm_service(sub_ep)
            sub_provider = sub_ep.provider_type or main_provider
            return sub_llm, sub_provider
        except Exception:
            logger.exception(
                "create_iteration_llm_service: failed to build subtask LLM, "
                "falling back to main client"
            )
            return main_llm, main_provider


    async def _build_system_prompt(
        self,
        *,
        assistant: Optional[Assistant],
        shared_context: AgentSharedContext,
        workspace: UserWorkspace,
        user: Optional[Any] = None,
        user_skill_content: Optional[str] = None,
        skills_system_prompt: Optional[str] = None,
        skill_files: Optional[List[Dict[str, Any]]] = None,
        identity_context: Optional[str] = None,
        conversation_id: Optional[str] = None,
        deathmatch_mode: bool = False,
        offered_tool_names: Optional[List[str]] = None,
        task: str = "",
        workspace_ctx: Optional[Any] = None,
    ) -> str:
        """Build the agent system prompt (P1.2 move-only).

        The implementation lives in :mod:`app.prompts.system_agent`; this
        delegate keeps the historical call site stable.  The temporal rule
        (_TEMPORAL_AWARENESS_RULE) is wired inside that builder, including the
        "当前时间" / "北京时间" clock line in its dynamic tier.
        """
        from app.prompts.system_agent import build_agent_system_prompt

        return await build_agent_system_prompt(
            assistant=assistant,
            shared_context=shared_context,
            workspace=workspace,
            user=user,
            user_skill_content=user_skill_content,
            skills_system_prompt=skills_system_prompt,
            skill_files=skill_files,
            identity_context=identity_context,
            conversation_id=conversation_id,
            deathmatch_mode=deathmatch_mode,
            offered_tool_names=offered_tool_names,
            task=task,
            workspace_ctx=workspace_ctx,
        )

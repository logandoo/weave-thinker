# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""AgentTaskSubmissionService — atomic voice delegation submission (§5).

One submission = 1 execution conversation (C2) + 1 initial goal Message +
1 AgentTask (`execution_mode="separate_v1"`), committed in a single transaction.
C1 (source voice conversation) is referenced only; it is never reused as the
execution context.

Feature-flagged via `[voice_delegation] enabled` (default false, §15.3).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_config
from app.db.database import AgentTask, Assistant, Conversation, Message
from app.model_gateway.registry import get_model_registry
from app.services.agent_run_spec import (
    canonical_request_fingerprint,
    build_execution_manifest,
    resolve_highest_reasoning,
)

logger = logging.getLogger(__name__)

SEPARATE_MODE = "separate_v1"
VOICE_DELEGATION_TASK_TYPE = "voice_delegation"
from app.services.agent_task_status import ACTIVE_STATUSES
_WHITESPACE_RE = re.compile(r"\s+")
_NAME_SEP_RE = re.compile(r"[\s_\-]+")


class AssistantResolutionError(Exception):
    """Explicit, user-safe failure — never fall back to an arbitrary assistant."""

    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class SubmissionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_assistant_name(name: str) -> str:
    """Display-normalised name (NFKC, collapsed whitespace)."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = text.replace("_", " ").replace("-", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


def _match_key(name: str) -> str:
    """ASR-tolerant match key: whitespace is insignificant inside a name."""
    return _NAME_SEP_RE.sub("", normalize_assistant_name(name)).casefold()


def resolve_unique_assistant(candidates: Iterable[Any], name: str) -> Any:
    """Exactly one normalised match, or an explicit error (never pick first)."""
    key = _match_key(name)
    if not key:
        raise AssistantResolutionError("assistant_name_empty", "助手名称为空")
    matches = [c for c in candidates if _match_key(getattr(c, "name", "")) == key]
    if not matches:
        raise AssistantResolutionError(
            "assistant_not_found", f"没有找到名为“{normalize_assistant_name(name)}”的助手")
    if len(matches) > 1:
        raise AssistantResolutionError(
            "assistant_ambiguous",
            f"有 {len(matches)} 个助手都叫“{normalize_assistant_name(name)}”，请先确认是哪一个",
        )
    return matches[0]


def _voice_assistant_names() -> set[str]:
    from app.services.assistant_service import VOICE_ASSISTANT_NAME, LEGACY_VOICE_ASSISTANT_NAME

    return {_match_key(VOICE_ASSISTANT_NAME), _match_key(LEGACY_VOICE_ASSISTANT_NAME)}


async def _load_candidates(db, user_id: str) -> list[Assistant]:
    result = await db.execute(select(Assistant).where(Assistant.user_id == user_id))
    excluded = _voice_assistant_names()
    return [a for a in result.scalars().all() if _match_key(a.name) not in excluded]


def _new_default_assistant(user_id: str) -> Assistant:
    config = get_config()
    return Assistant(
        user_id=user_id,
        name=config.default_assistant_name,
        system_prompt=config.default_assistant_system_prompt,
        temperature=config.default_assistant_temperature,
        top_p=config.default_assistant_top_p,
        top_k=config.default_assistant_top_k,
        presence_penalty=config.default_assistant_presence_penalty,
        frequency_penalty=config.default_assistant_frequency_penalty,
        max_tokens=config.default_assistant_max_tokens,
    )


async def resolve_target_assistant(
    db, *, user_id: str, assistant_name: Optional[str] = None,
) -> Assistant:
    """Resolve the delegation target inside the caller's ownership scope.

    Default = the user's built-in 默认助手 by configured NAME (never the first
    DB row, never the sidebar selection, never 语音助理). Missing default is
    created explicitly. A named assistant must match exactly one candidate.
    """
    candidates = await _load_candidates(db, user_id)
    if assistant_name and str(assistant_name).strip():
        return resolve_unique_assistant(candidates, assistant_name)

    config = get_config()
    default_key = _match_key(config.default_assistant_name)
    defaults = [a for a in candidates if _match_key(a.name) == default_key]
    if len(defaults) == 1:
        return defaults[0]
    if len(defaults) > 1:
        raise AssistantResolutionError(
            "default_assistant_ambiguous",
            f"存在 {len(defaults)} 个默认助手，请先整理或点名指定助手",
        )
    assistant = _new_default_assistant(user_id)
    db.add(assistant)
    await db.flush()
    return assistant


async def resolve_reasoning_for_assistant(assistant: Assistant):
    """Effective endpoint (user override applied) → strict-highest reasoning."""
    endpoint = get_model_registry().endpoint_for_assistant(assistant)
    return endpoint, resolve_highest_reasoning(endpoint)


def _active_count_condition(user_id: str):
    return (
        AgentTask.user_id == user_id,
        AgentTask.execution_mode == SEPARATE_MODE,
        AgentTask.status.in_(ACTIVE_STATUSES),
    )


async def active_separate_task_for_conversation(db, conversation_id: str) -> Optional[AgentTask]:
    """C2 ownership guard: an active task owns its execution conversation."""
    if not conversation_id:
        return None
    result = await db.execute(
        select(AgentTask).where(
            AgentTask.execution_conversation_id == conversation_id,
            AgentTask.execution_mode == SEPARATE_MODE,
            AgentTask.status.in_(ACTIVE_STATUSES),
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def _find_idempotent(db, user_id: str, key: str) -> Optional[AgentTask]:
    result = await db.execute(
        select(AgentTask).where(
            AgentTask.user_id == user_id,
            AgentTask.idempotency_key == key,
        )
    )
    return result.scalar_one_or_none()


async def source_message_is_current(
    db, *, conversation_id: str, source_message_id: str,
) -> bool:
    """True when the source user message is still the newest user turn.

    A barge-in / "别委派了" adds a newer user message before the tool call
    commits; the delegation must be discarded instead of acting on a stale
    permit (§5.1(6)(7)).
    """
    if not conversation_id or not source_message_id:
        return True
    message = await db.get(Message, source_message_id)
    if message is None or str(message.conversation_id) != str(conversation_id):
        return False
    if str(message.role or "") != "user":
        return False
    newer = await db.scalar(
        select(func.count(Message.id)).where(
            Message.conversation_id == conversation_id,
            Message.role == "user",
            Message.created_at > message.created_at,
        )
    )
    return (newer or 0) == 0


async def _daily_wall_time_seconds(db, user_id: str) -> float:
    """Sum of cumulative delegated-task wall time for the server-local day."""
    from datetime import timezone

    local_now = datetime.now().astimezone()
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    rows = (await db.execute(
        select(AgentTask.usage_meta).where(
            AgentTask.user_id == user_id,
            AgentTask.execution_mode == SEPARATE_MODE,
            AgentTask.created_at >= start,
        )
    )).scalars().all()
    total = 0.0
    for raw in rows:
        try:
            value = json.loads(raw or "{}")
            total += float((value or {}).get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return total


def _dedupe_result(task: AgentTask, *, fingerprint: str, assistant_name: str) -> dict:
    if task.request_fingerprint and task.request_fingerprint != fingerprint:
        raise SubmissionError(
            "idempotency_conflict",
            "同一个语音消息已经提交过参数不同的委派；如需重跑请发送新消息",
            status_code=409,
        )
    manifest = {}
    try:
        manifest = json.loads(task.execution_config or "{}")
    except (TypeError, json.JSONDecodeError):
        manifest = {}
    reasoning = manifest.get("reasoning") or {}
    return {
        "status": task.status,
        "task_id": task.id,
        "execution_conversation_id": task.execution_conversation_id,
        "assistant_name": assistant_name,
        "reasoning_policy": reasoning.get("policy", "strict_highest"),
        "requested_reasoning_effort": reasoning.get("effort"),
        "deduplicated": True,
    }


async def submit_voice_delegation(
    db,
    *,
    user,
    goal: str,
    assistant_name: Optional[str] = None,
    title: Optional[str] = None,
    source_conversation_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    origin_kind: str = "voice",
    enforce_flag: bool = True,
    project_path: Optional[str] = None,
) -> dict:
    """Create C2 + goal + T atomically, or return the original submission."""
    config = get_config()
    if enforce_flag and not config.voice_delegation_enabled:
        raise SubmissionError(
            "voice_delegation_disabled",
            "语音委派功能未开启（[voice_delegation] enabled=false）",
            status_code=403,
        )

    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        raise SubmissionError("user_required", "无法获取用户信息", status_code=401)
    goal_text = str(goal or "").strip()
    if not goal_text:
        raise SubmissionError("goal_required", "缺少 goal 参数（完整任务描述）")

    workspace_mode = "scratch"
    workspace_meta = None
    if project_path:
        if not config.voice_delegation_worktree_enabled:
            raise SubmissionError(
                "worktree_disabled", "worktree 委派模式未开启", status_code=403)
        from app.services.git_worktree_service import (
            WorktreeError,
            validate_project_path_async,
        )
        from app.services.workspace_service import ensure_user_workspace

        try:
            workspace = await ensure_user_workspace(
                db, user_id, getattr(user, "username", None))
            project = await validate_project_path_async(
                workspace.root_path, str(project_path))
        except WorktreeError as exc:
            raise SubmissionError(
                "project_path_invalid", str(exc), status_code=422) from exc
        workspace_mode = "worktree"
        workspace_meta = json.dumps({"project_path": str(project)}, ensure_ascii=False)

    # Same task context for submission and later execution: apply user model
    # overrides before resolving the effective endpoint. Failure must FAIL the
    # delegation — never silently freeze the system endpoint.
    try:
        from app.services.user_model_provider_service import activate_user_overrides

        await activate_user_overrides(db, user_id)
    except Exception as exc:
        logger.exception("activate_user_overrides failed during delegation submission")
        raise SubmissionError(
            "user_override_unavailable",
            "用户模型配置加载失败；为避免把任务发往其他供应商，本次委派未提交",
            status_code=503,
        ) from exc

    assistant = await resolve_target_assistant(
        db, user_id=user_id, assistant_name=assistant_name)
    alias = str(getattr(assistant, "model_alias", "") or "")
    if alias:
        from app.model_gateway.registry import get_model_registry

        if not get_model_registry().has(alias):
            raise SubmissionError(
                "model_alias_unavailable",
                f"目标助手的模型别名“{alias}”不可用；严格模式不会回落到 main",
                status_code=422,
            )
    endpoint, reasoning = await resolve_reasoning_for_assistant(assistant)
    policy = config.voice_delegation_reasoning_policy or "strict_highest"
    if policy == "strict_highest" and not reasoning.supported:
        raise SubmissionError(
            "reasoning_unavailable",
            f"目标模型不支持思考档位，严格模式无法提交；请改用普通模式或更换助手（{assistant.name}）",
            status_code=422,
        )
    if policy == "strict_highest":
        reasoning = reasoning.__class__(
            policy="strict_highest",
            supported=reasoning.supported,
            enabled=reasoning.enabled,
            effort=reasoning.effort,
            thinking_budget=reasoning.thinking_budget,
            scope=reasoning.scope,
        )
    else:
        reasoning = reasoning.__class__(
            policy="disabled", supported=reasoning.supported,
            enabled=False, effort=None,
        )

    idem_key = (
        f"{origin_kind}:{source_message_id}" if source_message_id
        else f"{origin_kind}:{uuid.uuid4()}"
    )
    fingerprint = canonical_request_fingerprint(
        goal=goal_text,
        target_assistant_id=assistant.id,
        reasoning_policy=reasoning.policy,
        reasoning_effort=reasoning.effort,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
    )

    existing = await _find_idempotent(db, user_id, idem_key)
    if existing is not None:
        return _dedupe_result(
            existing, fingerprint=fingerprint,
            assistant_name=_assistant_name_from_manifest(existing, assistant.name),
        )
    if source_conversation_id and source_message_id:
        try:
            current_source = await source_message_is_current(
                db, conversation_id=str(source_conversation_id),
                source_message_id=str(source_message_id),
            )
        except Exception:
            current_source = False
        if not current_source:
            raise SubmissionError(
                "delegation_superseded",
                "用户已经发了更新的消息；本次委派未提交（不会用旧许可创建任务）",
                status_code=409,
            )

    running = await db.scalar(
        select(func.count(AgentTask.id)).where(
            *_active_count_condition(user_id),
            AgentTask.status.in_(("claimed", "running", "resumable", "cancelling")),
        )
    )
    if (running or 0) >= config.voice_delegation_max_running_per_user:
        raise SubmissionError(
            "too_many_running",
            "当前已有执行中的委派任务，请等它结束或先取消",
            status_code=429,
        )
    pending = await db.scalar(
        select(func.count(AgentTask.id)).where(
            *_active_count_condition(user_id),
            # Parked waiting_* tasks hold a C2 and must count against the
            # per-user pending budget (§9.3).
            AgentTask.status.in_((
                "pending", "waiting_input", "waiting_auth", "waiting_budget",
            )),
        )
    )
    if (pending or 0) >= config.voice_delegation_max_pending_per_user:
        raise SubmissionError(
            "too_many_pending", "待执行的委派任务已满", status_code=429)

    daily_cap = config.voice_delegation_max_daily_wall_time_seconds_per_user
    if daily_cap > 0:
        used_today = await _daily_wall_time_seconds(db, user_id)
        if used_today >= daily_cap:
            raise SubmissionError(
                "daily_budget_exhausted",
                "今天的委派任务执行预算已用完；请明天再试或先结束现有任务",
                status_code=429,
            )

    now = datetime.utcnow()
    task_id = str(uuid.uuid4())
    title_text = str(title or "").strip()
    short_title = (title_text or goal_text[:20]).strip() or "语音委派任务"
    if len(short_title) > 40:
        short_title = short_title[:40]
    conv_title = f"{assistant.name} · {short_title}"[:120]

    conversation = Conversation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        assistant_id=assistant.id,
        title=conv_title,
    )
    db.add(conversation)
    await db.flush()

    goal_msg = Message(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        role="user",
        content=goal_text,
    )
    db.add(goal_msg)
    await db.flush()

    iterations_max = min(
        int(config.agent_tool_loop_max_iterations or 50),
        config.voice_delegation_iterations_cap,
    )
    wall_time = config.voice_delegation_wall_time_seconds
    manifest = build_execution_manifest(
        assistant=assistant,
        endpoint=endpoint,
        reasoning=reasoning,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        context_cutoff=now.isoformat(),
        workspace_task_root=f"agent_tasks/{task_id}",
        iterations_max=iterations_max,
        wall_time_seconds=wall_time,
    )
    runtime_meta = {
        "endpoint_alias": endpoint.alias,
        "endpoint_provider_type": endpoint.provider_type,
        "requested_reasoning_effort": reasoning.effort,
        "reasoning_policy": reasoning.policy,
        "target_assistant_id": assistant.id,
        "target_assistant_name": assistant.name,
    }
    task = AgentTask(
        id=task_id,
        user_id=user_id,
        conversation_id=None,  # legacy column stays unused in separate mode
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        execution_conversation_id=conversation.id,
        goal_message_id=goal_msg.id,
        execution_mode=SEPARATE_MODE,
        origin_kind=origin_kind,
        assistant_id=assistant.id,
        title=short_title,
        task_type=VOICE_DELEGATION_TASK_TYPE,
        goal=goal_text,
        context=None,
        status="pending",
        progress=0.0,
        iterations_done=0,
        iterations_max=iterations_max,
        execution_config=json.dumps(manifest, ensure_ascii=False),
        idempotency_key=idem_key,
        request_fingerprint=fingerprint,
        status_version=1,
        reasoning_status="requested",
        runtime_meta=json.dumps(runtime_meta, ensure_ascii=False),
        usage_meta=json.dumps({"attempts": 0, "tokens": {}}, ensure_ascii=False),
        workspace_mode=workspace_mode,
        workspace_meta=workspace_meta,
    )
    db.add(task)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _find_idempotent(db, user_id, idem_key)
        if existing is None:
            raise
        return _dedupe_result(
            existing, fingerprint=fingerprint,
            assistant_name=_assistant_name_from_manifest(existing, assistant.name),
        )

    await db.refresh(task)
    logger.info(
        "Voice delegation submitted: task=%s C1=%s C2=%s assistant=%s reasoning=%s/%s",
        task_id, source_conversation_id, conversation.id, assistant.name,
        reasoning.policy, reasoning.effort,
    )
    return {
        "status": "pending",
        "task_id": task.id,
        "execution_conversation_id": conversation.id,
        "assistant_name": assistant.name,
        "reasoning_policy": reasoning.policy,
        "requested_reasoning_effort": reasoning.effort,
        "deduplicated": False,
    }


def _assistant_name_from_manifest(task: AgentTask, fallback: str) -> str:
    try:
        manifest = json.loads(task.execution_config or "{}")
        return str((manifest.get("assistant") or {}).get("name") or fallback)
    except (TypeError, json.JSONDecodeError):
        return fallback

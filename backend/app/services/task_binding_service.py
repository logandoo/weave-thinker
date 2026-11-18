# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Conversation → WorkspaceTask 的绑定与路由（2026-11-10 波 · T4）。

设计契约 §6：

* 优先级：已有绑定续用 > 用户显式选择（仅未绑定会话）> 高置信延续意图 > 新空 Task >
  歧义只问一次；**默认不自动恢复旧 Task**（阈值未校准前 `recover_auto=false`）；
* 改绑是显式 CAS 操作（`expected_binding_version`），活跃 run 期间禁止；
* 每次改绑递增 version、追加 `workspace_binding_events`，使旧 run 的后续写入失效；
* 逐消息 `workspace_message_contexts` 快照：改绑不能追溯改写历史消息的来源与策略；
* 阶段 A 的延续检索只读元数据（title/goal/summary/时间/manifest），**不读文件正文**。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import inspect, select

from app.core.config import get_config
from app.db.database import (
    Conversation,
    WorkspaceBinding,
    WorkspaceBindingEvent,
    WorkspaceMessageContext,
)
from app.services import workspace_scope as ws
from app.services import workspace_task_service as svc

logger = logging.getLogger(__name__)

# 延续意图的置信阈值（设计 §6.1：阈值通过评测校准；未校准前 recover_auto 保持关闭）
RECOVER_MIN_SCORE = 0.55
RECOVER_MARGIN = 0.12
AMBIGUOUS_MARGIN = 0.05

# 占位标题：这些值**不代表用户起的名字**，因此允许被会话标题改写
# （conversations.title 的 DB 默认是「新对话」；bind 兜底「新任务」；
#   create_task 兜底「未命名任务」；标题生成兜底「未命名会话」；
#   语音链路自建「新语音对话」并以「语音会话」兜底 —— 评审 r1 Important-4）。
PLACEHOLDER_TASK_TITLES = frozenset({"新对话", "新任务", "未命名任务", "未命名会话",
                                     "新语音对话", "语音会话"})

# 自动任务一次性命名的摘要长度（DESIGN_20261115 §4.3：≤24 字）
AUTO_TASK_TITLE_MAX = 24


def _auto_task_title(hint: str | None) -> str:
    """首条用户消息 → 一次性任务名（≤24 字，压平空白，去掉纯占位词）。

    只用于**创建时**；之后任务标题独立，不跟随会话改名（§4.3）。
    """
    text = " ".join(str(hint or "").split())
    if not text:
        return ""
    text = text[:AUTO_TASK_TITLE_MAX].strip()
    if not text or text in PLACEHOLDER_TASK_TITLES:
        return ""
    return text


@dataclass(frozen=True)
class ScopingSettings:
    mode: str = "off"
    supported_modes: tuple[str, ...] = ("chat",)
    recover_auto: bool = False
    classify_new: bool = True
    inbox_project_name: str = "默认项目"
    default_ingest: str = "user_facts"
    staging_lease_seconds: int = 86400


def _settings() -> ScopingSettings:
    cfg = get_config()
    return ScopingSettings(
        mode=cfg.workspace_task_scoping_mode,
        supported_modes=tuple(cfg.workspace_task_scoping_supported_modes),
        recover_auto=cfg.workspace_task_recover_auto,
        classify_new=bool(getattr(cfg, "workspace_task_classify_new", True)),
        inbox_project_name=cfg.workspace_inbox_project_name,
        default_ingest=cfg.workspace_task_default_ingest,
        staging_lease_seconds=cfg.workspace_staging_lease_seconds,
    )


@dataclass(frozen=True)
class TaskCandidate:
    task_id: str
    project_id: str
    title: str
    score: float
    reason: str
    last_active_at: str | None = None


@dataclass
class BindingResolution:
    binding: WorkspaceBinding
    view: ws.WorkspaceBindingView
    action: str                      # reuse | bound | created | ambiguous
    candidates: list[TaskCandidate] = field(default_factory=list)
    question: str | None = None
    task_root_rel: str = ""
    # 2026-11-14 v2：新会话归类建议（不静默绑定——本轮仍进默认项目，UI 询问用户）
    suggested_task: TaskCandidate | None = None


# --- 读 -----------------------------------------------------------------------

async def get_binding(db, *, user_id: str, conversation_id: str,
                      for_update: bool = False) -> WorkspaceBinding | None:
    stmt = select(WorkspaceBinding).where(WorkspaceBinding.conversation_id == conversation_id,
                                          WorkspaceBinding.user_id == user_id)
    if for_update:
        # 改绑 CAS 的行锁：并发两个 rebind 不能都读到同一 binding_version 再各自 +1
        # （否则同一 version 对两个任务都成立，使 assert_write_epoch 的等值校验形同虚设）。
        # populate_existing=True 必带：FOR UPDATE 重读若走 identity map 会原样返回**已加载的旧对象**
        # （评审 r2 Important：实测锁读返回 v=3 而行已为 4），CAS 仍拿改绑前的版本比较 → 洞没堵上。
        stmt = stmt.with_for_update()
        res = await db.execute(stmt, execution_options={"populate_existing": True})
    else:
        res = await db.execute(stmt)
    return res.scalar_one_or_none()


async def binding_history(db, *, user_id: str, conversation_id: str,
                          limit: int = 50) -> list[WorkspaceBindingEvent]:
    res = await db.execute(
        select(WorkspaceBindingEvent)
        .where(WorkspaceBindingEvent.conversation_id == conversation_id,
               WorkspaceBindingEvent.user_id == user_id)
        .order_by(WorkspaceBindingEvent.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return list(res.scalars().all())


async def message_contexts_for_task(db, *, user_id: str, task_id: str,
                                    limit: int = 500) -> list[str]:
    """scoped `session_search` 的限定集：该 Task 已快照过的消息 id。"""
    res = await db.execute(
        select(WorkspaceMessageContext.message_id)
        .where(WorkspaceMessageContext.user_id == user_id,
               WorkspaceMessageContext.task_id == task_id)
        .limit(max(1, int(limit)))
    )
    return [r for r in res.scalars().all() if r]


async def message_context(db, *, message_id: str) -> WorkspaceMessageContext | None:
    res = await db.execute(
        select(WorkspaceMessageContext).where(WorkspaceMessageContext.message_id == message_id)
    )
    return res.scalar_one_or_none()


# --- 写 -----------------------------------------------------------------------

async def record_message_context(db, *, message_id: str, user_id: str, task_id: str,
                                 run_id: str | None, attempt_id: str | None,
                                 binding_version: int,
                                 system_memory_ingest: str | None) -> WorkspaceMessageContext:
    """逐消息快照（幂等：同一 message 重复记录取最后一次，不新增行）。"""
    res = await db.execute(
        select(WorkspaceMessageContext).where(WorkspaceMessageContext.message_id == message_id)
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = WorkspaceMessageContext(
            message_id=message_id, user_id=user_id, task_id=task_id, run_id=run_id,
            attempt_id=attempt_id, binding_version=int(binding_version),
            system_memory_ingest=system_memory_ingest,
        )
        db.add(row)
        await db.flush()
        return row
    # 已快照 = 该消息发生时的权威来源，**永不覆盖**（§8.5「不能以改绑后的当前
    # Task 反推历史」）。重复记录只在同值时幂等，异值时告警并保留首值。
    if (str(row.task_id) != str(task_id) or int(row.binding_version) != int(binding_version)):
        logger.warning("workspace message context %s already recorded for task %s v%s; "
                       "ignoring rewrite to task %s v%s", message_id, row.task_id,
                       row.binding_version, task_id, binding_version)
    return row


async def _append_event(db, *, conversation_id: str, user_id: str, from_task_id: str | None,
                        to_task_id: str | None, from_version: int | None, to_version: int | None,
                        actor: str, reason: str, system_memory_ingest: str | None,
                        run_id: str | None = None) -> None:
    db.add(WorkspaceBindingEvent(
        conversation_id=conversation_id, user_id=user_id, from_task_id=from_task_id,
        to_task_id=to_task_id, from_version=from_version, to_version=to_version, run_id=run_id,
        actor=actor, reason=str(reason or "")[:1000] or None,
        system_memory_ingest=system_memory_ingest,
    ))
    await db.flush()


async def _reset_deathmatch_snapshot(db, *, conversation_id: str, user_id: str,
                                     from_task_id: str | None, to_task_id: str | None,
                                     from_version: int | None, to_version: int | None,
                                     system_memory_ingest: str | None,
                                     trigger: str) -> bool:
    """换根（首绑/改绑）后清空验证快照 + 追加事件（WP2 行为 1）。

    不清空 → 下一回合拿旧根的 `workspace_files` 比新根 → 完整性违规 +
    已定案步骤翻案 + claim 撤回（B2）。非死磕会话不写事件。
    """
    from app.db.database import Conversation
    from app.services.deathmatch_verification import reset_verification_snapshot

    conv = await db.get(Conversation, conversation_id)
    if conv is None or not bool(getattr(conv, "deathmatch_mode", False)):
        return False
    if not reset_verification_snapshot(conv, reason=f"binding_change:{trigger}"):
        return False
    await _append_event(
        db, conversation_id=conversation_id, user_id=user_id,
        from_task_id=from_task_id, to_task_id=to_task_id,
        from_version=from_version, to_version=to_version,
        actor="system", reason="dm_snapshot_reset",
        system_memory_ingest=system_memory_ingest)
    return True


async def _park_active_runs(db, *, conversation_id: str) -> int:
    """显式改绑前停泊该会话的活跃 run（§6.3「先 stop/park 再改绑」的自动化，2026-11-16 Wave D）。

    用户裁定：项目/任务管理是用户主权，绝不因「有进行中的 run」被拒。park_run 封存
    current attempt，配合 rebind 的 binding_version 递增，使旧 attempt 的后续写入在
    `assert_write_epoch` 处 fail-closed（CODE_ATTEMPT_STALE / CODE_BINDING_STALE）——
    安全底座不削弱，只是把「先停」这一步从甩给用户改成服务端自动完成。返回停泊数。
    """
    from app.db.database import WorkspaceRun
    from app.services.workspace_run_service import ACTIVE_RUN_STATUSES, park_run

    res = await db.execute(
        select(WorkspaceRun.id).where(
            WorkspaceRun.conversation_id == conversation_id,
            WorkspaceRun.status.in_(ACTIVE_RUN_STATUSES)))
    parked = 0
    for (rid,) in res.all():
        run = await park_run(db, run_id=rid)
        if str(run.status) == "parked":   # 只数真正停泊的（park_run 对终态 run 幂等跳过）
            parked += 1
    return parked


async def rebind(db, *, user_id: str, conversation_id: str, task_id: str,
                 expected_binding_version: int, actor: str = "user",
                 reason: str = "") -> WorkspaceBinding:
    """显式改绑：CAS + 活跃 run 自动停泊 + 事件追加（**本服务函数不移动文件**）。

    文件归属：改绑后若源任务变空，由**调用方**（HTTP 端点 / agent 工具）在 commit 之后调
    `workspace_task_lifecycle_service.migrate_orphaned_task_files` 迁移孤儿文件；本函数保持
    「不搬文件」语义，使逐消息来源快照与跨任务产出视图的既有测试不受影响（2026-11-18 issue4）。

    2026-11-16 Wave D：活跃 run 不再阻断改绑——服务端自动 park 后再 CAS（§6.3「先停再改」
    自动化）。park_run 封存 attempt + binding_version 递增使旧 attempt 写入 fail-closed。
    返回的 binding 带 transient `_parked_active_runs`（停泊数）供 API/工具透出。
    """
    current = await get_binding(db, user_id=user_id, conversation_id=conversation_id,
                                for_update=True)
    if current is None:
        # 歧义选择器闭环（§6.1「只问一次」）：未绑定会话允许 expected=0 首绑，
        # CAS 语义 =「当前必须无绑定」；并发双选只有一个成功，另一个按 stale 处理。
        if int(expected_binding_version) == 0:
            # 纵深防御（评审 r3 #1）：首绑会**插入新行**，必须校验会话归属，
            # 否则任意用户可向他人会话占坑（conversation_id UNIQUE → 永久 DoS）。
            from app.db.database import Conversation

            conv = await db.get(Conversation, conversation_id)
            if conv is None or str(conv.user_id) != str(user_id):
                raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE,
                                             "conversation not found for user", conversation_id)
            task = await svc.get_task(db, user_id=user_id, task_id=task_id)
            if task is None:
                raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found", str(task_id))
            if str(task.status or "active") in ("archived", "deleted"):
                raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                             "cannot bind to archived/deleted task", str(task_id))
            # 评审 r2 Important-1：首绑也可能遇到「有活跃 run 但无绑定」的会话（resolve 会
            # 删除指向已归档/删除任务的绑定，留下孤儿活跃 run）。不 park 的话，新绑定 v1 与
            # 旧 run 的 binding_version=1 相等 → assert_write_epoch 的等值校验放行 → 旧回合
            # 继续写旧任务。首绑前先停泊该会话活跃 run（正常首绑无 run，幂等空操作）。
            parked = await _park_active_runs(db, conversation_id=conversation_id)
            new_binding = await _bind(db, user_id=user_id, conversation_id=conversation_id,
                                      task=task, existing=None, actor=actor,
                                      reason=reason or "user_choice")
            new_binding._parked_active_runs = parked
            return new_binding
        raise ws.WorkspaceScopeError(ws.CODE_BINDING_STALE,
                                     "conversation has no binding to rebind", conversation_id)
    if int(current.binding_version) != int(expected_binding_version):
        raise ws.WorkspaceScopeError(
            ws.CODE_BINDING_STALE,
            f"binding_version {current.binding_version} != expected {expected_binding_version}",
            conversation_id)
    if str(current.task_id) == str(task_id):
        return current   # 同任务幂等：不触碰活跃 run

    # 2026-11-16 Wave D（用户裁定 issue4/5：项目/任务管理是用户主权，绝不因「有进行中的
    # run」被拒）。原 `_has_active_run → CODE_RUN_ACTIVE` 拒绝让 UI 改绑遇卡死 running run
    # 永久失败，且 agent 改绑（永远发生在自己的活跃 run 内）恒失败。改为自动停泊活跃 run
    # 后继续 CAS：park_run 封存 attempt + 下面 binding_version 递增，使旧 attempt 的后续
    # 写入在 assert_write_epoch 处 fail-closed（安全底座不削弱，只是把「先停」自动化）。
    current._parked_active_runs = await _park_active_runs(db, conversation_id=conversation_id)
    # D-5（CE-2 修订，评审 r2 Critical）：**不**改写 deathmatch_status。
    # deathmatch_status 是控制字段（deathmatch_service.DEATHMATCH_CONTROL_FIELDS），从改绑
    # 这条外部事务写它会被 goal-loop 的 merge_sync 冻结整条流 → 循环的 done/partial 终局写
    # 被丢弃 → 用户下次发消息 resume 到「已完成的 goal」= 双跑；且 goal-loop 读的是 detached
    # 内存副本，根本看不到这次外部写。正确语义：goal 随会话迁移到新任务——park 旧 run 让旧
    # 回合写入在围栏处 fail-closed（D-3/D-4），下一回合 attach_chat_scope 见无活跃 run，
    # 在新任务上 mint 新 run 续跑。故此处只停泊、不动状态。

    task = await svc.get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "target task not found for user", task_id)
    if str(task.status or "active") in ("archived", "deleted"):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "target task is archived/deleted", task_id)

    from_version = int(current.binding_version)
    to_version = from_version + 1
    from_task_id = str(current.task_id)
    current.task_id = task.id
    current.project_id = task.project_id
    current.binding_version = to_version
    current.system_memory_ingest = task.system_memory_ingest
    current.bound_by = actor
    current.updated_at = datetime.utcnow()
    await db.flush()
    await _append_event(db, conversation_id=conversation_id, user_id=user_id,
                        from_task_id=from_task_id,
                        to_task_id=task.id, from_version=from_version, to_version=to_version,
                        actor=actor, reason=reason, system_memory_ingest=task.system_memory_ingest)
    # WP2：换根即重置死磕验证快照（旧根的相对路径在新根上没有意义）。
    await _reset_deathmatch_snapshot(
        db, conversation_id=conversation_id, user_id=user_id,
        from_task_id=from_task_id, to_task_id=task.id,
        from_version=from_version, to_version=to_version,
        system_memory_ingest=task.system_memory_ingest,
        trigger=f"rebind:{reason or actor}")
    return current


# --- 阶段 A：元数据检索（不读文件正文）---------------------------------------

def _score(query: str, *, title: str, goal: str | None, summary: str | None,
           tags: str | None) -> tuple[float, str]:
    """确定性打分：只按标题/摘要/目标/tags，不做主题分类器。"""
    q = str(query or "").strip().lower()
    if not q:
        return 0.0, ""
    hits: list[str] = []
    score = 0.0
    title_l = str(title or "").lower()
    if q == title_l:
        score += 0.9
        hits.append("标题完全匹配")
    elif q in title_l:
        score += 0.6
        hits.append("标题包含")
    else:
        tokens = [t for t in q.replace(",", " ").split() if len(t) >= 2]
        overlap = [t for t in tokens if t in title_l]
        if overlap:
            score += 0.45
            hits.append("标题词命中:" + "/".join(overlap[:3]))
    for label, text in (("摘要", summary), ("目标", goal), ("标签", tags)):
        low = str(text or "").lower()
        if low and q in low:
            score += 0.2
            hits.append(f"{label}包含")
            break
    return min(1.0, score), "、".join(hits[:3])


async def search_tasks(db, *, user_id: str, query: str, project_id: str | None = None,
                       limit: int = 6) -> list[TaskCandidate]:
    """跨 Task 阶段 A：只返回元数据与匹配原因。"""
    tasks = await svc.list_tasks(db, user_id=user_id, project_id=project_id,
                                 statuses=("active", "completed"), limit=max(limit * 4, 20))
    out: list[TaskCandidate] = []
    for task in tasks:
        score, reason = _score(query, title=task.title, goal=task.goal, summary=task.summary,
                               tags=task.tags_json)
        if score <= 0:
            continue
        out.append(TaskCandidate(
            task_id=task.id, project_id=task.project_id or "", title=task.title or "",
            score=round(score, 3), reason=reason,
            last_active_at=task.last_active_at.isoformat() if task.last_active_at else None,
        ))
    out.sort(key=lambda c: (-c.score, (c.last_active_at or "")), reverse=False)
    return out[:max(1, int(limit))]


# --- 路由主入口 ---------------------------------------------------------------

class TaskBindingService:
    """按设计 §6.1 的优先级解析当前会话应绑定的 Task。"""

    @staticmethod
    async def resolve(db, *, user_id: str, user_root: Path | str, conversation,
                      explicit_task_id: str | None = None,
                      continuation_hint: str | None = None) -> BindingResolution:
        settings = _settings()
        conv_id = str(getattr(conversation, "id", "") or "")
        if not conv_id:
            raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING, "conversation id required", "")

        existing = await get_binding(db, user_id=user_id, conversation_id=conv_id)
        # 2026-11-14 v2（评审 M6）：曾绑定过的会话（含绑定指向已消失任务）不是「新会话」，
        # 不得触发归类建议——否则有历史的会话会被当成新会话打扰。
        had_binding = existing is not None

        # 1) 已有绑定：普通续聊继续同一 Task（改绑必须走 CAS）
        if existing is not None and not explicit_task_id:
            task = await svc.get_task(db, user_id=user_id, task_id=existing.task_id)
            project = await svc.get_project(db, user_id=user_id,
                                            project_id=task.project_id) if task else None
            if task is not None and project is not None and str(task.status) not in ("archived", "deleted"):
                view = ws.build_binding_view(user_id=user_id, user_root=user_root,
                                             project=project, task=task, binding=existing,
                                             conversation_id=conv_id)
                return BindingResolution(existing, view, "reuse", [], None, task.root_rel)
            logger.warning("workspace binding %s points to unusable task %s — creating a fresh task",
                           conv_id, existing.task_id)
            await _append_event(db, conversation_id=conv_id, user_id=user_id,
                                from_task_id=existing.task_id, to_task_id=None,
                                from_version=existing.binding_version, to_version=None,
                                actor="system", reason="binding_task_missing",
                                system_memory_ingest=existing.system_memory_ingest)
            await db.delete(existing)
            await db.flush()
            existing = None

        if explicit_task_id and existing is not None and str(existing.task_id) != str(explicit_task_id):
            # §6.1：显式选择只对「新建/尚未绑定」的会话生效；已绑定必须走 CAS 改绑
            raise ws.WorkspaceScopeError(
                ws.CODE_BINDING_STALE,
                "conversation already bound; use the CAS rebind endpoint", conv_id)

        # 2) 用户显式选择（未绑定会话，最高优先级）
        if explicit_task_id:
            task = await svc.get_task(db, user_id=user_id, task_id=explicit_task_id)
            if task is None:
                raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE,
                                            "task not found for this user", explicit_task_id)
            project = await svc.get_project(db, user_id=user_id, project_id=task.project_id)
            if project is None:
                raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "project not found",
                                             task.project_id or "")
            binding = await _bind(db, user_id=user_id, conversation_id=conv_id, task=task,
                                  existing=existing, actor="user", reason="explicit_choice")
            view = ws.build_binding_view(user_id=user_id, user_root=user_root, project=project,
                                         task=task, binding=binding, conversation_id=conv_id)
            return BindingResolution(binding, view, "bound", [], None, task.root_rel)

        # 3) 明确的延续意图：只检索元数据，且只有唯一高置信候选才建议恢复
        if settings.recover_auto and str(continuation_hint or "").strip():
            candidates = await search_tasks(db, user_id=user_id, query=continuation_hint, limit=6)
            strong = [c for c in candidates if c.score >= RECOVER_MIN_SCORE]
            if len(strong) > 1:
                gap = strong[0].score - strong[1].score
                if gap <= AMBIGUOUS_MARGIN:
                    return BindingResolution(None, None, "ambiguous", strong,
                                             "你想继续哪个任务？（只问一次，选择后才会暴露该任务的文件）")
            if len(strong) == 1 and (len(strong) == len(candidates)
                                     or strong[0].score - (candidates[1].score if len(candidates) > 1 else 0.0)
                                     >= RECOVER_MARGIN):
                chosen = await svc.get_task(db, user_id=user_id, task_id=strong[0].task_id)
                if chosen is not None:
                    project = await svc.get_project(db, user_id=user_id, project_id=chosen.project_id)
                    if project is not None:
                        binding = await _bind(db, user_id=user_id, conversation_id=conv_id,
                                              task=chosen, existing=existing, actor="router",
                                              reason=f"continuation:{strong[0].reason}")
                        view = ws.build_binding_view(user_id=user_id, user_root=user_root,
                                                     project=project, task=chosen, binding=binding,
                                                     conversation_id=conv_id)
                        return BindingResolution(binding, view, "bound", strong, None, chosen.root_rel)

        # 3.5) 归类建议（2026-11-14 v2，用户要求）：新会话首条消息命中**唯一强候选**
        #      任务时，不静默绑定——本轮仍进默认项目，候选作为建议交给 UI 询问用户
        #      （「要归类到该任务吗？」）。判断不了/无候选 → 默认项目（不打扰）。
        suggestion: TaskCandidate | None = None
        if (bool(getattr(settings, "classify_new", True)) and existing is None
                and not had_binding and str(continuation_hint or "").strip()):
            candidates = await search_tasks(db, user_id=user_id, query=continuation_hint, limit=6)
            strong = [c for c in candidates if c.score >= RECOVER_MIN_SCORE]
            if len(strong) == 1:
                runner_up = candidates[1].score if len(candidates) > 1 else 0.0
                if len(strong) == len(candidates) or strong[0].score - runner_up >= RECOVER_MARGIN:
                    suggestion = strong[0]

        # 4) 默认：inbox Project 下的新空 Task（不自动恢复任何旧 Task）
        project = await svc.ensure_inbox_project(db, user_id=user_id, user_root=user_root)
        # 2026-11-15（DESIGN_20261115 §4.3 / 验收 #10）：自动任务**一次性**取首条
        # 用户消息摘要（≤24 字）作为初始名，origin='auto'；之后不跟随会话改名。
        title = _auto_task_title(continuation_hint) or "未命名任务"
        task = await svc.create_task(db, user_id=user_id, user_root=user_root,
                                     project_id=project.id, title=title, origin="auto")
        binding = await _bind(db, user_id=user_id, conversation_id=conv_id, task=task,
                              existing=existing, actor="system", reason="new_conversation")
        view = ws.build_binding_view(user_id=user_id, user_root=user_root, project=project,
                                     task=task, binding=binding, conversation_id=conv_id)
        return BindingResolution(binding, view, "created", [], None, task.root_rel,
                                 suggested_task=suggestion)


async def bind_new_task(db, *, user_id: str, user_root, conversation_id: str,
                        title: str = "", actor: str = "user") -> WorkspaceBinding:
    """歧义「开新任务」：在默认项目下建空 Task 并绑定。

    幂等：会话已有绑定时原样返回（不新建、不改绑）——「只问一次」的落地面，
    绑定后下一回合 resolve 直接 reuse。"""
    existing = await get_binding(db, user_id=user_id, conversation_id=conversation_id)
    if existing is not None:
        return existing
    project = await svc.ensure_inbox_project(db, user_id=user_id, user_root=user_root)
    task = await svc.create_task(db, user_id=user_id, user_root=user_root,
                                 project_id=project.id, title=(title or "新任务")[:300],
                                 origin="user")
    try:
        return await _bind(db, user_id=user_id, conversation_id=conversation_id, task=task,
                           existing=None, actor=actor, reason="user_new_task")
    except ws.WorkspaceScopeError as exc:
        if exc.code != ws.CODE_BINDING_STALE:
            raise
        # 并发「开新任务」败者：认领胜者绑定，并回收本次刚建的空 Task
        # （行 + 磁盘目录都要清，否则留下不可追踪目录，评审 r3 #2）。
        winner = await get_binding(db, user_id=user_id, conversation_id=conversation_id)
        if winner is None:
            raise
        await db.delete(task)
        await db.flush()
        import shutil

        from pathlib import Path as _P

        try:
            loser_dir = _P(str(user_root)) / _P(*task.root_rel.split("/"))
            if loser_dir.is_dir():
                shutil.rmtree(loser_dir, ignore_errors=True)
        except OSError:
            logger.warning("bind_new_task loser cleanup failed for %s", task.root_rel)
        return winner


async def sync_task_title_from_conversation(db, *, user_id: str, conversation_id: str,
                                            previous_title: str | None,
                                            new_title: str | None) -> bool:
    """**已停用**（2026-11-15 任务管理中心 · DESIGN_20261115 §4.3 / 验收 #10）。

    任务标题**不再持续镜像会话标题**：自动任务在创建时一次性取首条用户消息摘要
    作为初始名（`origin='auto'`），之后由用户/agent 独立改名。这里保留函数签名
    作为兼容空操作，返回 False（未写入），调用点无需改动。

    旧行为（2026-11-14 缺陷波）用镜像解决「任务永远叫新对话」；新设计用
    **创建时一次性命名**解决同一问题，且不再让会话改名污染任务标题。
    """
    return False


async def sync_task_title_after_rename(db, *, conversation,
                                       previous_title: str | None = None,
                                       new_title: str | None = None,
                                       user_intent: bool = False) -> bool:
    """**已停用**（同 `sync_task_title_from_conversation`）：兼容空操作，恒返回 False。

    保留包装是因为会话 PUT / 聊天终态 / 语音 / worker 多处调用它；删除调用点会
    扩大回归面，而空操作语义与「任务标题独立」完全一致。
    """
    return False


async def _bind(db, *, user_id: str, conversation_id: str, task, existing: WorkspaceBinding | None,
                actor: str, reason: str) -> WorkspaceBinding:
    if existing is not None and str(existing.task_id) == str(task.id):
        return existing
    if existing is not None:
        return await rebind(db, user_id=user_id, conversation_id=conversation_id, task_id=task.id,
                            expected_binding_version=existing.binding_version, actor=actor,
                            reason=reason)
    binding = WorkspaceBinding(
        conversation_id=conversation_id, user_id=user_id, task_id=task.id,
        project_id=task.project_id, binding_version=1,
        system_memory_ingest=task.system_memory_ingest, bound_by=actor,
    )
    db.add(binding)
    from sqlalchemy.exc import IntegrityError

    try:
        # savepoint：并发首绑撞 conversation_id UNIQUE 时只回滚本插入，
        # 外层事务可继续（bind_new_task 认领胜者要用）。
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ws.WorkspaceScopeError(
            ws.CODE_BINDING_STALE,
            "conversation was bound concurrently; refresh and retry", conversation_id) from exc
    await _append_event(db, conversation_id=conversation_id, user_id=user_id,
                        from_task_id=None, to_task_id=task.id, from_version=0, to_version=1,
                        actor=actor, reason=reason, system_memory_ingest=task.system_memory_ingest)
    # WP2：首绑同样换根（旧实现只守 rebind，首绑分支绕过 active-run 守卫）。
    await _reset_deathmatch_snapshot(
        db, conversation_id=conversation_id, user_id=user_id,
        from_task_id=None, to_task_id=task.id, from_version=0, to_version=1,
        system_memory_ingest=task.system_memory_ingest,
        trigger=f"bind:{reason or actor}")
    return binding


__all__ = ["AMBIGUOUS_MARGIN", "BindingResolution", "RECOVER_MARGIN", "RECOVER_MIN_SCORE",
           "TaskBindingService", "TaskCandidate", "bind_new_task", "binding_history", "get_binding",
           "message_context", "message_contexts_for_task", "record_message_context",
           "rebind", "search_tasks"]

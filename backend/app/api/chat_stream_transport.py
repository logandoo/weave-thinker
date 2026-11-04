# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Chat SSE transport (C-02 move-only, 2026-10-19).

The whole SSE/stream transport (session setup, event generator, finalization)
was moved byte-for-byte out of ``app/api/chat.py``.  ``chat_stream`` keeps only
the route decorator/signature and delegates here.

Module-level names owned by ``app/api/chat.py`` are synced at call time (not
copied at import time) so existing monkeypatches of ``chat._agent_registry`` /
``chat.agent_service`` keep working.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sse_starlette.sse import EventSourceResponse

from app.api.chat_persistence import interjection_anchor_from_buffer, interjection_items_from_buffer
from app.api.interjection_display import (
    SEGMENT_SEPARATOR,
    commit_prepared_item,
    prepare_interjection_item,
    resolve_persisted_display_sequence,
)

_CHAT_GLOBALS = (   'AgentLoop',
    'Assistant',
    'AsyncSessionLocal',
    'ChatSession',
    'Conversation',
    'DeathmatchManager',
    'EventSourceResponse',
    'HTTPException',
    'MARKER_RE',
    'Message',
    'MessageAttempt',
    'PartTranslator',
    'PreToolGate',
    'ProgressiveFlusher',
    'TitleGeneratorService',
    'User',
    '_DETACHED_AGENT_TASKS',
    '_agent_registry',
    '_build_content_fallback',
    '_clarification_tasks',
    '_collect_download_attachments',
    '_compose_terminal_content',
    '_delete_empty_streaming_row',
    '_display_sequence_keep_evidence',
    '_last_measured_prompt_tokens',
    '_load_conversation_messages',
    '_load_identity_memory_context',
    '_localize_media_for_persist',
    '_persist_deathmatch_failure',
    '_re',
    '_render_answer_citations',
    '_resolve_execution_target_for_conversation',
    '_sanitize_cited_content',
    'strip_stop_citation_echo',
    '_save_assistant_terminal',
    '_split_pre_tool_prose',
    '_parked_prose_item',
    '_strip_dsml_all',
    '_strip_leading_orphan_colon',
    '_strip_leading_orphan_punct',
    '_track_bg_task',
    '_track_detached_task',
    '_transform_tool_loop_results',
    '_trim_messages_for_edit',
    '_trim_messages_for_regeneration',
    'agent_service',
    'asyncio',
    'build_force_injected_skills_prompt',
    'build_skills_system_prompt',
    'datetime',
    'ensure_user_workspace',
    'extract_search_query_and_note_context',
    'get_config',
    'json',
    'logger',
    'registry',
    'resolve_skill',
    'sa_delete',
    'sa_update',
    'scrub_reasoning_meta',
    'scrub_reasoning_streaming',
    'select',
    'should_use_custom_model',
    'stream_buffer_manager',
    'time')


# 2026-11-03: turn-end browser teardown. The close is scheduled as its own
# task so it still runs when the agent task is cancelled (finally would
# otherwise be interrupted before the recorder stops).
_BROWSER_CLOSE_TASKS: set = set()
_module_logger = logging.getLogger(__name__)


def _schedule_browser_close(conversation_id: str, turn_token: str | None = None) -> None:
    async def _run() -> None:
        try:
            from app.services.interactive_browser_service import InteractiveBrowserService
            await InteractiveBrowserService.get_instance().close_for_turn_end(
                conversation_id, turn_token=turn_token
            )
        except Exception:
            _module_logger.debug(
                "turn-end browser close failed for %s", conversation_id, exc_info=True
            )

    try:
        task = asyncio.create_task(_run(), name=f"browser-close-{conversation_id[:8]}")
        _BROWSER_CLOSE_TASKS.add(task)
        task.add_done_callback(_BROWSER_CLOSE_TASKS.discard)
    except Exception:
        _module_logger.debug("turn-end browser close scheduling failed", exc_info=True)


def _begin_browser_turn() -> tuple[Any, Any]:
    """Start browser-turn ownership; fail-open (never kills a turn)."""
    try:
        from app.services.interactive_browser_service import BROWSER_TURN_TOKEN
        token = f"turn:{uuid.uuid4().hex}"
        return token, BROWSER_TURN_TOKEN.set(token)
    except Exception:
        _module_logger.debug("browser turn token unavailable", exc_info=True)
        return None, None


def _end_browser_turn(conversation_id: str, token: Any, handle: Any) -> None:
    if token:
        _schedule_browser_close(conversation_id, token)
    if handle is not None:
        try:
            from app.services.interactive_browser_service import BROWSER_TURN_TOKEN
            BROWSER_TURN_TOKEN.reset(handle)
        except Exception:
            pass


async def _run_agent_loop_task(impl, conversation_id, agent_state, registry):
    """Outer safety wrapper for the agent-loop task (conv 5228db60).

    ``task.cancel()`` landing before the impl's main try/finally used to skip
    the whole CancelledError chain and leak the adopted slot. This wrapper
    moves the cleanup finally to the coroutine's first line; identity-checked,
    so a duplicate inner call is a no-op.

    2026-11-03: also owns the browser/recorder teardown for the turn.
    """
    _browser_turn, _browser_token = _begin_browser_turn()
    try:
        await impl()
    except asyncio.CancelledError:
        if getattr(agent_state, "is_running", False):
            agent_state.is_running = False
            try:
                await agent_state.broadcast_done()
            except Exception:
                pass
            logger.info(
                "Stop-cancel escape-window conv=%s (pre-main-try cancel)",
                conversation_id,
            )
        raise
    except Exception:
        if getattr(agent_state, "is_running", False):
            agent_state.is_running = False
            logger.exception(
                "Agent loop impl died before main-try for conv=%s",
                conversation_id,
            )
        raise
    finally:
        # Scheduled (not awaited) so a cancellation cannot skip teardown;
        # the token prevents closing a background/newer-turn session.
        _end_browser_turn(conversation_id, _browser_turn, _browser_token)
        try:
            await registry.unregister(conversation_id, expected=agent_state)
        except Exception:
            pass


def _sync_chat_globals() -> None:
    from app.api import chat as _chat

    g = globals()
    for name in _CHAT_GLOBALS:
        if hasattr(_chat, name):
            g[name] = getattr(_chat, name)


@dataclass
class _ChatStreamState:
    _dm_terminal_status_saved: Any = None
    _injected_mem_ids: Any = None
    _stream_buf: Any = None
    client_disconnected: Any = None
    detached_agent_task: Any = None
    full_reasoning: Any = None
    full_response: Any = None
    message_count: Any = None
    response_saved: Any = None
    sse_cancelled_flag: Any = None
    tool_results_json: Any = None


@dataclass
class _AgentLoopRun:
    """Mutable per-run accumulator/context for one detached agent-loop run.

    C-02c: the event bridge, finalize and cancel paths share this object so the
    per-run state no longer travels as closure cells through a 1.6k-line
    function.  Field names mirror the former local variables 1:1.
    """

    assistant_content: str = ""
    # 2026-11-02（conv 861cb5af）：工具边界会把 assistant_content 清空（预工具
    # 散文只进时间线、不进终局答案）——但**用户已经看到**，必须仍能落库。
    # 这里累积所有已展示的散文；渐进落库写 visible_prose + assistant_content。
    visible_prose: str = ""
    assistant_reasoning: str = ""
    current_reasoning_segment: str = ""
    content_segments: list = field(default_factory=list)
    display_sequence: list = field(default_factory=list)
    interjection_items: list = field(default_factory=list)
    tool_results_accumulated: list = field(default_factory=list)
    tool_call_events_accumulated: list = field(default_factory=list)
    search_queries_used: list = field(default_factory=list)
    search_queries_by_call: dict = field(default_factory=dict)
    last_streamed_attachments: list = field(default_factory=list)
    dm_final_attachments: list = field(default_factory=list)
    dm_last_turn_attachments: list = field(default_factory=list)
    stitch_active: bool = False
    stitch_base: str = ""
    canary_tail: str = ""
    reasoning_canary_tail: str = ""
    reasoning_meta_tail: str = ""
    loop_error: str | None = None
    streaming_row_id: str | None = None
    dm_sync_snapshot: dict = field(default_factory=dict)
    dm_sync_frozen: frozenset = frozenset()
    pre_tool_gate: Any = None
    pf: Any = None
    state: Any = None
    agent_state: Any = None
    agent_queue: Any = None
    dm: Any = None
    put: Any = None
    conversation_id: str = ""
    cap_conversation_id: str = ""
    cap_assistant: Any = None
    user_id_local: Any = None
    username_local: Any = None
    ctx_window: int = 0
    agent_loop: Any = None
    messages_for_model: Any = None
    task_user: Any = None
    task_conversation: Any = None
    coord_result: Any = None
    conversation_messages: Any = None
    ttft_t0: float = 0.0
    strip_canary_streaming: Any = None
    sse_cancelled: Any = None
    generate_title_bg: Any = None
    persist_final_answer: Any = None


def _replace_deathmatch_plan_directive(messages: list[dict], directive: str) -> None:
    """Replace tagged synthetic directives; preserve lookalike user text."""
    messages[:] = [
        message for message in messages
        if not (
            isinstance(message, dict)
            and message.get("_deathmatch_plan_directive") is True
        )
    ]
    if directive:
        messages.insert(1, {
            "role": "user",
            "content": directive,
            "_deathmatch_plan_directive": True,
        })


def _rebind_deathmatch_manager_conversation(dm_mgr, conversation) -> None:
    """Rebind a manager to a fresh ORM row with its pre-mutation CAS baseline."""
    from app.services.deathmatch_write_through import _contract_snapshot
    conversation._dm_write_contract_baseline = _contract_snapshot(conversation)
    conversation._dm_write_stale = False
    dm_mgr._conv = conversation


async def _commit_deathmatch_contract(db, conversation, *, baseline_conversation=None) -> None:
    """Commit own state, then advance CAS to exactly the committed snapshot.

    A failed commit leaves the old baseline untouched. External writers still
    fail the row-locked comparison; only this task's successful commit rebases.
    """
    from app.services.deathmatch_write_through import _contract_snapshot
    committed = _contract_snapshot(conversation)
    await db.commit()
    qa = committed.get("deathmatch_grilling_qa_history") or []
    logging.getLogger(__name__).info(
        "deathmatch lazy contract commit conv=%s version=%s pending=%s qa_len=%s",
        getattr(conversation, "id", None), committed.get("deathmatch_plan_version"),
        [
            entry.get("goal_contract_pending")
            for entry in qa if isinstance(entry, dict)
            and "goal_contract_pending" in entry
        ],
        len(qa),
    )
    owner = baseline_conversation if baseline_conversation is not None else conversation
    owner._dm_write_contract_baseline = committed


def _is_substantive_resume_correction(text: str, *, goal: str = "") -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    kickoff_prefix = "目标已明确，请开始执行"
    if value.startswith(kickoff_prefix):
        # Exclude the automatic kickoff even for a legacy goal that is no
        # longer current. Only exact-goal-plus-new-content is substantive.
        if not goal:
            return False
        automatic = f"{kickoff_prefix}：{goal}"
        # Only the exact current-goal kickoff is provably synthetic. A message
        # that changes the goal text must remain visible as a correction.
        return value != automatic
    if "死磕模式 — 继续推进目标" in value[:80]:
        return False
    bare = value.lower().strip(" \t\r\n。.!！?？")
    return bare not in {
        "继续", "接着", "继续推进", "接着做", "请继续",
        "继续执行", "继续吧", "接着继续", "接着执行", "请继续执行",
        "继续推进目标", "继续执行计划",
        "resume", "continue", "go on", "resume please", "please resume",
        "continue please", "please continue", "go on please", "keep going", "go ahead",
    }


async def _resume_deathmatch_from_paused(dm_mgr, db, latest_user_query: str) -> None:
    """Consume a real resume correction before criteria can be regenerated."""
    if (
        getattr(dm_mgr._conv, "deathmatch_status", None) in ("paused", "human_gate")
        and _is_substantive_resume_correction(
            latest_user_query, goal=str(dm_mgr._conv.deathmatch_goal or "")
        )
    ):
        await dm_mgr.incorporate_resume_correction(db, latest_user_query)
    dm_mgr.resume()


def _make_act_gate_approval_callback(permission_cb):
    """P1b: wrap the chat permission callback so only act-gate-marked requests
    can use it.  A generic/background callback accidentally wired here fails
    closed instead of auto-approving an act tool."""
    async def _act_gate_permission_request_callback(
        conversation_id: str,
        tool_name: str,
        description: str,
        details: dict,
    ) -> bool:
        if not (details or {}).get("act_gate"):
            return False
        return await permission_cb(
            conversation_id=conversation_id,
            tool_name=tool_name,
            description=description,
            details=details,
        )
    return _act_gate_permission_request_callback


async def _handle_dm_verdict_event(run, verdict):
    """Per-turn deathmatch verdict handling: broadcast the verdict, optimistic state merge, per-turn message persistence, continuation/status messages and accumulator reset.

    C-02c: extracted verbatim from `_drive_agent_events`."""
    await run.put({"deathmatch_verdict": verdict})
    # Capture final deliverable attachments (set
    # when the goal is deemed complete) so they
    # can be attached to the final summary message.
    _new_att = verdict.get("final_attachments") or []
    if _new_att:
        run.dm_final_attachments = _new_att
    # _dm_last_turn_attachments is captured during
    # per-turn persistence above.
    try:
        from app.services.deathmatch_write_through import drain_write_through
        # A write-through chain link commits to DB before it advances the
        # detached runner's CAS baseline. Never judge freshness inside that
        # window: our own commit would look like an external G2, mark the
        # runner stale, and permanently block finalize_close_out step writes.
        await drain_write_through(run.conversation_id)
        async with AsyncSessionLocal() as _dm_db:
            # Keep the freshness check, merge and commit in one database-row
            # critical section. Otherwise an external G2 can commit after the
            # snapshot comparison and then be overwritten by this stale merge.
            _dm_result = await _dm_db.execute(
                select(Conversation)
                .where(Conversation.id == run.conversation_id)
                .with_for_update()
            )
            _dm_conv = _dm_result.scalar_one_or_none()
            if _dm_conv:
                from app.services.deathmatch_write_through import (
                    _CONTRACT_FIELDS, _snapshot_matches,
                )
                previous_contract = getattr(
                    run.task_conversation, "_dm_write_contract_baseline", None,
                )
                # Rebase only when the DB still holds this runner's previous
                # committed contract. An external G2/contract change must keep
                # the stale source baseline rejected after this merge commits.
                can_rebase_contract = previous_contract is not None and _snapshot_matches(
                    _dm_conv, previous_contract,
                )
                if not can_rebase_contract:
                    mismatch_fields = ["<missing-baseline>"]
                    if previous_contract is not None:
                        mismatch_fields = [
                            field for field in _CONTRACT_FIELDS
                            if getattr(_dm_conv, field, None) != previous_contract.get(field)
                        ]
                    def _qa_meta(value):
                        entries = value if isinstance(value, list) else []
                        return {
                            "len": len(entries),
                            "pending": [
                                entry.get("goal_contract_pending")
                                for entry in entries if isinstance(entry, dict)
                                and "goal_contract_pending" in entry
                            ],
                            "types": [type(entry).__name__ for entry in entries],
                        }
                    previous_qa = None if previous_contract is None else previous_contract.get(
                        "deathmatch_grilling_qa_history",
                    )
                    logger.warning(
                        "deathmatch contract conflict conv=%s source_status=%s "
                        "db_status=%s source_version=%s db_version=%s mismatch=%s "
                        "previous_qa=%s source_qa=%s db_qa=%s",
                        run.conversation_id,
                        getattr(run.task_conversation, "deathmatch_status", None),
                        getattr(_dm_conv, "deathmatch_status", None),
                        None if previous_contract is None else previous_contract.get("deathmatch_plan_version"),
                        getattr(_dm_conv, "deathmatch_plan_version", None),
                        mismatch_fields,
                        None if previous_contract is None else _qa_meta(previous_qa),
                        _qa_meta(getattr(run.task_conversation, "deathmatch_grilling_qa_history", None)),
                        _qa_meta(getattr(_dm_conv, "deathmatch_grilling_qa_history", None)),
                    )
                    # The old runner must not make any further data writes,
                    # including unguarded event writes, after this conflict.
                    run.task_conversation._dm_write_stale = True
                # A4.9: optimistic per-field merge
                # (external user actions win; see
                # merge_sync_deathmatch_state) —
                # replaces the old blind copy.
                from app.services.deathmatch_service import (
                    DEATHMATCH_CONTROL_FIELDS,
                    DEATHMATCH_SYNC_FIELDS,
                    merge_sync_deathmatch_state,
                )
                merge_source = run.task_conversation
                if not can_rebase_contract:
                    import copy as _copy_contract
                    merge_source = _copy_contract.copy(run.task_conversation)
                    # A conflict merge may update control fields, but every
                    # synchronized data field must stay anchored to the DB. This
                    # covers the contract plus verification/reflection/events,
                    # while control fields retain the original freeze semantics.
                    merge_data_fields = set(_CONTRACT_FIELDS) | (
                        set(DEATHMATCH_SYNC_FIELDS) - set(DEATHMATCH_CONTROL_FIELDS)
                    )
                    for field in merge_data_fields:
                        if hasattr(merge_source, field):
                            setattr(
                                merge_source, field,
                                _copy_contract.deepcopy(getattr(_dm_conv, field, None)),
                            )
                    if getattr(run.task_conversation, "deathmatch_status", None) in {
                        "done", "partial_complete", "paused", "human_gate",
                    }:
                        # A replacement contract owns its lifecycle too. A stale
                        # runner's terminal status/reason must never mark G2 done
                        # (or pause it) while G2 still has pending work.
                        for field in (
                            "deathmatch_status", "deathmatch_reason",
                            "deathmatch_human_gate", "deathmatch_pause_state",
                        ):
                            if hasattr(merge_source, field):
                                setattr(
                                    merge_source, field,
                                    _copy_contract.deepcopy(getattr(_dm_conv, field, None)),
                                )
                _new_snapshot, _new_frozen = (
                    merge_sync_deathmatch_state(
                        _dm_conv,
                        merge_source,
                        run.dm_sync_snapshot,
                        run.dm_sync_frozen,
                    )
                )
                if can_rebase_contract:
                    await _commit_deathmatch_contract(
                        _dm_db, _dm_conv,
                        baseline_conversation=run.task_conversation,
                    )
                else:
                    await _dm_db.commit()
                # Only advance the baseline AFTER a
                # successful commit — otherwise a
                # failed sync would look persisted
                # and later syncs would skip it.
                run.dm_sync_snapshot = _new_snapshot
                run.dm_sync_frozen = _new_frozen
    except Exception:
        logger.exception("Failed to commit deathmatch state")

    # ── Per-turn persistence: save each deathmatch turn's
    # content as a separate DB message so it survives page
    # refreshes. Without this, all intermediate turn content
    # lives only in memory and is lost on disconnect/refresh.
    _dm_should_continue = verdict.get("should_continue", False)
    _dm_continuation_prompt = verdict.get("continuation_prompt", "")
    _dm_status_val = verdict.get("status", "")
    _dm_message_val = verdict.get("message", "")
    _dm_turn_num = verdict.get("turns", 0)

    # Flush current assistant_content to content_segments
    # (A4.9 I1: per-turn deathmatch persistence is the
    # same final-answer surface — same single strip).
    _turn_content = _strip_leading_orphan_colon(_strip_leading_orphan_punct(_strip_dsml_all(run.assistant_content).strip()))
    if _turn_content:
        run.content_segments.append(_turn_content)
        run.display_sequence.append({"type": "text", "content": _turn_content})

    # Build joined content for this turn
    _turn_joined = "\n\n<!-- segment_split -->\n\n".join(run.content_segments) if run.content_segments else ""

    # Build tool results JSON for this turn
    _turn_tr_json = None
    if run.tool_results_accumulated:
        try:
            _turn_tr_json = _transform_tool_loop_results(
                run.tool_results_accumulated,
                search_queries_used=run.search_queries_used if run.search_queries_used else None,
                queries_by_call=run.search_queries_by_call or None,
            )
        except Exception:
            # A transform failure must never kill the
            # detached goal loop (conv 6b0faf81: an
            # unhandled raise here escaped to the
            # task catch-all, leaving "active" with
            # no terminal message — silent zombie).
            logger.exception(
                "Deathmatch turn transform failed (turn %d) — persisting raw tool results",
                _dm_turn_num,
            )
            _turn_tr_json = json.dumps({
                "content_segments": run.content_segments,
                "display_sequence": run.display_sequence,
            }, ensure_ascii=False)
        if _turn_tr_json:
            try:
                _tr_obj = json.loads(_turn_tr_json)
            except Exception:
                _tr_obj = None
            if _tr_obj:
                _a_steps = _tr_obj.get("agent_steps", [])
                _resolved_turn = []
                _step_idx = 0
                for _item in run.display_sequence:
                    if _item.get("type") == "text":
                        _resolved_turn.append(_item)
                    elif _item.get("type") == "reasoning_step":
                        _resolved_turn.append(_item)
                    elif _item.get("type") == "tool_placeholder" and _step_idx < len(_a_steps):
                        _step = dict(_a_steps[_step_idx])
                        _step["type"] = _step.get("step_type", "tool")
                        _resolved_turn.append(_step)
                        _step_idx += 1
                while _step_idx < len(_a_steps):
                    _step = dict(_a_steps[_step_idx])
                    _step["type"] = _step.get("step_type", "tool")
                    _resolved_turn.append(_step)
                    _step_idx += 1
                _tr_obj["display_sequence"] = _resolved_turn
                _tr_obj["content_segments"] = run.content_segments
                _turn_tr_json = json.dumps(_tr_obj, ensure_ascii=False)
    elif run.content_segments:
        _turn_tr_json = json.dumps({
            "content_segments": run.content_segments,
            "display_sequence": run.display_sequence,
        }, ensure_ascii=False)

    # Serialize tool_calls for this turn
    _turn_tc_json = None
    if run.tool_call_events_accumulated:
        try:
            from app.services.tool_history import build_persisted_tool_calls
            _turn_tc_json = build_persisted_tool_calls(run.tool_call_events_accumulated)
        except Exception:
            pass

    # Save this turn's assistant message to DB
    if _turn_joined or _turn_tr_json:
        try:
            _turn_content = _build_content_fallback(_turn_joined, _turn_tr_json)
            _turn_content, _turn_tr_json = await _localize_media_for_persist(
                _turn_content, _turn_tr_json,
                run.user_id_local, run.username_local,
            )
            async with AsyncSessionLocal() as _turn_db:
                _turn_msg = Message(
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=_turn_content,
                    tool_results=_turn_tr_json,
                    tool_calls=_turn_tc_json,
                )
                _turn_db.add(_turn_msg)
                await _turn_db.commit()
                logger.info(
                    "Deathmatch turn %d persisted as message %s (%d chars)",
                    _dm_turn_num, _turn_msg.id, len(_turn_joined or ""),
                )
                # Capture attachments from the most
                # recent per-turn tool_results so the
                # final summary message can carry
                # download cards even when the
                # deathmatch ver dict has no
                # final_attachments (paused/human_gate
                # paths).
                if _turn_tr_json:
                    try:
                        _trp = json.loads(_turn_tr_json)
                        _ta = _trp.get("attachments")
                        if isinstance(_ta, list):
                            run.dm_last_turn_attachments = list(_ta)
                    except Exception:
                        pass
        except Exception:
            logger.exception("Failed to save deathmatch turn message")

    # Save continuation prompt as user message
    if _dm_should_continue and _dm_continuation_prompt:
        _clean_prompt = MARKER_RE.sub("", _dm_continuation_prompt).strip()
        if _clean_prompt:
            try:
                async with AsyncSessionLocal() as _turn_db:
                    _turn_user_msg = Message(
                        conversation_id=run.conversation_id,
                        role="user",
                        content=_clean_prompt,
                    )
                    _turn_db.add(_turn_user_msg)
                    await _turn_db.commit()
            except Exception:
                logger.exception("Failed to save deathmatch continuation prompt")
    elif not _dm_should_continue and (
        _dm_status_val in ("human_gate", "paused", "partial_complete")
        or verdict.get("verdict") == "wait"
    ) and _dm_message_val:
        # Save status message for human_gate/paused/
        # partial_complete. partial_complete carries
        # the collected deliverables as attachments so
        # the ending shows download cards next to the
        # status text instead of an empty bubble.
        _status_tr_json = None
        if run.dm_final_attachments:
            try:
                _status_tr_json = json.dumps(
                    {"attachments": run.dm_final_attachments},
                    ensure_ascii=False,
                )
            except Exception:
                _status_tr_json = None
        try:
            async with AsyncSessionLocal() as _turn_db:
                _turn_status_msg = Message(
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=_dm_message_val,
                    tool_results=_status_tr_json,
                )
                _turn_db.add(_turn_status_msg)
                await _turn_db.commit()
            run.state._dm_terminal_status_saved = True
        except Exception:
            logger.exception("Failed to save deathmatch status message")

    # Reset accumulators for next turn
    run.assistant_content = ""
    run.assistant_reasoning = ""
    run.current_reasoning_segment = ""
    run.content_segments = []
    run.display_sequence = []
    run.tool_results_accumulated = []
    run.tool_call_events_accumulated = []
    run.search_queries_used = []
    run.search_queries_by_call = {}
    run.canary_tail = ""
    run.reasoning_canary_tail = ""
    run.reasoning_meta_tail = ""
    # I3：拼接链标记不得跨死磕轮泄漏（每轮=独立输出单元）。
    run.stitch_active = False
    run.stitch_base = ""
    # Deathmatch per-turn persistence above writes
    # assistant_content (which INCLUDES gated text);
    # flush the gated text to the stream so the
    # live view matches what is persisted, then
    # reset the gate for the next turn.
    _gate_flush = run.pre_tool_gate.flush()
    if _gate_flush.strip():
        await run.put({"content": _gate_flush})


async def _persist_interjection_commit(run, event) -> None:
    """Persist one interjection commit and insert its inline timeline item.

    2026-11-01: extracted from _drive_agent_events (function-size bound); the
    behavior is unchanged — flush the pre-tool gate, write the user row with its
    embedding parent, write the small sidecar, commit, then append the item.
    """
    # 插话 commit-echo（2026-08-29）：loop 在迭代边界把用户插话注入
    # 消息列表。2026-11-01 内联定位：先把已产生的 gate/正文/思考落到
    # display_sequence，再插入 interjection 项——该项就是「插话真实
    # 进入 run」的位置；终态持久化会把它写进 interjection_items。
    _ij_payload = event["interjection_committed"] or {}
    _ij_content = str(_ij_payload.get("content") or "")
    _ij_client_message_id = _ij_payload.get("client_message_id") or None
    # Release any gate-held pre-tool text so the user sees exactly what
    # preceded the interjection (the text is already in assistant_content).
    _ij_gate_flush = run.pre_tool_gate.flush()
    if _ij_gate_flush.strip():
        await run.put({"content": _ij_gate_flush})
    _ij_msg_id = None
    _ij_created = None
    _ij_item = None
    try:
        async with AsyncSessionLocal() as _ij_db:
            _ij_conv = (await _ij_db.execute(
                select(Conversation).where(Conversation.id == run.conversation_id)
            )).scalar_one_or_none()
            if _ij_conv is not None:
                _ij_msg = Message(
                    conversation_id=run.conversation_id,
                    role="user",
                    content=_ij_content,
                    # 标记渲染归属：刷新后前端把这条用户行收进
                    # assistant 气泡的内联位置，而不再作为顶层气泡。
                    embedded_in_message_id=run.streaming_row_id,
                )
                _ij_db.add(_ij_msg)
                _ij_conv.updated_at = datetime.utcnow()
                await _ij_db.flush()
                _ij_msg_id = str(_ij_msg.id)
                _ij_created = (
                    _ij_msg.created_at.isoformat()
                    if _ij_msg.created_at else datetime.utcnow().isoformat()
                )
                _ij_item = prepare_interjection_item(
                    run,
                    message_id=_ij_msg_id,
                    content=_ij_content,
                    created_at=_ij_created,
                )
                if run.streaming_row_id:
                    # Small sidecar survives tool_results externalization.
                    await _ij_db.execute(
                        sa_update(Message)
                        .where(Message.id == run.streaming_row_id)
                        .values(interjection_items=json.dumps(
                            list(run.interjection_items) + [_ij_item],
                            ensure_ascii=False,
                        ))
                    )
                await _ij_db.commit()
                commit_prepared_item(run, _ij_item)
    except Exception:
        logger.exception(
            "Interjection persistence failed for conversation %s",
            run.conversation_id,
        )
        # Keep the in-memory timeline consistent even when the row
        # write fails: the echo still reaches the live client.
        if _ij_item is None:
            _ij_item = prepare_interjection_item(
                run,
                message_id=_ij_msg_id or "",
                content=_ij_content,
                created_at=_ij_created or datetime.utcnow().isoformat(),
            )
        if _ij_item not in run.interjection_items:
            commit_prepared_item(run, _ij_item)
    await run.put({"interjection_committed": {
        "id": _ij_msg_id,
        "conversation_id": run.conversation_id,
        "content": _ij_content,
        "created_at": _ij_created,
        # W26（B3）：回带客户端配对 id（旧客户端/旧队列项为 null）
        "client_message_id": _ij_client_message_id,
        # 2026-11-01 inline placement:
        "embedded_in_message_id": run.streaming_row_id,
        "display_item": (
            {"type": "interjection", **_ij_item}
            if _ij_item is not None else None
        ),
    }})


async def _drive_agent_events(run):
    """Bridge one AgentLoop run's events onto the SSE stream/buffer.

    C-02c: extracted verbatim from `_chat_agent_loop_impl`; every former
    closure free variable is read from the shared `_AgentLoopRun` state.
    """
    _ttft_t1 = time.monotonic()
    _ttft_t2 = 0  # coordinator return
    _ttft_t3 = 0  # first content delta
    async for event in run.agent_loop.run(
        run.messages_for_model,
        user=run.task_user,
        conversation=run.task_conversation,
        assistant=run.cap_assistant,
        precomputed_coord=run.coord_result,
        interjection_queue=run.agent_state.interjection_queue,
        initial_prompt_tokens=_last_measured_prompt_tokens(run.conversation_messages),
    ):
        if "coord_done" in event:
            _ttft_t2 = time.monotonic()

        elif "coordinator" in event:
            _coord_route = (event["coordinator"] or {}).get("route")
            _coord_expects = (event["coordinator"] or {}).get("expects_tools")
            run.pre_tool_gate.arm(_coord_route, _coord_expects)

        elif "reasoning_content" in event:
            if not _ttft_t3:
                _ttft_t3 = time.monotonic()
                logger.info(
                    "TTFT(reasoning) conv=%s setup=%dms coord=%dms prefill=%dms total=%dms",
                    run.conversation_id,
                    int((_ttft_t1 - run.ttft_t0) * 1000),
                    int((_ttft_t2 - _ttft_t1) * 1000) if _ttft_t2 else -1,
                    int((_ttft_t3 - _ttft_t2) * 1000) if _ttft_t2 else -1,
                    int((_ttft_t3 - run.ttft_t0) * 1000),
                )
            # 遵循词 canary marker may appear in CoT when
            # the model restates the echo rule — strip it
            # here (single funnel for display + persist).
            # Streaming-aware: the marker can be split
            # across deltas, so a tail buffer is held back
            # (conv 6227fb26 leaked it into the thinking
            # panel otherwise).
            _reasoning_chunk, run.reasoning_canary_tail = run.strip_canary_streaming(
                event["reasoning_content"], run.reasoning_canary_tail
            )
            # 元引用清洗（canary 之后，尾缓冲处理跨分片拆词）
            _reasoning_chunk, run.reasoning_meta_tail = scrub_reasoning_streaming(
                _reasoning_chunk, run.reasoning_meta_tail
            )
            run.assistant_reasoning += _reasoning_chunk
            run.current_reasoning_segment += _reasoning_chunk
            await run.put({"reasoning_content": _reasoning_chunk})

        elif "content" in event:
            if not _ttft_t3:
                _ttft_t3 = time.monotonic()
                logger.info(
                    "TTFT conv=%s setup=%dms coord=%dms prefill=%dms total=%dms",
                    run.conversation_id,
                    int((_ttft_t1 - run.ttft_t0) * 1000),
                    int((_ttft_t2 - _ttft_t1) * 1000) if _ttft_t2 else -1,
                    int((_ttft_t3 - _ttft_t2) * 1000) if _ttft_t2 else -1,
                    int((_ttft_t3 - run.ttft_t0) * 1000),
                )
            _clean_content, run.canary_tail = run.strip_canary_streaming(
                event["content"], run.canary_tail
            )
            _clean_content = _strip_dsml_all(_clean_content)
            run.assistant_content += _clean_content
            run.pf.feed(run.visible_prose + run.assistant_content)
            if _clean_content:
                _gate_stream = run.pre_tool_gate.on_content(
                    _clean_content, time.monotonic()
                )
                if _gate_stream:
                    # Hold cap released (or gate not armed):
                    # the text must reach the client NOW.
                    await run.put({"content": _gate_stream})

        elif "tool_call" in event:
            reason_seg = run.current_reasoning_segment.strip()
            if reason_seg:
                _reason_item = {
                    "type": "reasoning_step",
                    "title": "💭 思考过程",
                    "content": reason_seg,
                }
                run.display_sequence.append(_reason_item)
                await run.put({"reasoning_segment": _reason_item})
                run.current_reasoning_segment = ""
            # When the assistant emits tool calls, the content
            # produced so far is a transient pre-tool
            # utterance (often a duplicate or placeholder).
            # Only the part the client NEVER saw (still in
            # the gate hold) is dropped; anything already
            # streamed must persist — A4.9 I2 contract
            # generalized (conv 4e159a79, see helper doc).
            # 输出单元契约（2026-10-14 用户裁定）：非拼接链
            # 下预工具散文停车进时间线（过程区），不拼进终局
            # 答案；拼接链（length 续写）全量保留。未展示
            # hold 仍丢弃（conv 149ce886）。
            run.assistant_content, _parked_prose = _split_pre_tool_prose(
                run.assistant_content, run.pre_tool_gate, run.stitch_active
            )
            # 已展示散文并入 visible_prose：终局答案仍不含它（语义不变），但渐进
            # 落库/被杀后的 interrupted 行保留用户看过的内容（2026-11-02）。
            if _parked_prose:
                run.visible_prose += _parked_prose
            if _parked_prose:
                # 位序契约（A4.9 C1）：parked 文本在工具卡
                # **之前**——与 SSE 直播顺序（旁白→工具）一致；
                # 且 _display_sequence_keep_evidence 只丢「最后
                # 工具卡之后」的 text，parked 在前=压缩后存活
                # （用户已见文本不得刷新即蒸发），前后端手术镜像
                # （dropDraftTextAfterLastTool）同向。
                run.display_sequence.append(_parked_prose_item(_parked_prose))
            run.display_sequence.append({"type": "tool_placeholder"})
            if run.assistant_content:
                # 拼接链头快照：随链增长推进锚点（I4）。
                run.stitch_base = run.assistant_content
            # I5：渐进落库单飞器与累积同步（见 iteration_done 侧）。
            # 2026-11-02：写 visible_prose（用户已见散文），不再把行清空——
            # 否则每次工具边界都把渐进落库的正文抹掉，被杀时行是空的。
            await run.pf.revert_to(run.visible_prose)
            # Held canary tails belong to the discarded
            # pre-tool draft — reset so no stale partial
            # marker is prepended to post-tool content.
            # Lossless for prose: the held tail is by
            # construction a pure `[遵循词…` marker-prefix
            # fragment (CANARY_PREFIX_RE; see
            # tests/test_canary_tail_invariant.py) — never
            # answer text, so dropping it on a KEPT draft
            # loses nothing (a flush would leak marker
            # fragments into the answer instead).
            run.canary_tail = ""
            run.reasoning_canary_tail = ""
            run.reasoning_meta_tail = ""
            run.pre_tool_gate.on_tool_call()
            tc = event["tool_call"]
            run.tool_call_events_accumulated.append(tc)
            run.pf.tick(run.assistant_content)
            if tc.get("name") == "web_search":
                args = tc.get("arguments", {})
                queries = args.get("queries", [])
                if isinstance(queries, list):
                    run.search_queries_used = list(queries)
                    # Per-call queries for round-level display
                    # (conv a3cfb421 2026-08-09): keep each
                    # web_search call's own query list keyed
                    # by call id so the persisted rounds show
                    # real per-round queries instead of the
                    # last call's for every round. AgentLoop
                    # emits tool_call events with `call_id`
                    # (never `id` — A4.9 review finding).
                    run.search_queries_by_call[tc.get("call_id") or tc.get("id") or ""] = list(queries)
            await run.put({"tool_call": tc})

        elif "tool_result" in event:
            tr = event["tool_result"]
            run.tool_results_accumulated.append(tr)
            run.pf.tick(run.assistant_content)
            await run.put({"tool_result": tr})
            # Live download-card set: recompute the
            # ACCUMULATED set (same helper as the
            # persist-time transform) so the streamed card
            # count grows monotonically and never drops
            # files. The previous per-call events replaced
            # the whole set with only the current tool
            # call's files, so the live count jumped
            # 1→1→1→1→4 with docx cards vanishing
            # (conv fbf5779b, 2026-08-06).
            _live_atts = _collect_download_attachments(run.tool_results_accumulated)
            if _live_atts and _live_atts != run.last_streamed_attachments:
                run.last_streamed_attachments = _live_atts
                await run.put({"attachments": _live_atts})

        elif "iteration" in event:
            await run.put({"iteration": event["iteration"]})

        elif "interjection_committed" in event:
            await _persist_interjection_commit(run, event)

        elif "context_info" in event:
            # 紧急压缩 recovery（LLM 错误/final-thinking）路径
            # 从 agent_loop 直接 yield context_info——转发给
            # 中继层刷新前端 token 徽章。
            await run.put({"context_info": event["context_info"]})

        elif "iteration_done" in event:
            # Iteration boundary without a tool_call event:
            # the previous iteration ended. A no-tool
            # iteration means the held text IS the answer —
            # stream it now instead of parking it until
            # `done` (whole-answer pop, 2026-08-06). A
            # tool iteration means the held text is stale
            # pre-tool prose — drop it (also covers a
            # tool_call event lost to subscriber-queue
            # overflow). Same tool-boundary contract as
            # the tool_call branch: only the never-shown
            # hold is droppable (conv 4e159a79).
            _id_info = event["iteration_done"] or {}
            # 截断续写链标记（finish_reason=length 分支发出）：
            # 链上文本允许跨工具边界拼接，是唯一合法拼接场景。
            if _id_info.get("stitch"):
                run.stitch_active = True
                run.stitch_base = run.assistant_content
            if bool(_id_info.get("tool_calls")):
                run.assistant_content, _parked_prose = _split_pre_tool_prose(
                    run.assistant_content, run.pre_tool_gate, run.stitch_active
                )
                if _parked_prose:
                    run.visible_prose += _parked_prose
                    run.display_sequence.append(_parked_prose_item(_parked_prose))
                if run.assistant_content:
                    run.stitch_base = run.assistant_content
                # I5：渐进落库单飞器与累积同步——清零后
                # feed 的长度守卫会饿死刷写（在途行留旧旁白）。
                # 2026-11-02：写 visible_prose（用户已见散文），不清空在途行。
                await run.pf.revert_to(run.visible_prose)
            _gate_flush = run.pre_tool_gate.on_iteration_done(
                bool(_id_info.get("tool_calls")),
                time.monotonic(),
            )
            if bool(_id_info.get("tool_calls")):
                # Tool-call event lost to queue overflow:
                # the tool_call branch (which resets the
                # canary tails) never ran — reset here so
                # stale partial-marker text cannot merge
                # into post-tool content (A4.9 review).
                run.canary_tail = ""
                run.reasoning_canary_tail = ""
                run.reasoning_meta_tail = ""
            if _gate_flush.strip():
                logger.info(
                    "Pre-tool gate released by iteration_done "
                    "(iteration=%s, tool_calls=%s): streaming held text (%d chars)",
                    _id_info.get("iteration"), _id_info.get("tool_calls"),
                    len(_gate_flush),
                )
                await run.put({"content": _gate_flush})

        elif "ping" in event:
            await run.put({"ping": True})

        elif "compression" in event:
            # 压缩 = 丢弃草稿文本、以压缩后上下文重答（A4.9 I2：
            # 持久化答案是重答稿，不是拼接稿）。
            # conv 038e837a（2026-09-22）修复两点：
            #   1. 工具证据三件套必须跨压缩存活——citation_ledger
            #      不随压缩重置，清空 tool_results_accumulated 会让
            #      落库 results[] 缩水（30→10），草稿合法 [N] 越界被
            #      sanitize_texts 误剥（角标/卡片全丢）。与 audit_reset
            #      （conv efaf8f9c）既定政策对齐：工具工作是本轮证据。
            #   2. 下发 audit_reset 做客户端时间轴清创（conv 827a6f78
            #      同族）——被丢弃的旧稿不再与重答同屏（重复生成）。
            logger.debug("Context compressed: %s", event["compression"])
            # A4.9 R2 N1 / R3 + 输出单元契约（2026-10-14 I3）：
            # 压缩=以压缩后上下文重答（新输出单元）——草稿与
            # 拼接链标记一并复位；已展示停车文本在 display_
            # sequence（keep_evidence 保留在前 text 项）不蒸发。
            _keep_prose = ""
            _had_dropped_text = bool(
                run.assistant_content and run.assistant_content != _keep_prose
            ) or bool(run.content_segments)
            run.assistant_content = _keep_prose
            run.stitch_active = False
            run.stitch_base = ""
            # trailing 思考段不静默丢弃（audit_reset A4 parity）：
            # 落盘为 reasoning_step，思考与工具不随草稿丢弃。
            reason_seg = run.current_reasoning_segment.strip()
            if reason_seg:
                run.display_sequence.append({
                    "type": "reasoning_step",
                    "title": "💭 思考过程",
                    "content": reason_seg,
                })
            run.current_reasoning_segment = ""
            run.content_segments = []
            _seq_before = len(run.display_sequence)
            run.display_sequence = _display_sequence_keep_evidence(run.display_sequence)
            _had_dropped_text = _had_dropped_text or (
                len(run.display_sequence) != _seq_before
            )
            run.canary_tail = ""
            run.reasoning_canary_tail = ""
            run.reasoning_meta_tail = ""
            run.pre_tool_gate.reset()
            # 客户端清创：仅当确有草稿文本被丢弃才下发——
            # 无文本可丢的前置压缩（token-threshold/deathmatch
            # forced）保持既有 no-op，不得误删上一轮已落库文本
            # （A4.9 R1 Important-4）。死磕多轮流（turn 边界
            # 不在事件里）暂不下发：dropDraftTextAfterLastTool
            # 非 turn 作用域，会连带删掉上一轮已落库答案文本
            # （A4.9 R2 N3，已知边界）。
            if _had_dropped_text and run.dm is None:
                await run.put({"audit_reset": True})
            # 压缩前后 token 对比（含工具 schema 口径）——
            # 前端在「上下文压缩」步骤块内展示 X → Y tokens 变化。
            _comp = event["compression"] or {}
            _c_before = int(_comp.get("before") or 0)
            _c_after = int(_comp.get("after") or 0)
            # A4 (2026-08-21): canary-triggered compressions
            # are silent — no timeline step, only logs +
            # token-badge refresh. Token-threshold
            # compressions (pre-turn maintenance) stay
            # visible as before.
            if not _comp.get("silent") or _had_dropped_text:
                # A4 (2026-08-21)：canary 压缩静默（仅日志 +
                # token 徽章）。但草稿被丢弃时用户会看见文本消失
                # ——此时不再是「用户不可见」，必须补时间线步骤
                # 说明（A4.9 R1 Minor-2）。
                from app.services.context_compressor import format_compression_step_content
                await run.put({
                    "agent_step": {
                        "name": "context_compression",
                        "title": "上下文压缩",
                        "content": format_compression_step_content(_c_before, _c_after),
                        "step_type": "system",
                        "tokens_before": _c_before or None,
                        "tokens_after": _c_after or None,
                    }
                })
            # 压缩后上下文已变小——刷新前端 token 徽章。
            if _c_after > 0:
                await run.put({"context_info": {
                    "tokens": _c_after,
                    "context_length": run.ctx_window,
                    "compressed": True,
                }})

        elif "audit_reset" in event:
            # A4 (2026-08-21): internal QC reset event.
            # A draft was streamed live and then failed the
            # internal quality audit (or the canary re-assert
            # path) — reset the accumulators so the rejected
            # draft never concatenates into the PERSISTED
            # answer; the regeneration that follows is the
            # only authoritative content. Nothing is
            # rendered to the client (silent QC).
            #
            # conv 3b58af5b (2026-08-23, R1/R2): ONLY the
            # rejected draft TEXT is discarded. The turn's
            # real work survives:
            #   - reasoning (assistant_reasoning +
            #     display_sequence reasoning_step items) —
            #     post-reject revisions run thinking-off
            #     (revision_thinking_off, user decision
            #     2026-08-21), so discarding pre-reject
            #     thinking erased the turn's ENTIRE
            #     thinking panel from the persisted answer;
            #   - tool_placeholder items — they pair 1:1
            #     with the preserved tool_results
            #     accumulated entries at completion;
            #     clearing them starved the pairing and
            #     appended every agent_step AFTER the final
            #     text (tools-at-bottom symptom).
            if event.get("keep_stitch") and run.stitch_active:
                # 可恢复重试：只回退本次失败残稿，保住
                # 截断续写链已积累的 head（唯一拼接场景）。
                run.assistant_content = run.stitch_base
            else:
                run.assistant_content = ""
                # 拼接链随打回复位：重答稿是新输出单元。
                run.stitch_active = False
                run.stitch_base = ""
            # A4.9 r1 M1：被拒草稿可能已被刷进在途行——
            # 撤回到本稿前基线（keep_stitch=链锚，否则空），
            # 残稿永不含被拒文字。
            await run.pf.revert_to(run.assistant_content)
            reason_seg = run.current_reasoning_segment.strip()
            if reason_seg:
                # Thinking accrued since the last tool
                # boundary belongs to the rejected round —
                # flush it as a preserved reasoning_step.
                # Silent QC: NOT re-emitted live (the user
                # already saw these deltas stream); this
                # only fixes the persisted surfaces.
                run.display_sequence.append({
                    "type": "reasoning_step",
                    "title": "💭 思考过程",
                    "content": reason_seg,
                })
                run.current_reasoning_segment = ""
            # conv efaf8f9c (2026-08-20): do NOT clear
            # tool_results_accumulated /
            # tool_call_events_accumulated /
            # search_queries_used / search_queries_by_call
            # here. What must be discarded is the rejected
            # DRAFT text/reasoning that was streamed live;
            # the tool work from earlier iterations is the
            # turn's evidence and must survive so the final
            # persisted tool_results keeps results[],
            # agent_steps[] and the citation mapping.
            run.canary_tail = ""
            run.reasoning_canary_tail = ""
            run.reasoning_meta_tail = ""
            run.pre_tool_gate.reset()
            # conv 827a6f78 turn-B (2026-09-03): sync the
            # CLIENT on the silent QC reset — without this
            # the frontend's s.content kept every rejected
            # draft and the done-time answer bubble
            # contained the whole mess (refresh restored
            # the clean DB row). The buffer resets its own
            # accumulator on this event (resume replay
            # parity); the SSE generator forwards it as a
            # frame for the client-side timeline surgery.
            # keep_stitch（2026-10-14）：可恢复重试仅回退失败
            # 残稿——keep_text=链锚，客户端按锚截断（streamDispatch
            # → onAuditReset），截断续写链文本用户已见不蒸发。
            _ks = bool(event.get("keep_stitch") and run.stitch_active)
            await run.put({
                "audit_reset": True,
                "keep_stitch": _ks,
                "keep_text": run.assistant_content if _ks else "",
            })

        elif "agent_step" in event:
            await run.put({"agent_step": event["agent_step"]})

        elif "error" in event:
            logger.error("Agent loop error: %s", event["error"])
            run.loop_error = str(event["error"])
            await run.agent_queue.put({"error": event["error"]})

        elif "deathmatch_verdict" in event:
            await _handle_dm_verdict_event(run, event["deathmatch_verdict"])

        elif "done" in event:
            # Turn ended without a tool call: flush any
            # gated pre-tool text (it IS the final answer).
            _gate_flush = run.pre_tool_gate.flush()
            if _gate_flush.strip():
                await run.put({"content": _gate_flush})
            # A4.9 R1 NEW-2：流结束时冲刷元引用尾缓冲
            # （canary 尾是残缺标记、按设计丢弃；元引用尾
            # 是正常思考文本，必须清洗后补发，否则尾部
            # 最多 63 字符静默丢失）。
            if run.reasoning_meta_tail:
                _meta_flush = scrub_reasoning_meta(run.reasoning_meta_tail)
                if _meta_flush:
                    run.assistant_reasoning += _meta_flush
                    run.current_reasoning_segment += _meta_flush
                    await run.put({"reasoning_content": _meta_flush})
                run.reasoning_meta_tail = ""
            break


def _stop_tool_results_with_sequence(run, stop_tool_results):
    """Stop/interrupted rows with inline interjections need the persisted
    display sequence *including the in-flight tail* (otherwise the reload
    renders only the flushed segments and loses the answer after the last
    interjection). Extracted to keep the cleanup pin's byte offset in
    _handle_agent_loop_cancel stable."""
    if not getattr(run, "interjection_items", None):
        return stop_tool_results
    try:
        _stop_tail = (getattr(run, "assistant_content", "") or "").strip()
        _stop_reason_tail = (getattr(run, "current_reasoning_segment", "") or "").strip()
        if _stop_reason_tail:
            run.display_sequence.append({
                "type": "reasoning_step",
                "title": "💭 思考过程",
                "content": _stop_reason_tail,
            })
            run.current_reasoning_segment = ""
        if _stop_tail:
            run.content_segments.append(_stop_tail)
            run.display_sequence.append({"type": "text", "content": _stop_tail})
            run.assistant_content = ""
        _stop_tr_json = _transform_tool_loop_results(
            run.tool_results_accumulated,
            search_queries_used=run.search_queries_used or None,
            queries_by_call=run.search_queries_by_call or None,
        )
        _stop_obj = json.loads(_stop_tr_json) if _stop_tr_json else {}
        _stop_obj["display_sequence"] = resolve_persisted_display_sequence(
            run.display_sequence, _stop_obj.get("agent_steps", []),
        )
        _stop_obj["content_segments"] = run.content_segments
        return json.dumps(_stop_obj, ensure_ascii=False)
    except Exception:
        logger.debug("stop display sequence enrich failed", exc_info=True)
        return stop_tool_results


async def _handle_agent_loop_cancel(run):
    """Persist the partial reply (or clean the zombie streaming row) and the deathmatch terminal state after a deliberate stop / passive cancellation.

    C-02c: extracted verbatim from `_chat_agent_loop_impl`; the caller re-raises."""
    # CancelledError (BaseException in 3.8+) bypasses
    # `except Exception` below — handle it explicitly so the
    # stream buffer is marked complete (is_running=False).
    # Otherwise the safety-net reconciliation in
    # GET /api/conversations/{id} sees is_running=True and
    # refuses to reset a zombie deathmatch_status="active".
    run.agent_state.is_running = False
    await run.agent_state.broadcast_done()
    logger.info(
        "Stop-cancel conv=%s content=%d reasoning=%d segments=%d tools=%d state_content=%d dm=%s",
        run.cap_conversation_id, len(run.assistant_content), len(run.assistant_reasoning),
        len(run.content_segments), len(run.tool_results_accumulated),
        len(run.agent_state.content), run.dm is not None,
    )
    # Deliberate stop (POST /stream/stop/{id}) or passive
    # disconnect cancels the task mid-reply: persist the
    # partial content accumulated so far, or the conversation
    # shows only the user message and the generated text is
    # lost (chat.spec "can stop a streaming response").
    # Mirror the self-save path: fresh session (the request's
    # `db` may be mid-cancellation) + empty-content guard.
    # P1（2026-10-31 UPSTREAM_TODO_20261031）：正常终态已落库（response_saved）
    # 的迟到 stop 不得再落重复 final——终态路径已保存并翻行，这里再保存会插重复
    # final（conv d7ea65b8 事故面之一）。
    if run.dm is None and getattr(run.state, "response_saved", None):
        logger.info(
            "Stop-cancel conv=%s: response_saved already set — skip late duplicate persistence",
            run.cap_conversation_id,
        )
    if run.dm is None and not getattr(run.state, "response_saved", None):
        try:
            _stop_partial_text = _strip_dsml_all(run.assistant_content).strip()
            _stop_segments = [s for s in run.content_segments if s and s.strip()]
            if _stop_partial_text:
                _stop_segments.append(_stop_partial_text)
            _stop_content = "\n\n<!-- segment_split -->\n\n".join(_stop_segments)
            if not _stop_content.strip():
                # Response-audit wipe: the rejected draft was
                # streamed to the client then discarded from the
                # local accumulators (regeneration is the only
                # authoritative content). If the user stops
                # during regeneration, nothing else is left to
                # persist — fall back to what the client
                # actually received (_agent_state.content is
                # append-only and survives the audit reset).
                _stop_content = run.agent_state.content or ""
            _stop_tool_results = (
                json.dumps(run.tool_results_accumulated, ensure_ascii=False)
                if run.tool_results_accumulated else None
            )
            # 2026-11-01: stop/interrupted rows with inline interjections need
            # the persisted display sequence; otherwise after_char fallback on a
            # sanitized/trimmed content can misplace the item after refresh.
            _stop_tool_results = _stop_tool_results_with_sequence(run, _stop_tool_results)
            _stop_reasoning = (run.assistant_reasoning or run.agent_state.reasoning or "").strip() or None
            # A4.9 I2: stop-interrupted persistence is another
            # final-answer surface — the glitch lands on the
            # FIRST token, and a mid-reply stop would persist
            # it. Strip at the joined-content head (covers the
            # segment join AND the _agent_state fallback).
            _stop_content = _strip_leading_orphan_colon(_strip_leading_orphan_punct(_stop_content))
            # P1（2026-10-31 UPSTREAM_TODO_20261031）：门槛不再把 reasoning 当作
            # 落库条件（reasoning-only 不落 final，conv d7ea65b8 空气泡 f697cf2d
            # 根因）；reasoning 仍在流式阶段可见，不作为聊天消息持久化。
            # 2026-11-02（C5）：必须计入 tool_calls。W11 的渐进落库会把
            # tool_call_events_accumulated flush 进在途行；只按 content/tool_results
            # 判定会让「已发起工具调用、结果未回」的轮次走进空行删除分支，
            # 把同形状行在 stop 时删掉、在崩溃 sweep 时保留为 interrupted。
            # 2026-11-02（评审 C1）：必须用**规范 OpenAI 形状**落库
            # （`{id,type,function:{name,arguments}}`）。原始事件形状
            # `{call_id,name,arguments}` 会让下一轮 rebuild_structured_history
            # 产出无 id/function 的调用 → sanitize 丢弃配对 tool 消息 → 400。
            _stop_tool_calls = None
            if getattr(run, "tool_call_events_accumulated", None):
                from app.services.tool_history import build_persisted_tool_calls
                _stop_tool_calls = build_persisted_tool_calls(
                    run.tool_call_events_accumulated
                )
            _stop_should_persist = bool(
                _stop_content.strip() or _stop_tool_results or _stop_tool_calls
            )
            if _stop_should_persist:
                # P0 R1: user-stop is another final-answer persistence surface.
                # Use the SYNCHRONOUS ledger sanitizer (not the async LLM
                # disambiguator): stop already cancelled the request, and
                # awaiting a 60s judge call here delays persistence/recovery
                # (review R2 round-2 Important).
                _stop_content = strip_stop_citation_echo(
                    _stop_content,
                    run.tool_results_accumulated,
                    run.search_queries_used,
                    run.search_queries_by_call,
                )
                _stop_content, _stop_tool_results = await _localize_media_for_persist(
                    _stop_content, _stop_tool_results,
                    run.user_id_local, run.username_local,
                )
                # P1 复检（规格 §3.2-3）：sanitize/localize 在门槛之后执行，可能把
                # 正文清空——「引用回声-only + reasoning」同样不得落 final，转空行清理。
                # 2026-11-02（评审 I3）：复检必须与门槛同口径——否则
                # 「只有工具调用、正文为空」会在这里翻回 False，进入空行清理，
                # 使前面新加的 `or _stop_tool_calls` 成为空操作。
                if not _stop_content.strip() and not _stop_tool_results and not _stop_tool_calls:
                    _stop_should_persist = False
            if _stop_should_persist:
                async with AsyncSessionLocal() as _stop_db:
                    _stop_conv = (await _stop_db.execute(
                        select(Conversation).where(
                            Conversation.id == run.cap_conversation_id
                        )
                    )).scalar_one_or_none()
                    if _stop_conv is not None:
                        # 渐进落库：streaming 在途行原地更新。
                        # 用户主动 stop = 刻意截断（用户知情且
                        # 见证），按既有语义落 'final'；进程
                        # 死亡类非预期中断才标 'interrupted'。
                        _stop_msg = await _save_assistant_terminal(
                            _stop_db,
                            conversation_id=run.cap_conversation_id,
                            content=_stop_content,
                            reasoning=_stop_reasoning,
                            tool_results=_stop_tool_results,
                            # 2026-11-02（C5）：把本 run 的工具调用一并落库，
                            # 不让「有调用无结果」的轮次在 stop 时丢证据。
                            tool_calls=_stop_tool_calls,
                            context_info=(
                                json.dumps(run.state._stream_buf.context_info, ensure_ascii=False)
                                if getattr(run.state._stream_buf, "context_info", None) else None
                            ),
                            status="final",
                            # P1（2026-10-31）：绑定本 run 的 streaming 行，
                            # 防「最新 streaming」反查命中别的 run 的僵尸行。
                            row_id=getattr(
                                run.state._stream_buf, "streaming_message_id", None),
                            # 插话排序：本 run 已 commit 插话的最大时间戳（buffer 权威）
                            interjection_anchor=interjection_anchor_from_buffer(
                                run.state._stream_buf),
                            interjection_items=(
                                json.dumps(run.interjection_items, ensure_ascii=False)
                                if getattr(run, "interjection_items", None) else None
                            ),
                        )
                        _stop_conv.updated_at = datetime.utcnow()
                        await _stop_db.commit()
                        await _stop_db.refresh(_stop_msg)
                        try:
                            await run.state._stream_buf.mark_complete(str(_stop_msg.id))
                        except Exception:
                            pass
                        logger.info(
                            "Persisted partial reply after stop for conversation %s (%d chars)",
                            run.cap_conversation_id, len(_stop_content),
                        )
            else:
                # 零内容 stop（首 token 前停止/审计清空后停止）：
                # 本 run 的渐进落库 streaming 空行无人落库——
                # 按行 id 精确删除，防僵尸行（conv 92398a80）。
                try:
                    async with AsyncSessionLocal() as _zdb:
                        _zomb_deleted = await _delete_empty_streaming_row(
                            _zdb,
                            getattr(run.state._stream_buf, "streaming_message_id", None),
                        )
                        # C1（A4.9 评审 2026-10-18）：必须显式
                        # commit——session 关闭会回滚未提交事务，
                        # 缺了它 DELETE 是空操作（与 4047 清道夫
                        # 同构口径）。
                        await _zdb.commit()
                    if _zomb_deleted:
                        logger.info(
                            "Stop-cancel: deleted empty streaming row %s for conversation %s",
                            getattr(run.state._stream_buf, "streaming_message_id", None),
                            run.cap_conversation_id,
                        )
                except Exception:
                    logger.debug(
                        "stop-cancel empty streaming row cleanup failed",
                        exc_info=True,
                    )
        except Exception:
            logger.exception(
                "Failed to persist partial reply after stop for conversation %s",
                run.cap_conversation_id,
            )
    try:
        await run.state._stream_buf.mark_complete(None)
    except Exception:
        pass
    # DEATHMATCH (conv 6b0faf81): a cancelled/errored goal loop
    # must NEVER leave deathmatch_status="active" with no
    # terminal message — the user sees a silent stop. Persist
    # a paused terminal state + reason so the conversation is
    # visibly resumable instead of a zombie that only the
    # reconcile flips 18 minutes later.
    await _persist_deathmatch_failure(
        run.cap_conversation_id, run.dm,
        "运行被取消",
        "死磕模式运行被取消。发送任意消息可继续推进目标。",
        expected_agent_state=run.agent_state,
    )


async def _finalize_agent_run(run):
    """Finalize one agent-loop run: compose/strip the terminal answer, sanitize citations, persist single-writer, broadcast `_completed`, and self-save after an SSE cancellation.

    C-02c: extracted verbatim from `_chat_agent_loop_impl`."""
    full_response_local = _strip_leading_orphan_colon(_strip_leading_orphan_punct(_strip_dsml_all(run.assistant_content).strip()))
    # M4（A4.9）：仅预工具散文、无工具后正文的轮次（答完即调工具
    # 收尾）——终稿空时提升末停车单元为答案，不得误标「回答生成
    # 失败」；从 display_sequence 摘除对应项防重复渲染。
    if not full_response_local:
        for _pi in range(len(run.display_sequence) - 1, -1, -1):
            _pit = run.display_sequence[_pi]
            if isinstance(_pit, dict) and _pit.get("type") == "text" and str(_pit.get("content") or "").strip():
                full_response_local = _strip_leading_orphan_colon(
                    _strip_leading_orphan_punct(str(_pit["content"]).strip())
                )
                if full_response_local:
                    del run.display_sequence[_pi]
                break
    if full_response_local:
        _paused_reason_already_segmented = bool(
            run.dm
            and getattr(run.task_conversation, "deathmatch_status", None) == "paused"
            and run.content_segments
            and run.content_segments[-1] == full_response_local
        )
        if not _paused_reason_already_segmented:
            run.content_segments.append(full_response_local)
        run.display_sequence.append({"type": "text", "content": full_response_local})
    joined_content = "\n\n<!-- segment_split -->\n\n".join(run.content_segments)

    # Error-terminated OR content-empty-with-tool-work loop:
    # never emit an empty/partial `_completed`. Substitute a
    # visible failure message so every downstream path
    # (finalize save, detached self-save, buffer replay,
    # legacy resume save) persists something the user can
    # actually see — an empty bubble is filtered by the
    # frontend and renders as "query with nothing below"
    # (conv 149ce886 msg f9d168bd, conv 38ce8810 msg 63f0e6ee).
    _had_tool_activity = bool(run.tool_results_accumulated or run.tool_call_events_accumulated)
    _terminal_content, _terminal_substituted = _compose_terminal_content(
        joined_content, run.loop_error, _had_tool_activity,
    )
    if _terminal_substituted:
        run.content_segments.append(_terminal_content)
        run.display_sequence.append({"type": "text", "content": _terminal_content})
        joined_content = _terminal_content

    # DEATHMATCH: grilling completion is now handled via
    # the subagent question/answer flow, not [GOAL_SUMMARY].
    full_reasoning_local = run.assistant_reasoning.strip()

    # Insert any remaining (post-last-tool_call) reasoning as a
    # final reasoning_step. Earlier per-turn reasoning chunks
    # were already flushed into display_sequence at each
    # tool_call boundary, so we only attach the trailing chunk
    # here. Fallback: if no per-turn chunks were captured but
    # we have full reasoning (e.g., no tool calls), attach it.
    trailing_reason = run.current_reasoning_segment.strip()
    has_inline_reasoning = any(
        it.get("type") == "reasoning_step" for it in run.display_sequence
    )
    if trailing_reason and run.display_sequence:
        _reasoning_item = {
            "type": "reasoning_step",
            "title": "💭 思考过程",
            "content": trailing_reason,
        }
        _last_text_idx = -1
        for _i in range(len(run.display_sequence) - 1, -1, -1):
            if run.display_sequence[_i].get("type") == "text":
                _last_text_idx = _i
                break
        if _last_text_idx >= 0:
            run.display_sequence.insert(_last_text_idx, _reasoning_item)
        else:
            run.display_sequence.append(_reasoning_item)
    elif not has_inline_reasoning and full_reasoning_local and run.display_sequence:
        # No per-turn reasoning chunks ever observed (and no
        # trailing leftover either): fall back to one global
        # reasoning block before the final answer.
        _reasoning_item = {
            "type": "reasoning_step",
            "title": "💭 思考过程",
            "content": full_reasoning_local,
        }
        _last_text_idx = -1
        for _i in range(len(run.display_sequence) - 1, -1, -1):
            if run.display_sequence[_i].get("type") == "text":
                _last_text_idx = _i
                break
        if _last_text_idx >= 0:
            run.display_sequence.insert(_last_text_idx, _reasoning_item)
        else:
            run.display_sequence.append(_reasoning_item)

    tr_json = None
    if run.tool_results_accumulated:
        logger.info(
            "DBG transform: search_queries_used=%s map=%s accum_calls=%s",
            run.search_queries_used,
            run.search_queries_by_call,
            [tr.get("call_id") for tr in run.tool_results_accumulated if tr.get("name") == "web_search"],
        )
        tr_json = _transform_tool_loop_results(
            run.tool_results_accumulated,
            search_queries_used=run.search_queries_used if run.search_queries_used else None,
            queries_by_call=run.search_queries_by_call or None,
        )
    if run.content_segments or run.interjection_items:
        if tr_json:
            tr_obj = json.loads(tr_json)
            tr_obj["display_sequence"] = resolve_persisted_display_sequence(
                run.display_sequence, tr_obj.get("agent_steps", []),
            )
            tr_obj["content_segments"] = run.content_segments
            tr_json = json.dumps(tr_obj, ensure_ascii=False)
        else:
            tr_json = json.dumps({
                "content_segments": run.content_segments,
                "display_sequence": resolve_persisted_display_sequence(
                    run.display_sequence, [],
                ),
            }, ensure_ascii=False)

    # Deathmatch: attach final deliverable download cards to the
    # final summary message. Three sources, merged in priority:
    #   1. _dm_final_attachments — collected from ALL previous
    #      turns via collect_final_deliverables_from_messages
    #      (populated when verdict is "done" or "partial_complete")
    #   2. _dm_last_turn_attachments — extracted from the most
    #      recent per-turn save (captured above during per-turn
    #      persistence). Critical fallback for paused/human_gate
    #      paths where _dm_final_attachments is empty.
    _dm_all_atts: list[dict] = []
    if run.dm_final_attachments:
        _dm_all_atts.extend(run.dm_final_attachments)
    if run.dm_last_turn_attachments:
        _dm_all_atts.extend(run.dm_last_turn_attachments)
    if _dm_all_atts:
        if tr_json:
            _dm_tr_obj = json.loads(tr_json)
        else:
            _dm_tr_obj = {
                "content_segments": run.content_segments,
                "display_sequence": run.display_sequence,
            }
        existing_atts = _dm_tr_obj.get("attachments") or []
        # Deduplicate by name, keeping the largest size.
        _att_by_name: dict[str, dict] = {}
        for _a in list(existing_atts) + _dm_all_atts:
            _nm = _a.get("name") or _a.get("filename") or ""
            if not _nm:
                continue
            _cur = _att_by_name.get(_nm)
            if _cur is None or (_a.get("size") or 0) > (_cur.get("size") or 0):
                _att_by_name[_nm] = _a
        _dm_tr_obj["attachments"] = list(_att_by_name.values())
        tr_json = json.dumps(_dm_tr_obj, ensure_ascii=False)

    # PHASE 2B: serialize structured tool_calls for persistence.
    from app.services.tool_history import build_persisted_tool_calls
    tool_calls_json_local = build_persisted_tool_calls(
        run.tool_call_events_accumulated
    )

    # conv 3b58af5b (R4, 2026-08-23): sanitize + localize +
    # PERSIST here, in the producer, BEFORE any subscriber is
    # notified. The old order broadcast `_completed` first and
    # left the DB write to the SSE consumer (or the cancelled
    # self-save path); a client disconnect in that window killed
    # the consumer before it saved AND skipped the self-save
    # branch — a fully-generated, audit-passed answer vanished
    # without any log line. Persistence must not depend on any
    # subscriber staying alive.
    _cap_content = _build_content_fallback(joined_content, tr_json)
    _cap_content, tr_json = await _sanitize_cited_content(
        _cap_content, tr_json,
    )
    # M4：出货 citations 载荷（服务端样式化 reference + in_text）
    _cap_content, tr_json = await _render_answer_citations(
        _cap_content, tr_json,
        getattr(run.cap_assistant, "citation_style", None),
    )
    if _cap_content:
        joined_content = _cap_content
    # Localize remote media BEFORE persisting/broadcasting so
    # the DB row, buffer done_data and every replay surface
    # carry identical content.
    _cap_content, tr_json = await _localize_media_for_persist(
        _cap_content, tr_json, run.user_id_local, run.username_local,
    )
    # Guard mirrors the consumer's _skip_empty_final: when the
    # deathmatch terminal status message was already persisted
    # with text + deliverables, an empty fallback bubble would
    # only duplicate its attachments.
    _dm_skip_empty = (
        not (_cap_content or "").strip() and run.state._dm_terminal_status_saved
    )
    producer_message_id: str | None = None
    if (_cap_content or "").strip() or (tr_json and not _dm_skip_empty):
        producer_message_id = await run.persist_final_answer(
            conv_id=run.cap_conversation_id,
            content=_cap_content,
            reasoning=full_reasoning_local,
            tr_json_str=tr_json,
            tc_json=tool_calls_json_local,
            # P2 (2026-09-05): 落账本轮最新 token 快照
            # （buffer 持有 context_info 事件的最新值），
            # 跨设备加载会话时前端可播种徽章。
            ctx_info=getattr(run.state._stream_buf, "context_info", None),
        )
        if producer_message_id:
            try:
                await run.state._stream_buf.mark_complete(producer_message_id)
            except Exception:
                pass

    completed_payload = {
        "_completed": {
            "joined_content": joined_content,
            "full_reasoning": full_reasoning_local,
            "tool_results_json": tr_json,
            "tool_calls_json": tool_calls_json_local,
            # R4: consumers (SSE + legacy resume) skip their own
            # save when this is set — single-writer persistence.
            "message_id": producer_message_id,
        }
    }
    await run.agent_state.broadcast(completed_payload)
    run.agent_state.is_running = False
    run.agent_state.completed_data = {
        "joined_content": joined_content,
        "full_reasoning": full_reasoning_local,
        "tool_results_json": tr_json,
    }
    await run.agent_state.broadcast_done()

    # Pre-generate LLM title BEFORE notifying resume subscribers
    # via the buffer's `done` event, so the title is already
    # present in `done_data` when the resume `done` payload is
    # resolved (avoids a race where the frontend marks the
    # conversation finished while the sidebar title is still
    # "新对话").
    _pre_title: str | None = None
    if run.sse_cancelled.is_set() and (joined_content or "").strip():
        try:
            async with AsyncSessionLocal() as _title_db:
                _conv_row_for_title = (await _title_db.execute(
                    select(Conversation).where(Conversation.id == run.cap_conversation_id)
                )).scalar_one_or_none()
                if _conv_row_for_title is not None:
                    _pre_title = await run.generate_title_bg(
                        _title_db, _conv_row_for_title, joined_content,
                    )
        except Exception:
            logger.exception("Pre-done title generation failed")
    if _pre_title:
        try:
            if run.state._stream_buf.done_data is None:
                run.state._stream_buf.done_data = {}
            run.state._stream_buf.done_data["title"] = _pre_title
        except Exception:
            pass

    # (R4) citation sanitize + media localization already ran
    # above, before the broadcast — the buffer's done payload
    # feeds resume replay and must carry the SAME clean
    # content/tool_results as the DB row (conv 8629bdfe).

    # Mark stream buffer as done
    try:
        await run.state._stream_buf.append({
            "done": True,
            "conversation_id": run.cap_conversation_id,
            "joined_content": joined_content,
            "full_reasoning": full_reasoning_local,
            "tool_results_json": tr_json,
        })
    except Exception:
        pass

    if run.sse_cancelled.is_set():
        if producer_message_id:
            # R4: already persisted pre-broadcast — nothing left
            # for the legacy self-save path to do.
            logger.info(
                "Agent loop completed after SSE cancellation; "
                "answer already persisted (msg %s)",
                producer_message_id,
            )
            return
        _cap_content = _build_content_fallback(joined_content, tr_json)
        _cap_content, tr_json = await _sanitize_cited_content(_cap_content, tr_json)
        _cap_content, tr_json = await _render_answer_citations(
            _cap_content, tr_json,
            getattr(run.cap_assistant, "citation_style", None),
        )
        # Localize remote media (same as the finalize path —
        # the self-save path persists the FULL final answer
        # when SSE is cancelled after loop completion, so it
        # must not bypass localization; conv 97ff355d).
        _cap_content, tr_json = await _localize_media_for_persist(
            _cap_content, tr_json, run.user_id_local, run.username_local,
        )
        # Guard: never self-save a completely empty bubble
        # (no text, no tool results, no tool calls). The
        # normal finalize path skips such messages via
        # _skip_empty_final; the self-save path persisted one
        # unconditionally, producing an empty card as the last
        # message (conv 4d9a5289).
        if not _cap_content and not tr_json and not tool_calls_json_local:
            logger.info(
                "Agent loop completed after SSE cancellation with empty payload; "
                "skipping self-save (terminal status already persisted or nothing to save)"
            )
            try:
                # 空终态：清掉渐进落库的空 streaming 行
                # 2026-11-02（C5）：与启动 sweep 同源判据 + 只按本 run 行 id
                # 精确删除（旧的按会话批量删 content 为空的 streaming 行会
                # 误删并发 run 的证据行）。
                try:
                    from app.api.chat_persistence import _delete_empty_streaming_row as _del_empty
                    _clean_row_id = getattr(run.state._stream_buf, "streaming_message_id", None)
                    if _clean_row_id:
                        async with AsyncSessionLocal() as _clean_db:
                            await _del_empty(_clean_db, _clean_row_id)
                            await _clean_db.commit()
                    else:
                        logger.debug(
                            "sse-cancel empty: no own streaming row id; "
                            "leaving cleanup to boot sweep"
                        )
                except Exception:
                    logger.debug("empty streaming row cleanup failed", exc_info=True)
                await run.state._stream_buf.mark_complete(None)
            except Exception:
                pass
            return
        logger.info("Agent loop completed after SSE cancellation, self-saving results")
        try:
            for attempt in range(3):
                try:
                    async with AsyncSessionLocal() as bg_db:
                        conv_row = (await bg_db.execute(
                            select(Conversation).where(Conversation.id == run.cap_conversation_id)
                        )).scalar_one_or_none()
                        if conv_row is None:
                            break
                        # 渐进落库：streaming 在途行原地更新翻 final
                        msg = await _save_assistant_terminal(
                            bg_db,
                            conversation_id=run.cap_conversation_id,
                            content=_cap_content,
                            reasoning=full_reasoning_local if full_reasoning_local else None,
                            tool_results=tr_json,
                            tool_calls=tool_calls_json_local,
                            context_info=None,
                            status="final",
                            # 绑定本 run 的 streaming 行（A4.9 I3：不反查僵尸行）
                            row_id=getattr(
                                run.state._stream_buf, "streaming_message_id", None),
                            interjection_anchor=interjection_anchor_from_buffer(
                                run.state._stream_buf),
                            interjection_items=(
                                json.dumps(run.interjection_items, ensure_ascii=False)
                                if getattr(run, "interjection_items", None) else None
                            ),
                        )
                        conv_row.updated_at = datetime.utcnow()
                        await bg_db.commit()
                        await bg_db.refresh(msg)
                        try:
                            await run.state._stream_buf.mark_complete(str(msg.id))
                        except Exception:
                            pass
                        try:
                            _final_title = await run.generate_title_bg(bg_db, conv_row, _cap_content)
                            if _final_title:
                                try:
                                    if run.state._stream_buf.done_data is None:
                                        run.state._stream_buf.done_data = {}
                                    run.state._stream_buf.done_data["title"] = _final_title
                                except Exception:
                                    pass
                        except Exception:
                            logger.exception("Background title generation failed (self-save path)")
                        logger.info("Agent self-save succeeded on attempt %d", attempt + 1)
                        break
                except Exception:
                    import traceback as _tb
                    _tb.print_exc()
                    await asyncio.sleep(2.0)
        except Exception:
            logger.exception("Agent self-save failed entirely")


async def _chat_agent_loop_impl(_state, **kw):
    _sync_chat_globals()
    _agent_queue = kw.get('_agent_queue')
    _agent_state = kw.get('_agent_state')
    _cap_assistant = kw.get('_cap_assistant')
    _cap_conversation_id = kw.get('_cap_conversation_id')
    _cap_latest_user_query = kw.get('_cap_latest_user_query')
    _cap_user_id = kw.get('_cap_user_id')
    _ctx_tokens = kw.get('_ctx_tokens')
    _ctx_window = kw.get('_ctx_window')
    _deathmatch_mgr = kw.get('_deathmatch_mgr')
    _generate_title_bg = kw.get('_generate_title_bg')
    _sse_cancelled = kw.get('_sse_cancelled')
    _user_id_local = kw.get('_user_id_local')
    _username_local = kw.get('_username_local')
    agent_loop = kw.get('agent_loop')
    conversation_id = kw.get('conversation_id')
    conversation_messages = kw.get('conversation_messages')
    coord_result = kw.get('coord_result')
    messages_for_model = kw.get('messages_for_model')
    strip_canary_streaming = kw.get('strip_canary_streaming')
    # NOTE (C-02c): the deathmatch terminal-status flag lives on
    # `_state._dm_terminal_status_saved` (mirrored as `run.state`).
    run = _AgentLoopRun()
    _ttft_t0 = time.monotonic()
    run.assistant_content = ""
    run.assistant_reasoning = ""
    # Pre-tool text gating: on tool-requiring turns (coordinator
    # route=tool_loop + expects_tools=True) content produced BEFORE
    # the first tool call is transient — it is dropped from the
    # persisted answer, so streaming it makes the user see a
    # "first answer" that later vanishes or duplicates the real
    # one (conv 149ce886: the model claimed "我进行了真实检索"
    # before the auto-invoked web_search). Gated text is discarded
    # on the first tool_call, released at iteration boundaries and
    # flushed at turn end otherwise. The hold is BOUNDED
    # (HOLD_MAX_SECONDS) so a turn where the coordinator
    # over-predicted tools but the model answered directly can
    # never park the whole answer silently until `done` — the
    # answer streams live instead of popping whole (2026-08-06).
    run.pre_tool_gate = PreToolGate()
    # Set when the agent loop terminated via an error event. The
    # completion block uses it to substitute a visible failure
    # message instead of persisting an empty/invisible turn.
    run.loop_error: str | None = None
    run.current_reasoning_segment = ""
    # A4.9 R2 N1（2026-10-14 输出单元契约修订）：预工具散文停车进
    # display_sequence（keep_evidence 存活），content 侧不再保留
    # 散文快照位（_released_prose 已退役——「散文+旧稿+重答」三段
    # 拼接病形从构造上消灭）。拼接链锚=_stitch_base。
    # 输出单元契约（2026-10-14）：finish_reason=length 截断续写链
    # 标记——链上文本跨工具边界拼接（唯一合法拼接）；audit_reset/
    # 压缩/新 turn 复位。_stitch_base=链锚（head 快照，keep_stitch
    # 回退点）。
    run.stitch_active = False
    run.stitch_base = ""
    # 遵循词 canary streaming strip: hold-back tails. LLM deltas are
    # 1-3 chars, so [遵循词:xxxxxx] is routinely split across chunks
    # and per-chunk regex strip misses it (conv 6227fb26 leak).
    run.canary_tail = ""
    run.reasoning_canary_tail = ""
    # 显示层元引用清洗（2026-09-18 思考卫生波）：与 canary 同级的
    # 确定性护栏——提示词禁止仍会概率性复发，思考对用户可见，
    # 命中“系统提示词/coordinator note/identity rules”等内部脚手架
    # 的短语由该尾缓冲清洗，跨分片拆词也不会泄漏。
    run.reasoning_meta_tail = ""
    run.content_segments: list[str] = []
    run.display_sequence: list[dict] = []
    run.tool_results_accumulated: list[dict] = []
    # PHASE 2B: collect raw tool_call events parallel to results.
    # Used to populate Message.tool_calls so structured history
    # can be replayed in future turns.
    run.tool_call_events_accumulated: list[dict] = []
    run.search_queries_used: list[str] = []
    # Per-call web_search query map (conv a3cfb421 2026-08-09):
    # call_id -> queries, so the persisted rounds display each
    # round's OWN queries instead of the last call's for all.
    run.search_queries_by_call: dict[str, list[str]] = {}
    # Last accumulated download-card set streamed to the client
    # (conv fbf5779b: emit the accumulated set on each tool_result
    # so the live count equals the persisted one).
    run.last_streamed_attachments: list[dict] = []
    # Deathmatch: deliverable file attachments collected when the
    # goal completes. Injected into the final summary message's
    # tool_results so download cards appear without relying on
    # per-tool-call attachments.
    run.dm_final_attachments: list[dict] = []
    # Deathmatch: attachments from the most recent per-turn save,
    # used as fallback when _dm_final_attachments is empty (e.g.
    # paused/human_gate paths where collect_final_deliverables was
    # never called). Ensures the final summary message carries
    # download cards for files generated in the last turn.
    run.dm_last_turn_attachments: list[dict] = []

    # Create stream buffer for this conversation
    try:
        _state._stream_buf = await stream_buffer_manager.create_buffer(
            _cap_conversation_id, _cap_user_id
        )
        await _state._stream_buf.set_sse_active(True)
    except Exception:
        # Buffer creation failed before the main try below — no
        # `_failed` would ever be broadcast, so the relay loop
        # (drain mode in setup-recovery) would spin on 10s pings
        # forever with an adopted slot. Broadcast failure and
        # terminate; the task's finally releases the slot.
        logger.exception(
            "Stream buffer creation failed for conversation %s",
            _cap_conversation_id,
        )
        try:
            await _agent_state.broadcast({"_failed": "流缓冲创建失败"})
            await _agent_state.broadcast_done()
        except Exception:
            pass
        return

    # 渐进落库（2026-10-06，conv ae9aa092）：run 开始即建
    # delivery_status='streaming' 的 assistant 行 + 周期刷写器——
    # 进程被杀死（部署/崩溃/OOM）时已流式正文存活于 DB，启动 sweep
    # 翻 interrupted 后带徽标展示，不再整段蒸发。死磕按轮落库
    # 不在此路径（其逐轮保存已有独立契约）。
    # 渐进落库证据序列化上限：工具结果可能是几十 KB/条，长任务每 5s 写一次会
    # 放大写入；超限部分显式标记（合法 JSON），完整内容仍随终态落库。
    _PF_EVIDENCE_MAX_CHARS = 512_000

    def _pf_evidence_json(items: list, label: str) -> str:
        kept, size = [], 0
        for item in items:
            chunk = json.dumps(item, ensure_ascii=False)
            if size + len(chunk) > _PF_EVIDENCE_MAX_CHARS:
                kept.append({
                    "_truncated": True,
                    "omitted_items": len(items) - len(kept),
                    "note": f"渐进落库仅保留前 {len(kept)} 条{label}；完整内容随终态落库",
                })
                break
            kept.append(item)
            size += len(chunk)
        return json.dumps(kept, ensure_ascii=False)

    run.streaming_row_id: str | None = None
    if _deathmatch_mgr is None:
        try:
            async with AsyncSessionLocal() as _pf_db:
                _pf_row = Message(
                    conversation_id=_cap_conversation_id,
                    role="assistant",
                    content="",
                    delivery_status="streaming",
                )
                _pf_db.add(_pf_row)
                await _pf_db.commit()
                await _pf_db.refresh(_pf_row)
                run.streaming_row_id = str(_pf_row.id)
                # 行 id 挂到 buffer（A4.9 r1 M2）：detach/relay/消费端
                # 的空行清理按行 id 精确删除，不跨 run 误删下一 run 的新行。
                try:
                    _state._stream_buf.streaming_message_id = run.streaming_row_id
                except Exception:
                    pass
        except Exception:
            logger.warning(
                "streaming row creation failed (fail-open)", exc_info=True)

    async def _pf_write(text: str) -> None:
        if not run.streaming_row_id:
            return
        # 2026-11-01（conv 861cb5af）：渐进落库不止正文——工具调用/工具结果/
        # 思考同样是「被杀后能交代清楚跑了什么」的证据。纯工具型长任务正文
        # 长期为空，旧实现只写 content=空 → 重启清扫把整行当空壳删掉。
        # 2026-11-01 inline interjections: assistant_content is reset at each
        # boundary, so an interrupted row must show ALL visible segments, not
        # just the post-interjection tail.
        _pf_text = text
        if run.content_segments:
            _pf_text = SEGMENT_SEPARATOR.join([*run.content_segments, text or ""])
        payload: Dict[str, Any] = {"content": _pf_text}
        try:
            if run.tool_call_events_accumulated:
                payload["tool_calls"] = _pf_evidence_json(
                    run.tool_call_events_accumulated, "工具调用")
            if run.tool_results_accumulated:
                payload["tool_results"] = _pf_evidence_json(
                    run.tool_results_accumulated, "工具结果")
            if (run.assistant_reasoning or "").strip():
                payload["reasoning_content"] = run.assistant_reasoning
        except Exception:
            logger.debug("progressive evidence serialize failed (fail-open)", exc_info=True)
        try:
            async with AsyncSessionLocal() as _pf_db:
                # Core 级 UPDATE：不触发 ORM/sync 事件（streaming
                # 行本就不下发同步）；仅当行仍在途时刷写。
                await _pf_db.execute(
                    sa_update(Message)
                    .where(Message.id == run.streaming_row_id,
                           Message.delivery_status == "streaming")
                    .values(**payload)
                )
                await _pf_db.commit()
        except Exception:
            logger.debug("progressive flush failed (fail-open)", exc_info=True)

    run.pf = ProgressiveFlusher(_pf_write)

    # F1-1: per-stream part translator. Legacy events keep flowing
    # unchanged; each translated event additionally emits
    # part_started/part_delta/part_updated so v2 clients can drive
    # the timeline by stable part_id instead of positional heuristics.
    part_translator = PartTranslator()

    async def _put(event_dict):
        # F1-1: translate + broadcast part events FIRST. Ordering
        # matters: the frontend activates partMode on the first part
        # event; legacy events arriving afterwards only update
        # accumulators (content string, tool list), never the
        # displaySequence — preventing double-rendered items.
        #
        # Ordering between buffer and broadcast: the stream buffer
        # is the resume-replay authority — a client that
        # disconnects and resumes must be able to reconstruct
        # EVERYTHING it already displayed. Recording the buffer
        # BEFORE broadcasting guarantees the replay snapshot is
        # never behind the live stream; broadcasting first opened
        # a window where the client received an event that the
        # snapshot missed, so the resume replay regressed the
        # timeline and already-displayed content vanished
        # mid-answer (refresh restored it from the DB).
        part_events = part_translator.translate(event_dict)
        for _pe in part_events:
            try:
                await _state._stream_buf.append(_pe)
            except Exception:
                pass
            await _agent_state.broadcast(_pe)

        # Update registry state
        if "content" in event_dict:
            _agent_state.content += event_dict["content"]
        elif "content_segment" in event_dict:
            seg = event_dict["content_segment"]
            if seg.strip():
                _agent_state.content_segments.append(seg)
        elif "reasoning_content" in event_dict:
            _agent_state.reasoning += event_dict["reasoning_content"]
        elif "tool_call" in event_dict:
            _agent_state.tool_calls.append(event_dict["tool_call"])
        elif "tool_result" in event_dict:
            _agent_state.tool_results.append(event_dict["tool_result"])
        elif "attachments" in event_dict:
            _agent_state.file_attachments = list(event_dict["attachments"])
        elif "iteration" in event_dict:
            _agent_state.iteration = event_dict["iteration"]
        elif "agent_step" in event_dict:
            pass  # pass through to broadcast

        # Also write to persistent stream buffer (before the
        # broadcast — see ordering note above).
        try:
            await _state._stream_buf.append(event_dict)
        except Exception:
            pass

        # Broadcast to all subscribers (last: buffer already
        # recorded the event, so a disconnect right after the
        # broadcast can always be recovered via resume replay).
        await _agent_state.broadcast(event_dict)

    async def _persist_final_answer(
        *,
        conv_id: str,
        content: str,
        reasoning: str | None,
        tr_json_str: str | None,
        tc_json: str | None,
        ctx_info: dict | None = None,
    ) -> str | None:
        """conv 3b58af5b (R4, 2026-08-23): single-writer producer-
        side persistence for the final answer. Runs BEFORE the
        `_completed` broadcast so a client disconnect in the
        completion window can never lose a fully-generated answer.
        Returns the message id, or None on failure (callers fall
        back to the legacy consumer/self-save paths)."""
        try:
            async with AsyncSessionLocal() as db:
                conv_row = (await db.execute(
                    select(Conversation).where(Conversation.id == conv_id)
                )).scalar_one_or_none()
                if conv_row is None:
                    return None
                # 渐进落库（2026-10-06）：有 streaming 在途行则原地
                # 更新翻 final，不插入重复行。
                msg = await _save_assistant_terminal(
                    db,
                    conversation_id=conv_id,
                    content=content,
                    reasoning=reasoning or None,
                    tool_results=tr_json_str,
                    tool_calls=tc_json,
                    context_info=(
                        json.dumps(ctx_info, ensure_ascii=False) if ctx_info else None
                    ),
                    status="final",
                    row_id=getattr(
                        getattr(getattr(run, "state", None), "_stream_buf", None),
                        "streaming_message_id", None),
                    interjection_anchor=interjection_anchor_from_buffer(
                        getattr(getattr(run, "state", None), "_stream_buf", None)),
                    interjection_items=(
                        json.dumps(run.interjection_items, ensure_ascii=False)
                        if getattr(run, "interjection_items", None) else None
                    ),
                )
                conv_row.updated_at = datetime.utcnow()
                await db.commit()
        except Exception:
            logger.exception(
                "Producer-side persistence failed for conversation %s", conv_id
            )
            return None
        # A4.9 fix (Important-1): commit succeeded — the row is
        # durable. Exceptions during refresh/close must NOT be
        # reported as "not persisted" (callers would fall back to
        # the legacy save path and insert a duplicate row).
        try:
            return str(msg.id)
        except Exception:
            logger.exception(
                "Persisted message id unavailable for conversation %s "
                "(row committed; falling back to legacy consumer save)",
                conv_id,
            )
            return None

    # 每轮开始时推送上下文 token 用量（前端头部徽章）。
    if _ctx_tokens > 0:
        await _put({"context_info": {
            "tokens": _ctx_tokens,
            "context_length": _ctx_window,
        }})

    task_db = AsyncSessionLocal()
    # A4.9 closure residual: optimistic sync snapshot — captured at
    # bind time so the very first verdict sync already detects
    # concurrent user actions (pause/stop/resume) instead of
    # clobbering them (last-writer-wins).
    run.dm_sync_snapshot: dict = {}
    run.dm_sync_frozen = frozenset()
    try:
        task_conversation = await task_db.get(Conversation, _cap_conversation_id)
        task_user = await task_db.get(User, _cap_user_id)
        # DEATHMATCH: Rebind deathmatch manager to task_db conversation
        if _deathmatch_mgr is not None and task_conversation is not None:
            _rebind_deathmatch_manager_conversation(_deathmatch_mgr, task_conversation)
            agent_loop.deathmatch_manager = _deathmatch_mgr
            from app.services.deathmatch_service import (
                DEATHMATCH_SYNC_FIELDS as _DM_SYNC_FIELDS,
                _ensure_creative_judged,
            )
            import copy as _copy
            # Deep-copied seed: several fields are mutated in place
            # (events/reflections/plan), so aliasing the live row's
            # objects would hide changes (A4.9 r-residual F2).
            run.dm_sync_snapshot = {
                _f: _copy.deepcopy(getattr(task_conversation, _f))
                for _f in _DM_SYNC_FIELDS
                if hasattr(task_conversation, _f)
            }
            async def _ensure_deathmatch_contract() -> None:
                """Lazy-generate criteria/plan after an active frame is visible.

                Also covers zombie recovery, which becomes active only after
                the normal initial frame. The first agent turn receives the
                newly generated plan directive on the shared message list.
                """
                if not _deathmatch_mgr.is_goal_active:
                    return
                # Final-round/zombie readiness is lazy too; early-stop rounds
                # already have it and this helper is current-round idempotent.
                try:
                    await _deathmatch_mgr._ensure_grilling_readiness_for_contract(task_db)
                except Exception as exc:
                    logger.warning("lazy grilling readiness failed (non-blocking): %s", exc)
                goal_refined = False
                try:
                    # Independent retry: a recorded report must not suppress a
                    # later refinement when the authoritative goal still lacks
                    # any of its defaults.
                    goal_refined = await _deathmatch_mgr._refine_goal_with_readiness_if_needed(task_db)
                except Exception as exc:
                    logger.warning("lazy goal refinement failed: %s", exc)
                    _deathmatch_mgr.pause("目标默认假设重建失败，请重试")
                    await _commit_deathmatch_contract(task_db, task_conversation)
                    await _put({"deathmatch_verdict": _deathmatch_mgr.get_verdict_dict()})
                    return
                # Classification is required even when a persisted draft lets
                # us skip generation (cold process cache + plan regeneration).
                try:
                    await _ensure_creative_judged(str(task_conversation.deathmatch_goal or ""))
                except Exception as exc:
                    logger.warning("lazy creative judgment failed (non-blocking): %s", exc)
                # Drafting remains lazy (after the active frame) and idempotent.
                if not getattr(task_conversation, "deathmatch_bible_draft", None):
                    try:
                        await _deathmatch_mgr._draft_bible_from_grilling(
                            task_db, str(task_conversation.deathmatch_goal or "")
                        )
                    except Exception as exc:
                        logger.warning("lazy bible drafting failed (non-blocking): %s", exc)
                if not getattr(task_conversation, "deathmatch_acceptance_criteria", None):
                    try:
                        await _deathmatch_mgr._synthesize_acceptance_criteria()
                    except Exception as exc:
                        logger.warning("lazy criteria synthesis failed (non-blocking): %s", exc)
                if not _deathmatch_mgr.is_goal_active:
                    await _commit_deathmatch_contract(task_db, task_conversation)
                    await _put({"deathmatch_verdict": _deathmatch_mgr.get_verdict_dict()})
                    return
                if not getattr(task_conversation, "deathmatch_plan", None):
                    try:
                        await _deathmatch_mgr.generate_goal_plan(
                            task_db, fresh_goal=goal_refined,
                        )
                    except Exception as exc:
                        logger.warning("lazy goal-plan generation failed (non-blocking): %s", exc)
                await _commit_deathmatch_contract(task_db, task_conversation)
                plan_directive = _deathmatch_mgr.build_plan_directive()
                _replace_deathmatch_plan_directive(messages_for_model, plan_directive)
                if getattr(task_conversation, "deathmatch_plan", None):
                    _dm_plan_init = _deathmatch_mgr.get_verdict_dict()
                    _dm_plan_init["verdict"] = None
                    _dm_plan_init["reason"] = None
                    _dm_plan_init["last_verification"] = None
                    await _put({"deathmatch_verdict": _dm_plan_init})

            # Emit initial deathmatch status for frontend.
            # R4（诊断机制 C#4）：初始事件必须携带 plan_steps/
            # plan_version（get_verdict_dict 单源）——此前手拼
            # 最小集缺 plan，前端打开会话首轮清单即空。
            # M3（评审）：初始帧不得携带上一轮残留 verdict/reason
            # （会误触"继续推进..."脉冲）与 last_verification
            # （400 文件快照的 payload 膨胀）。
            _dm_init = _deathmatch_mgr.get_verdict_dict()
            _dm_init["verdict"] = None
            _dm_init["reason"] = None
            _dm_init["last_verification"] = None
            await _put({"deathmatch_verdict": _dm_init})
            # The active stage is now visible. Only then spend time on the
            # criteria/plan LLM calls; answer endpoints no longer block on them.
            await _ensure_deathmatch_contract()
    except Exception:
        logger.warning("Agent task failed to load conversation/user from fresh DB session")
        await _agent_queue.put({"_failed": "DB session init failed"})
        try:
            await task_db.close()
        except Exception:
            pass
        return

    # DEATHMATCH: Generate grilling questions during grilling phase
    _grilling_generated = False
    if _deathmatch_mgr and _deathmatch_mgr.is_grilling:
        # First, try to recover from a zombie grilling state
        # (all tasks completed but goal never synthesized).
        recovered = await _deathmatch_mgr.try_recover_stalled_grilling(task_db)
        if recovered:
            await _commit_deathmatch_contract(task_db, task_conversation)
            await _put({
                "deathmatch_verdict": {
                    "status": "active",
                    "verdict": "grilling_recovered",
                    "reason": "盘问已完成，目标已合成，死磕模式已恢复",
                    "turns": _deathmatch_mgr._conv.deathmatch_turns or 0,
                    "max_turns": _deathmatch_mgr._conv.deathmatch_max_turns or 0,
                    "grilling_completed": _deathmatch_mgr._conv.deathmatch_grilling_completed or 0,
                    "grilling_total": _deathmatch_mgr._conv.deathmatch_grilling_total or 0,
                    "message": "盘问阶段已完成，死磕目标执行已恢复",
                }
            })
            # Recovery also becomes active after the normal initial frame;
            # generate its contract now instead of leaving plan/criteria null.
            await _ensure_deathmatch_contract()
            _grilling_generated = False
        else:
            try:
                grilling_questions = await _deathmatch_mgr.generate_grilling_questions(
                    query=_cap_latest_user_query,
                    db=task_db,
                    user_id=_cap_user_id,
                    assistant_id=_cap_assistant.id if _cap_assistant else None,
                )
                await _commit_deathmatch_contract(task_db, task_conversation)
                completed, total = _deathmatch_mgr.grilling_progress
                round_num = _deathmatch_mgr._conv.deathmatch_grilling_round or 1
                round_total = _deathmatch_mgr._conv.deathmatch_grilling_round_total or 3
                await _put({
                    "deathmatch_verdict": {
                        "status": "grilling",
                        "verdict": "grilling_questions_generated",
                        "reason": f"已生成第{round_num}轮{total}个盘问问题",
                        "turns": 0,
                        "max_turns": 0,
                        "grilling_completed": completed,
                        "grilling_total": total,
                        "grilling_round": round_num,
                        "grilling_round_total": round_total,
                        "message": f"盘问阶段 第{round_num}/{round_total}轮 — 请回答{total}个问题以明确目标",
                        "grilling_questions": grilling_questions,
                    }
                })
                _grilling_generated = True
            except Exception:
                logger.exception("Failed to generate grilling questions")
                await _put({
                    "deathmatch_verdict": {
                        "status": "grilling",
                        "verdict": "grilling_generation_failed",
                        "reason": "盘问问题生成失败",
                        "turns": 0,
                        "max_turns": 0,
                        "grilling_completed": 0,
                        "grilling_total": 0,
                        "grilling_round": 0,
                        "grilling_round_total": 0,
                        "message": "盘问问题生成失败，请重试或关闭死磕模式",
                    }
                })
                _grilling_generated = True

    # Release the setup session before the (potentially long) agent loop.
    try:
        await task_db.close()
    except Exception:
        pass
    task_db = None

    # C-02c: bind the immutable run context + callables the extracted
    # event bridge needs (formerly closure cells of this function).
    run.state = _state
    run.agent_state = _agent_state
    run.agent_queue = _agent_queue
    run.dm = _deathmatch_mgr
    run.put = _put
    run.conversation_id = conversation_id
    run.cap_conversation_id = _cap_conversation_id
    run.cap_assistant = _cap_assistant
    run.user_id_local = _user_id_local
    run.username_local = _username_local
    run.ctx_window = _ctx_window
    run.agent_loop = agent_loop
    run.messages_for_model = messages_for_model
    run.task_user = task_user
    run.task_conversation = task_conversation
    run.coord_result = coord_result
    run.conversation_messages = conversation_messages
    run.ttft_t0 = _ttft_t0
    run.strip_canary_streaming = strip_canary_streaming
    run.sse_cancelled = _sse_cancelled
    run.generate_title_bg = _generate_title_bg
    run.persist_final_answer = _persist_final_answer

    try:
        # DEATHMATCH: If in grilling phase, skip the agent loop.
        # Grilling questions were already generated and emitted above.
        # The agent loop should only run during the active goal phase.
        if _deathmatch_mgr and _deathmatch_mgr.is_grilling and _grilling_generated:
            run.content_segments.append(f"盘问阶段：已生成 {_deathmatch_mgr._conv.deathmatch_grilling_total or 0} 个问题，等待回答")
            run.display_sequence.append({"type": "text", "content": "盘问阶段：请回答上方的问题以明确目标"})
            await _put({"done": True})
        elif _deathmatch_mgr and task_conversation.deathmatch_status == "paused":
            # A lazy contract failure must stop before ANY agent/tool turn,
            # not merely skip plan generation. The persisted reason is the
            # only terminal content for this request.
            reason = str(task_conversation.deathmatch_reason or "死磕任务已暂停")
            run.content_segments.append(reason)
            run.display_sequence.append({"type": "text", "content": reason})
            await _put({"done": True})
        else:
            await _drive_agent_events(run)

        # Strip a leading orphan colon HERE (not only at
        # _compose_terminal_content) because the frontend renders
        # the display_sequence / content_segments copies — a
        # content-only strip would leave the dangling '：' visible
        # in v2 rendering (same dual-surface lesson as conv 8629bdfe).
        await _finalize_agent_run(run)
    except asyncio.CancelledError:
        await _handle_agent_loop_cancel(run)
        logger.info("Agent loop task cancelled for conversation %s", _cap_conversation_id)
        raise
    except Exception as exc:
        logger.exception("Agent loop task failed: %s", exc)
        _agent_state.is_running = False
        _agent_state.error = str(exc)
        await _agent_state.broadcast({"_failed": str(exc)})
        await _agent_state.broadcast_done()
        try:
            await _state._stream_buf.mark_complete(None)
        except Exception:
            pass
        # DEATHMATCH (conv 6b0faf81): persist the failure as a
        # visible paused terminal state instead of a silent
        # active→paused zombie flip by the reconcile later. The
        # user must see WHY the goal loop stopped and that they
        # can resume.
        await _persist_deathmatch_failure(
            _cap_conversation_id, _deathmatch_mgr,
            f"运行异常中断: {type(exc).__name__}: {str(exc)[:300]}",
            (
                "死磕模式运行异常中断。可发送任意消息继续推进目标；"
                "若反复中断请检查模型服务或调整目标。"
            ),
            expected_agent_state=_agent_state,
        )
    finally:
        try:
            if task_db is not None:
                await task_db.close()
        except Exception:
            pass
        # Identity-checked: a stale task (its slot re-reserved by
        # a newer request) must never pop the new run's state.
        await _agent_registry.unregister(_cap_conversation_id, expected=_agent_state)

_HISTORY_LOGGER = logging.getLogger(__name__)


async def _select_history_and_prefix(
    conversation, conversation_messages, config, conversation_id: str, llm=None,
):
    """P4: 2D window selection + (deathmatch-inclusive) truncation prefix."""
    from app.services.agent_loop_context import _resolve_context_length
    from app.services.history_window_service import (
        build_deathmatch_disclosure,
        resolve_history_token_budget,
        select_history_window_meta,
    )

    selection = select_history_window_meta(
        conversation_messages,
        config.agent_conversation_context_limit,
        resolve_history_token_budget(config, _resolve_context_length(llm)),
        mode=config.agent_context_window_mode,
        min_messages=config.agent_conversation_min_messages,
    )
    conv_msgs = selection.messages
    dropped = list(selection.dropped)
    prefix = ""
    if dropped and bool(config.agent.get("context_continuity_enabled", False)):
        # P4: deathmatch is no longer silently dropped — its cross-round
        # summary stays in force and the truncation prefix carries the
        # plan/settled/retracted state from the same conversation row.
        deathmatch = bool(getattr(conversation, "deathmatch_mode", False))
        try:
            from app.db.database import AsyncSessionLocal as _db_session
            from app.services.context_continuity_service import build_truncation_prefix

            async with _db_session() as cc_db:
                prefix = await asyncio.wait_for(
                    build_truncation_prefix(
                        cc_db, conversation_id, dropped,
                        all_messages=conversation_messages,
                        deathmatch=deathmatch,
                    ),
                    timeout=2.0,
                )
                await cc_db.commit()
        except Exception:
            _HISTORY_LOGGER.warning(
                "context continuity prefix failed (fail-open)", exc_info=True,
            )
            prefix = build_deathmatch_disclosure(len(dropped)) if deathmatch else ""
    return conv_msgs, prefix


async def _memory_v2_constraint_hooks(messages_for_model, latest_user_query,
                                      user_id, conversation_id) -> None:
    """W1-2（记忆/长程 v2，2026-10-23）：用户轮约束抽取→pin + 每轮 system 段
    重放（压缩后必须存活——ConstraintRot 验收面；mode=enforce 注入，
    shadow/off 无感）。B1 信任门在服务内 fail-closed；此处 fail-open，
    钩子失败不阻断对话轮。"""
    try:
        from app.db.database import AsyncSessionLocal
        from app.services.memory_constraint_service import (
            build_replay_message_for, pin_constraints_from_user_turn)
        if latest_user_query:
            await pin_constraints_from_user_turn(
                AsyncSessionLocal, user_id, str(conversation_id),
                str(latest_user_query))
        pin_msg = await build_replay_message_for(
            AsyncSessionLocal, user_id, str(conversation_id))
        if pin_msg:
            messages_for_model.insert(1, pin_msg)
            logger.info("pinned constraints replayed (conv=%s)", conversation_id)
    except Exception:
        logger.debug("memory-v2 constraint hooks failed-open", exc_info=True)


def _qwen_sampling_for(assistant, wire_provider_type):
    """Resolve Qwen3.8(Local) thinking/non-thinking sampling sets (wave-7).

    Extracted from _chat_run_tool_loop to keep the transport function bounded;
    behavior is byte-for-byte identical (NULL columns keep the model-card
    defaults; only this provider uses the sets).
    """
    _thinking_sampling: dict = {}
    _non_thinking_sampling: dict = {}
    _preserve_thinking: bool | None = None
    if wire_provider_type != "qwen3.8_vllm":
        return _thinking_sampling, _non_thinking_sampling, _preserve_thinking
    from app.model_gateway.profiles import get_thinking_profile
    _q38_profile = get_thinking_profile("qwen3.8_vllm")
    QWEN38_VLLM_THINKING_DEFAULTS = _q38_profile.sampling_defaults(True)
    QWEN38_VLLM_NON_THINKING_DEFAULTS = _q38_profile.sampling_defaults(False)
    _thinking_sampling = {
        k: getattr(assistant, f"thinking_{k}", None)
        for k in QWEN38_VLLM_THINKING_DEFAULTS
    }
    _thinking_sampling = {
        k: (v if v is not None else QWEN38_VLLM_THINKING_DEFAULTS[k])
        for k, v in _thinking_sampling.items()
    }
    _non_thinking_sampling = {
        k: getattr(assistant, k, None)
        for k in QWEN38_VLLM_NON_THINKING_DEFAULTS
    }
    _non_thinking_sampling = {
        k: (v if v is not None else QWEN38_VLLM_NON_THINKING_DEFAULTS[k])
        for k, v in _non_thinking_sampling.items()
    }
    # M1 (A4.9): NULL column must keep the "default ON" contract
    _preserve_thinking = (
        getattr(assistant, "preserve_thinking", True)
        if getattr(assistant, "preserve_thinking", True) is not None
        else True
    )
    return _thinking_sampling, _non_thinking_sampling, _preserve_thinking


async def _chat_run_tool_loop(_state, **kw):
    _sync_chat_globals()
    _finalize_and_emit_done = kw.get('_finalize_and_emit_done')
    _reserved_agent_state = kw.get('_reserved_agent_state')
    _spawn_interrupted_save = kw.get('_spawn_interrupted_save')
    _user_id_local = kw.get('_user_id_local')
    _username_local = kw.get('_username_local')
    _wire_provider_type = kw.get('_wire_provider_type')
    assistant = kw.get('assistant')
    chat_session = kw.get('chat_session')
    conversation = kw.get('conversation')
    conversation_id = kw.get('conversation_id')
    conversation_messages = kw.get('conversation_messages')
    coordinator_llm = kw.get('coordinator_llm')
    current_user = kw.get('current_user')
    db = kw.get('db')
    latest_user_query = kw.get('latest_user_query')
    llm_service = kw.get('llm_service')
    req = kw.get('req')
    request = kw.get('request')
    search_query = kw.get('search_query')
    skill_content = kw.get('skill_content')
    skill_files = kw.get('skill_files')
    wire_provider_type = kw.get('wire_provider_type')
    """Phase 1.2: New tool-calling agent loop — LLM decides which tools to call.

    The agent loop runs in a detached asyncio.Task so it completes
    even if the SSE connection is cancelled by sse-starlette.
    Events are bridged via an asyncio.Queue.
    """
    pass

    # 4.8: refresh the reservation at setup milestones so the
    # provisional reaper never kills a legitimately-slow setup
    # (deathmatch classify + coordinator pre-pass LLM calls).
    if _reserved_agent_state is not None and _reserved_agent_state.provisional:
        _reserved_agent_state.reserved_at = time.time()

    import app.tools

    from app.services.memory_service import build_shared_agent_context
    from app.core.config import get_config as _gc
    from app.db.database import AsyncSessionLocal as _AsyncSessionLocal
    _cfg = _gc()

    async def _memory_ctx() -> str | None:
        """§5.2 工程要求 1：召回管线与 coordinator/准备工作并发（取 max 非 sum）。
        独立 DB 会话（AsyncSession 不可并发共用）；失败回退 None 走旧方案。
        2026-10-04：改 retrieve_with_meta 收本轮注入 id（P1-① 采纳回写输入）。"""
        pass
        from app.services.memory_runtime_state import memory_runtime_enabled as _mem_rt
        if not (_mem_rt(_cfg) and _cfg.memory.get("retrieval_enabled")):
            logger.warning("[SETUP-TRACE] memory_ctx skip (disabled)")
            return None
        logger.warning("[SETUP-TRACE] memory_ctx enter")
        try:
            from app.services import memory_retrieval_service
            # W6-1（记忆/长程 v2，2026-10-23）：模型分档——窗口 ≥128k 走
            # hybrid（现状形态），显式/分档 raw 跳过设计型注入（fail-open：
            # 分档异常按 hybrid 处理）。A/B 前默认路径与现状逐字节一致。
            try:
                from app.services.agent_loop_context import _resolve_context_length
                from app.services.memory_tiering_service import (
                    injection_enabled, select_memory_mode)
                _mode = select_memory_mode(_resolve_context_length(llm_service))
            except Exception:
                _mode = "hybrid"
            if not injection_enabled(_mode):
                logger.info("memory tier=raw — designed injection skipped")
                return None
            async with _AsyncSessionLocal() as mem_db:
                _ctx, _ids, _top = await memory_retrieval_service.retrieve_with_meta(
                    mem_db, current_user.id, conversation_messages,
                    conversation_id=conversation_id)
                # Keep the parent closure's list object identity: the
                # terminal `_finalize_and_emit_done` reads the parent's
                # `_injected_mem_ids` for adoption feedback.
                _state._injected_mem_ids[:] = list(_ids or [])
                logger.warning("[SETUP-TRACE] memory_ctx exit ok ids=%d", len(_state._injected_mem_ids))
                return _ctx
        except Exception:
            logger.warning("[SETUP-TRACE] memory_ctx exit exc")
            logger.exception("New memory retrieval failed, falling back")
            return None

    # --- 与 sys_prompt 无关的准备工作前移（§5.2 工程要求 1：coordinator 与召回并发）---
    messages_for_model = [{"role": "system", "content": ""}]  # placeholder，sys_prompt 就绪后回填
    _config = get_config()
    conv_msgs, _context_prefix = await _select_history_and_prefix(
        conversation, conversation_messages, _config, conversation_id,
        llm=llm_service,
    )
    # PHASE 2A + 2B: rebuild structured OpenAI history so multi-turn
    # conversations show the model genuine assistant.tool_calls / tool
    # message pairs instead of opaque content text. Without this the
    # model loses the "I called tools and got results" pattern around
    # turn 5-6 and stops invoking tools (same failure mode hermes-agent
    # PR #3528 fixed).
    from app.services.agent_service import _sanitize_history_content
    from app.services.tool_history import (
        build_history_message,
        rebuild_structured_history,
        sanitize_api_messages,
    )
    for msg in conv_msgs:
        if msg.role == "assistant" and getattr(msg, "tool_calls", None):
            messages_for_model.extend(
                rebuild_structured_history(
                    role="assistant",
                    content=_sanitize_history_content(msg.content),
                    tool_calls_json=msg.tool_calls,
                    tool_results_json=getattr(msg, "tool_results", None),
                    reasoning_content=getattr(msg, "reasoning_content", None),
                )
            )
        else:
            # build_history_message applies the conv-a040c24e citation
            # neutralization for assistant rows only (users untouched);
            # reasoning_content round-trips via the same helper.
            messages_for_model.append(
                build_history_message(
                    msg.role,
                    _sanitize_history_content(msg.content),
                    reasoning_content=getattr(msg, "reasoning_content", None),
                )
            )
    messages_for_model = sanitize_api_messages(messages_for_model)
    # P4: fixed per-tool-result cap (cheap, prefix-stable) before compression
    # is considered.  0 (default) preserves the historical history exactly.
    _tool_cap = _config.agent_history_tool_result_max_tokens
    if _tool_cap > 0 and messages_for_model:
        from app.services.history_window_service import cap_tool_results
        messages_for_model, _capped_n = cap_tool_results(messages_for_model, _tool_cap)
        if _capped_n:
            logger.info(
                "history tool-result cap: %d message(s) capped at %d tokens",
                _capped_n, _tool_cap,
            )
    _has_compaction_summary = any(
        isinstance(m, dict)
        and str(m.get("content") or "").lstrip().startswith(
            "[CONTEXT COMPACTION"
        )
        for m in messages_for_model
    )
    # W1-2 约束固定（记忆/长程 v2，2026-10-23）：抽取→pin + 每轮 system 段
    # 重放（压缩后必须存活——ConstraintRot 验收面；helper 见 _memory_v2_constraint_hooks）
    await _memory_v2_constraint_hooks(
        messages_for_model, latest_user_query, current_user.id, conversation_id)
    if _context_prefix and not _has_compaction_summary:
        messages_for_model.insert(1, {
            "role": "system",
            "content": _context_prefix,
            "_ephemeral": "context_continuity",
        })

    # W17-P1/P2（复审 IMPORTANT-1，2026-10-10）：轮次门控文本抽取必须在
    # deathmatch 合成注入（build_context_messages 跨轮摘要 / 计划指令，
    # 均为 user 角色）**之前**——否则合成文本混入近 3 条 ctx，非代码死磕
    # 任务可被伪激活（dd3b2408 同类危害的新通道）。
    from app.services.vibeweaver_turn_gate import extract_recent_user_text as _vw_extract
    _vw_text, _vw_ctx = _vw_extract(messages_for_model)

    # PHASE 3: build the iteration (sub-task) LLM. If the assistant
    # has subtask_custom_* fields populated, spin up a separate
    # cheaper client for tool-calling iterations; otherwise reuse
    # the main llm_service. AgentLoop already forces iteration
    # extra_body to disable thinking either way.
    iteration_llm, iteration_provider_type = (
        agent_service.create_iteration_llm_service(assistant, main_llm=llm_service)
    )
    if iteration_llm is not llm_service:
        logger.info(
            "Phase 3: subtask LLM enabled (provider=%s)", iteration_provider_type
        )

    # DEATHMATCH: Wire up deathmatch manager if conversation is in deathmatch mode
    _deathmatch_mgr: DeathmatchManager | None = None
    _blocked_tools: set[str] = set()
    if conversation.deathmatch_mode:
        _deathmatch_mgr = DeathmatchManager(conversation)
        # P0: judge/verifier inherit the assistant's model client.
        _deathmatch_mgr.set_assistant_llm(llm_service)
        # Block background_task during deathmatch — all work must
        # stay inline in the same conversation stream. Background
        # tasks create new conversations and scatter results.
        _blocked_tools.add("background_task")
        # Inject compressed context summary from previous rounds.
        messages_for_model = _deathmatch_mgr.build_context_messages(messages_for_model)
        # PEVR: Inject plan directive when goal loop is active so the
        # agent knows the plan from the very first turn — not just
        # from continuation prompts on turn 2+.
        if _deathmatch_mgr.is_goal_active:
            plan_directive = _deathmatch_mgr.build_plan_directive()
            _replace_deathmatch_plan_directive(messages_for_model, plan_directive)

    identity_context = await _load_identity_memory_context(current_user.id)

    async def _permission_request_callback(
        conversation_id: str,
        tool_name: str,
        description: str,
        details: dict,
    ) -> bool:
        import uuid as _uuid
        from app.services.permission_manager import request_permission
        # UUID (not timestamp) so parallel blocked calls cannot collide and
        # resolve each other's prompt.
        request_id = f"perm_{_uuid.uuid4().hex}"
        perm_data = {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "user_id": str((details or {}).get("user_id") or ""),
            "tool_name": tool_name,
            "description": description,
            "details": details,
        }
        perm_event = {"permission_request": perm_data}

        state = await _agent_registry.get(conversation_id)
        if state:
            try:
                await state.broadcast(perm_event)
            except Exception:
                logger.exception("Failed to broadcast permission request")

        # Permission prompts are live-only: do NOT append them to the replay
        # buffer.  A reconnect inside the 120 s window would otherwise show an
        # actionable prompt whose payload is stale/redacted; timing out (deny)
        # is the safe behavior.  The live broadcast above has the full review
        # payload.

        return await request_permission(
            conversation_id=conversation_id,
            tool_name=tool_name,
            description=description,
            details=details,
            timeout=120.0,
            request_id=request_id,
        )

    # 遵循词 canary: the marker is injected into the system prompt
    # below (_build_system_prompt) AND handed to the loop so only this
    # path ever checks for it (worker/scheduler/delegate never do).
    from app.services.canary_marker import make_canary, strip_canary_streaming
    _canary_marker = make_canary(str(conversation_id)) if _config.agent_canary_enabled else None

    _thinking_sampling, _non_thinking_sampling, _preserve_thinking = _qwen_sampling_for(
        assistant, _wire_provider_type,
    )

    # 2026-11-01 per-user MCP: revalidate this user's servers in the current
    # worker before schema building. A no-op diff after the first run; a newly
    # added server is registered here (other workers converge at their next run).
    try:
        from app.services.user_mcp_service import ensure_user_mcp_tools as _ensure_user_mcp
        async with AsyncSessionLocal() as _mcp_db:
            await _ensure_user_mcp(_mcp_db, str(_user_id_local))
    except Exception:
        logger.warning(
            "User MCP apply failed for user %s (run continues with current tools)",
            _user_id_local, exc_info=True,
        )

    agent_loop = AgentLoop(
        llm=llm_service,
        tool_schemas=None,
        max_iterations=_config.agent_tool_loop_max_iterations,
        workspace_path="",  # gather 后回填（workspace 未就绪；run 前必被覆盖）
        provider_type=_wire_provider_type,
        enable_reasoning=request.enable_reasoning,
        reasoning_effort=request.reasoning_effort,
        thinking_budget=request.thinking_budget,
        thinking_sampling=_thinking_sampling or None,
        non_thinking_sampling=_non_thinking_sampling or None,
        preserve_thinking=_preserve_thinking,
        iteration_llm=iteration_llm,
        iteration_provider_type=iteration_provider_type,
        blocked_tools=_blocked_tools if _blocked_tools else None,
        permission_callback=_permission_request_callback,
        act_gate_approval_callback=_make_act_gate_approval_callback(
            _permission_request_callback
        ),
        session_factory=AsyncSessionLocal,
        identity_context=identity_context,
        coordinator_llm=coordinator_llm,
        # Must be passed here (not assigned post-init): the deathmatch
        # iteration-budget override is evaluated inside __init__.
        deathmatch_manager=_deathmatch_mgr,
        enable_compression=_config.agent_compression_enabled,
        canary_marker=_canary_marker,
        user_id=str(_user_id_local) if _user_id_local else None,
        # 二期工具集按需发送：浏览器交互子工具（navigate/click/type 等）
        # 首轮不发送，模型调用 browser 入口后由 AgentLoop 自动追加
        # （只增不减，会话内最多破坏一次前缀缓存；省 ~1.5k tokens/轮）。
        lazy_tools=[
            n for n in registry.get_tool_names_for_toolset("web")
            if n not in ("browser", "pdf_export", "web_search", "citation_render")
        ],
    )

    # 渐进落库（2026-10-06，conv ae9aa092）：审计打回时被拒草稿全文+
    # 裁决元数据写入 message_attempts 台账（用户裁定：保留并标记）。
    # 台账挂到本轮 streaming 在途行下；无在途行（死磕等路径）则跳过。
    async def _on_draft_rejected(*, content, verdict, problem, source, attempt_no):
        try:
            async with AsyncSessionLocal() as _att_db:
                _att_row = (await _att_db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id,
                           Message.delivery_status == "streaming")
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )).scalars().first()
                if _att_row is None:
                    return
                _att_db.add(MessageAttempt(
                    message_id=_att_row.id,
                    conversation_id=conversation_id,
                    attempt_no=attempt_no,
                    status="rejected",
                    content=content or "",
                    verdict_json=json.dumps({
                        "verdict": verdict, "problem": problem, "source": source,
                    }, ensure_ascii=False),
                ))
                await _att_db.commit()
        except Exception:
            logger.warning("message_attempts write failed (fail-open)", exc_info=True)

    agent_loop.on_draft_rejected = _on_draft_rejected

    # §5.2 工程要求 1：coordinator 与召回管线/工作区准备/skills 组装真并发
    # （合并等待取 max 非 sum）。coordinator 只读 user/assistant 消息，
    # 不依赖 system prompt，可提前启动；失败回退 _ALO_UNSET 由 run 内串行重试。
    from app.services.agent_loop import _UNSET as _ALO_UNSET

    async def _coord_prepass():
        try:
            logger.warning("[SETUP-TRACE] coord enter")
            _r = await agent_loop._coordinate(list(messages_for_model))
            logger.warning("[SETUP-TRACE] coord exit %s", type(_r).__name__)
            return _r
        except Exception:
            logger.warning("[SETUP-TRACE] coord exit exc")
            logger.exception("coordinator pre-pass failed; in-loop retry")
            return _ALO_UNSET

    # Setup heartbeat: this gather includes the coordinator LLM
    # pre-pass and can legitimately exceed 30-60s. Before the buffer /
    # registry / agent keepalive exist, total silence would trip the
    # frontend's 30s stall watchdog and kill a healthy setup (and with
    # it the whole run — the detached task does not exist yet). Emit
    # raw SSE pings until the gather settles.
    from app.services.sse_heartbeat import HEARTBEAT as _HB, aiter_with_heartbeat as _aiter_hb

    async def _workspace_ctx():
        """Request-independent workspace fetch.

        A client disconnect during setup (tab switch / refresh /
        network blip) must NOT kill the run — the SSE abort path
        re-drives the tool loop in a detached task AFTER the request
        session is closed, so setup can no longer depend on the
        request-scoped ``db`` (conv b078987b, 2026-08-03: 20s-in
        abort → dead-setup release → user message left unanswered).
        """
        async with _AsyncSessionLocal() as _ws_db:
            logger.warning("[SETUP-TRACE] workspace enter")
            _ws = await ensure_user_workspace(
                _ws_db, current_user.id, current_user.username,
            )
            logger.warning("[SETUP-TRACE] workspace exit")
            return _ws

    _setup_result = None

    async def _skills_ctx():
        logger.warning("[SETUP-TRACE] skills enter")
        _s = await build_skills_system_prompt(current_user, include_force_inject=False)
        logger.warning("[SETUP-TRACE] skills exit")
        return _s

    async for _hb_item in _aiter_hb(asyncio.gather(
        _memory_ctx(),
        _workspace_ctx(),
        _skills_ctx(),
        _coord_prepass(),
    ), interval=10.0):
        if _hb_item is _HB:
            # Keep the provisional reservation alive: a live setup
            # (SSE pings flowing) must never be reaped by the 180s
            # provisional TTL — expiry would let a newer request
            # re-reserve the slot and fail THIS run with
            # conversation_superseded (user-visible "已被新的请求取代"
            # error on a healthy stream). Identity-checked: only
            # refresh while the slot is still ours.
            try:
                if (
                    _reserved_agent_state is not None
                    and _agent_registry.get_local(conversation_id) is _reserved_agent_state
                    and _reserved_agent_state.provisional
                ):
                    _reserved_agent_state.reserved_at = time.time()
            except Exception:
                pass
            yield {"event": "ping", "data": json.dumps({"ping": True})}
        else:
            _setup_result = _hb_item
    memory_context, workspace, skills_sys_prompt, coord_result = _setup_result
    logger.warning("[SETUP-TRACE] gather done")
    agent_loop.workspace_path = str(workspace.root_path)

    # W17 vibeweaver 轮次门控：force-inject 全文与 gate/audit 执法仅对
    # coding 任务或显式提及 vibeweaver 的轮次激活（生产 conv dd3b2408：
    # 非代码任务被无条件注入拖入工程仪式直至空回答）。coordinator 缺位
    # （死亡模式/显式技能调用/解析失败）→ coding_task None → 关键词兜底。
    from app.services.vibeweaver_turn_gate import (
        explicit_trigger_hit as _vw_explicit,
        resolve_effective_active as _vw_resolve_eff,
    )
    # 门控文本/ctx 已于 messages_for_model 定稿后、deathmatch 合成注入前
    # 抽取（见上方 IMPORTANT-1 注释）。coordinator 缺位（死亡模式/技能调用）
    # 时启发式兜底吃近 3 条 ctx——「继续」类跟帖不丢工程上下文；
    # explicit 仍只看末条（防历史提及对无关新话题过度激活）。
    _vw_coding = coord_result.get("coding_task") if isinstance(coord_result, dict) else None
    _vw_explicit_hit = _vw_explicit(_vw_text)
    try:
        _vw_gating = get_config().vibeweaver_task_gating_enabled
        # D9/H15：mode=off = 不注入 + 不装钩子（单一 owner；kill-switch 全关，
        # 含 force-inject 短路——round-6 三接缝之一）。
        _vw_mode = get_config().vibeweaver_mode
    except Exception:
        _vw_gating, _vw_mode = True, "auto"
    if _vw_mode == "off":
        _vw_active = False
    else:
        _vw_active = _vw_resolve_eff(_vw_text, _vw_coding, _vw_gating, context_text=_vw_ctx)
    agent_loop.vibeweaver_active = _vw_active
    conversation.vibeweaver_active = _vw_active
    if _deathmatch_mgr is not None:
        _deathmatch_mgr.vibeweaver_active = _vw_active
    if _vw_active:
        _vw_block = await build_force_injected_skills_prompt(current_user)
        if _vw_block:
            skills_sys_prompt = (
                (skills_sys_prompt + "\n\n" + _vw_block) if skills_sys_prompt else _vw_block
            )
    logger.info(
        "vibeweaver turn gate active=%s explicit=%s coding=%s gating=%s mode=%s conv=%s",
        _vw_active, _vw_explicit_hit, _vw_coding, _vw_gating, _vw_mode, conversation_id,
    )

    # D4 compactResume：压缩发生后的**下一轮** system 装配一次性注入恢复包
    # （会话级 pending 标记；消费即清，绝不重复注入）。fail-open。
    try:
        if _vw_mode != "off" and _vw_active:
            from app.services import vibeweaver_delivery_service as _vw_delivery
            from app.services.vibeweaver_gate_service import (
                find_project_root as _vw_find_root,
                resolve_session_root as _vw_resolve_root,
            )
            _vw_ws = str(workspace.root_path)
            _vw_root = _vw_resolve_root(_vw_ws, str(conversation_id))
            if _vw_root is None:
                _vw_root = _vw_find_root([_vw_ws], bound=_vw_ws)
            if _vw_root is not None:
                _vw_resume = _vw_delivery.consume_pending_resume(_vw_root, str(conversation_id))
                if _vw_resume:
                    skills_sys_prompt = (
                        (skills_sys_prompt + "\n\n" + _vw_resume) if skills_sys_prompt else _vw_resume
                    )
                    logger.info("vibeweaver compact-resume injected (conv=%s)", conversation_id)
    except Exception:
        logger.debug("vibeweaver compact-resume injection failed-open", exc_info=True)

    from types import SimpleNamespace
    if memory_context is not None and memory_context.strip():
        shared_context = SimpleNamespace(
            agent_state=None,
            memory_summary=memory_context or '',
            dream_summary='',
            memory_entries=[],
        )
    else:
        from app.services.memory_runtime_state import memory_runtime_enabled as _mem_rt2
        if _mem_rt2(_config):
            # v2 运行时（2026-09-13 v1 退休，方案 A）：检索为空即无记忆
            # 注入——不再回落 v1 摘要（陈旧数据；v1 生成在 v2 下已停摆）。
            shared_context = SimpleNamespace(
                agent_state=None, memory_summary='', dream_summary='', memory_entries=[])
        else:
            async with _AsyncSessionLocal() as _sctx_db:
                shared_context = await build_shared_agent_context(_sctx_db, current_user.id)

    # 记忆优先级最高（2026-09-18 conv 3583d840，A4.9 I2 修复）：把本轮
    # 注入写手上下文的**全部**记忆源（v2 检索摘要 / v1 dream / 条目层 /
    # 身份记忆）带给审计证据域（[M] 权威来源），避免审计把记忆事实判成
    # 「凭空编造」并指示写手拒答；v1 回落路径（[memory] 关闭）同样覆盖。
    _mem_parts = [
        str(getattr(shared_context, "memory_summary", "") or ""),
        str(getattr(shared_context, "dream_summary", "") or ""),
        "\n".join(
            str(getattr(_e, "content", "") or "")
            for _e in (getattr(shared_context, "memory_entries", None) or [])
        ),
        str(identity_context or ""),
    ]
    agent_loop.injected_memory_context = "\n\n".join(
        _p for _p in _mem_parts if _p.strip())

    sys_prompt = await agent_service._build_system_prompt(
        assistant=assistant,
        shared_context=shared_context,
        workspace=workspace,
        user=current_user,
        user_skill_content=skill_content,
        skills_system_prompt=skills_sys_prompt,
        skill_files=skill_files,
        identity_context=identity_context,
        conversation_id=str(conversation_id),
        deathmatch_mode=bool(conversation.deathmatch_mode),
        offered_tool_names=[
            (s.get("function") or {}).get("name", "")
            for s in (agent_loop.tool_schemas or [])
            if (s.get("function") or {}).get("name")
        ],
        task=("coding" if isinstance(coord_result, dict)
              and coord_result.get("coding_task") is True else ""),
    )
    messages_for_model[0] = {
        "role": "system",
        "content": sys_prompt,
        "prompt_id": "agent.system",
    }

    # 上下文 token 用量：估算「系统提示词 + 历史 + 工具 schema」的完整
    # 请求量，随流发给前端显示（头部徽章）。压缩事件携带 before/after
    # 对比并在压缩后刷新本值。CJK 感知估算（与压缩决策同源）。
    from app.services.agent_loop import _resolve_context_length
    _ctx_window = _resolve_context_length(getattr(agent_loop, 'llm', None))
    _ctx_tokens = 0
    try:
        from app.services.context_compressor import estimate_request_tokens_rough
        _ctx_tokens = estimate_request_tokens_rough(
            messages_for_model, tools=agent_loop.tool_schemas
        )
    except Exception:
        logger.exception("Failed to estimate context tokens")

    _agent_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _agent_done = object()
    pass
    _state.sse_cancelled_flag = asyncio.Event()
    _sse_cancelled = _state.sse_cancelled_flag

    # 4.8 session lock handoff: adopt the slot reserved in the
    # endpoint (identity-checked). If the registry no longer holds
    # OUR state object, a newer request re-reserved the slot — this
    # run is stale and must abort instead of spawning a second loop.
    # Refresh first: a slow-but-alive setup must not abort itself.
    if _reserved_agent_state is not None:
        if _reserved_agent_state.provisional:
            _reserved_agent_state.reserved_at = time.time()
        _agent_state = await _agent_registry.adopt_reservation(
            conversation_id, _reserved_agent_state
        )
        if _agent_state is None:
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "该会话的回答已被新的请求取代，已取消重复请求。",
                    "code": "conversation_superseded",
                    # A newer request legitimately owns the conversation —
                    # the frontend must end this stale stream SILENTLY
                    # (no error bubble) and resync from the DB instead
                    # of alarming the user.
                    "silent": True,
                }, ensure_ascii=False),
            }
            return
    else:
        _agent_state = await _agent_registry.register(conversation_id)
    # Cancel any previous detached agent task for this conversation to
    # prevent stale tasks from emitting permission requests alongside the
    # new one.
    old_task = _DETACHED_AGENT_TASKS.pop(conversation_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
        logger.info("Cancelled stale detached agent task for conversation %s", conversation_id)
    _agent_queue = await _agent_state.subscribe()

    _TOOL_STEP_TITLES: dict[str, str] = {
        "web_search": "联网检索结果",
        "browser": "浏览网页结果",
        "execute_code": "代码执行结果",
        "memory": "记忆操作",
        "delegate_task": "子任务执行结果",
        "context7_resolve_library_id": "查找库文档ID",
        "context7_query_docs": "查询库文档",
    }

    _cap_conversation_id = conversation_id
    _cap_assistant = assistant
    _cap_search_query = search_query
    _cap_latest_user_query = latest_user_query
    _cap_user_id = current_user.id
    _cap_username = current_user.username
    _cap_workspace_path = str(workspace.root_path)
    _cap_llm_service = llm_service

    async def _generate_title_bg(bg_db, conv_row, full_content: str) -> str | None:
        """Run TitleGeneratorService for background save paths so that
        conversations whose original SSE was interrupted still get a
        proper LLM-generated title (instead of just slicing the query)."""
        if not conv_row or conv_row.title != "新对话":
            return conv_row.title if conv_row else None
        _tq = _cap_search_query or _cap_latest_user_query or ""
        generated_title: str | None = None
        try:
            tg = TitleGeneratorService(
                **agent_service.title_generator_kwargs(_cap_assistant, _cap_llm_service),
                provider_type=wire_provider_type(_cap_llm_service, getattr(_cap_assistant, "provider_type", "deepseek") or "deepseek"),
            )
            try:
                generated_title = await tg.generate_title(
                    user_query=_tq,
                    assistant_response=full_content or "",
                )
            except Exception:
                logger.exception("Background title LLM call failed; using fallback")
                generated_title = None
            if not generated_title:
                generated_title = await tg.get_fallback_title(_tq)
        except Exception:
            logger.exception("Background TitleGeneratorService init failed")
        if not generated_title or generated_title == "新对话":
            src = (_tq or full_content or "").strip()
            generated_title = src[:16] if src else "未命名会话"
        conv_row.title = generated_title
        await bg_db.commit()
        return generated_title

    # Deathmatch terminal-status flag shared with the nested
    # _agent_loop_task (which sets it) and this function's finalize
    # path (which reads it). Must live in THIS scope — defining it
    # only inside _agent_loop_task caused a NameError at the final
    # save whenever the loop ended with empty content (conv 51d74833).
    _state._dm_terminal_status_saved = False

    # Stream buffer for this conversation, shared between the agent
    # task (creator) and the relay loop (post-disconnect save). Must
    # live in _run_tool_loop scope, NOT inside _agent_loop_task: the
    # relay's _bg_save / interrupted-save paths reference it after
    # the agent task completes (conv b078987b recovery hit a
    # NameError here when the post-disconnect relay path ran in the
    # detached re-run).
    _state._stream_buf = None

    async def _agent_loop_impl():
        # C-02c: the free-variable bridge is built AT THE POINT OF USE (inside
        # the spawned task), never at def time — a later rebind in this scope
        # can then never hand the hoisted impl a stale value.
        await _chat_agent_loop_impl(_state, **{
            '_agent_queue': _agent_queue,
            '_agent_state': _agent_state,
            '_cap_assistant': _cap_assistant,
            '_cap_conversation_id': _cap_conversation_id,
            '_cap_latest_user_query': _cap_latest_user_query,
            '_cap_user_id': _cap_user_id,
            '_ctx_tokens': _ctx_tokens,
            '_ctx_window': _ctx_window,
            '_deathmatch_mgr': _deathmatch_mgr,
            '_generate_title_bg': _generate_title_bg,
            '_sse_cancelled': _sse_cancelled,
            '_user_id_local': _user_id_local,
            '_username_local': _username_local,
            'agent_loop': agent_loop,
            'conversation_id': conversation_id,
            'conversation_messages': conversation_messages,
            'coord_result': coord_result,
            'messages_for_model': messages_for_model,
            'strip_canary_streaming': strip_canary_streaming,
        })

    async def _agent_loop_task():
        await _run_agent_loop_task(
            _agent_loop_impl, _cap_conversation_id, _agent_state, _agent_registry
        )

    _agent_task = asyncio.create_task(_agent_loop_task(), name=f"agent-loop-{conversation_id[:8]}")
    pass
    _state.detached_agent_task = _agent_task
    # 兜底①绑定（conv 5228db60）：done-callback 据此 identity 释放孤儿槽。
    try:
        _agent_state.task_handle = _agent_task
    except Exception:
        pass
    _track_detached_task(_agent_task, _cap_conversation_id)

    try:
        while True:
            try:
                # 10s server-side keepalive — must stay well under the
                # frontend's 30s stall watchdog. Agent phases that emit
                # no events on their own (e.g. the tool-digest batch,
                # up to 300s) must never look like a dead connection.
                event = await asyncio.wait_for(_agent_queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"ping": True})}
                if _state.client_disconnected:
                    continue
                # In deathmatch mode, be more tolerant of slow agent
                # iterations — the agent is detached and should keep
                # running in the background regardless of the SSE
                # viewer connection.
                _dm_active = bool(_deathmatch_mgr and _deathmatch_mgr.is_active)
                if _dm_active:
                    continue
                try:
                    if await req.is_disconnected():
                        _state.client_disconnected = True
                        logger.warning("Client disconnected during tool loop, agent continues in background")
                except Exception:
                    pass
                continue

            if isinstance(event, object) and not isinstance(event, dict):
                break

            if "_completed" in event:
                completed = event["_completed"]
                # conv 3b58af5b (R4): the producer already sanitized,
                # localized and persisted the answer BEFORE broadcasting.
                # Single-writer: skip every consumer-side save path and
                # go straight to the done emission — whether or not this
                # client is still connected.
                if completed.get("message_id"):
                    _state.full_response = _build_content_fallback(
                        completed["joined_content"], completed.get("tool_results_json")
                    )
                    _state.full_reasoning = completed["full_reasoning"]
                    _state.tool_results_json = completed.get("tool_results_json")
                    tool_calls_json = completed.get("tool_calls_json")
                    assistant_message_id = completed["message_id"]
                    _state.response_saved = True
                    # A4.9 fix (Important-2): keep chat_sessions
                    # bookkeeping on the now-default producer-persist
                    # path (legacy save path maintained this counter).
                    try:
                        _state.message_count += 1
                        if chat_session is not None:
                            chat_session.message_count = _state.message_count
                            await db.commit()
                    except Exception:
                        logger.exception(
                            "chat_session message_count update failed after producer persist"
                        )
                    logger.info(
                        "Final answer already persisted by producer (msg %s) — skipping consumer save",
                        assistant_message_id,
                    )
                    try:
                        async for done_event in _finalize_and_emit_done(
                            assistant_message_id=assistant_message_id,
                            final_content=_state.full_response,
                            final_tool_results_json=_state.tool_results_json,
                            fallback=False,
                        ):
                            yield done_event
                    except Exception as done_exc:
                        logger.exception("Failed to emit done event: %s", done_exc)
                        yield {
                            "event": "done",
                            "data": json.dumps({
                                "conversation_id": conversation_id,
                                "message_id": assistant_message_id,
                                "title": conversation.title if conversation else None,
                                "tool_results": None,
                                "done": True,
                                "fallback": False,
                            }),
                        }
                    return
                _cap_content = _build_content_fallback(completed["joined_content"], completed.get("tool_results_json"))
                # Citation ledger verify step: strip [N] markers that
                # reference no real fetched source before persistence
                # (grounded-citations port — frontend cannot catch
                # out-of-range [N] when any search ran). Sanitizes the
                # content AND the tool_results content_segments /
                # display_sequence text surfaces in one judgment.
                _cap_content, _cap_tool_results = await _sanitize_cited_content(
                    _cap_content, completed.get("tool_results_json"),
                )
                _cap_content, _cap_tool_results = await _render_answer_citations(
                    _cap_content, _cap_tool_results,
                    getattr(_cap_assistant, "citation_style", None),
                )
                _cap_reasoning = completed["full_reasoning"]
                _cap_tool_calls = completed.get("tool_calls_json")

                # Localize remote media the agent decided to display
                # (content-addressed into the user's workspace).
                _cap_content, _cap_tool_results = await _localize_media_for_persist(
                    _cap_content, _cap_tool_results, _user_id_local, _username_local,
                )

                _state.full_response = _cap_content
                _state.full_reasoning = _cap_reasoning
                _state.tool_results_json = _cap_tool_results
                tool_calls_json = _cap_tool_calls

                if _state.client_disconnected:
                    logger.info("Agent loop completed after disconnect, saving results via background task")
                    _bg_buf = _state._stream_buf
                    async def _bg_save():
                        # Guard: never persist a completely empty bubble
                        # (mirrors the self-save guard above).
                        if not (_cap_content or "").strip() and not _cap_tool_results and not _cap_tool_calls:
                            logger.info("Post-disconnect save skipped: empty payload")
                            # 空终态：清掉渐进落库的空 streaming 行
                            # （A4.9 r1 M2：行 id 精确删除，不跨 run 误删）
                            # 2026-11-02（C5）：同源判据 + 只按本 run 行 id 精确删
                            # （旧实现按会话批量删 content 为空的 streaming 行，
                            # 会误删并发 run 的证据行）。
                            try:
                                from app.api.chat_persistence import _delete_empty_streaming_row as _del_empty
                                _clean_row_id = getattr(_bg_buf, "streaming_message_id", None)
                                if _clean_row_id:
                                    async with AsyncSessionLocal() as _clean_db:
                                        await _del_empty(_clean_db, _clean_row_id)
                                        await _clean_db.commit()
                                else:
                                    logger.debug(
                                        "post-disconnect empty: no own streaming row id; "
                                        "leaving cleanup to boot sweep"
                                    )
                            except Exception:
                                logger.debug("empty streaming row cleanup failed", exc_info=True)
                            try:
                                await _bg_buf.mark_complete(None)
                            except Exception:
                                pass
                            return
                        for attempt in range(3):
                            try:
                                async with AsyncSessionLocal() as bg_db:
                                    conv_row = (await bg_db.execute(
                                        select(Conversation).where(Conversation.id == _cap_conversation_id)
                                    )).scalar_one_or_none()
                                    if conv_row is None:
                                        return
                                    # 渐进落库：streaming 在途行原地更新翻 final
                                    msg = await _save_assistant_terminal(
                                        bg_db,
                                        conversation_id=_cap_conversation_id,
                                        content=_cap_content,
                                        reasoning=_cap_reasoning if _cap_reasoning else None,
                                        tool_results=_cap_tool_results,
                                        tool_calls=_cap_tool_calls,
                                        context_info=None,
                                        status="final",
                                        row_id=getattr(
                                            _bg_buf, "streaming_message_id", None),
                                        interjection_anchor=interjection_anchor_from_buffer(
                                            _bg_buf),
                                        # Reconstruct from the buffer for the
                                        # new-row fallback (no run object here).
                                        interjection_items=interjection_items_from_buffer(_bg_buf),
                                    )
                                    conv_row.updated_at = datetime.utcnow()
                                    await bg_db.commit()
                                    await bg_db.refresh(msg)
                                    try:
                                        await _bg_buf.mark_complete(str(msg.id))
                                    except Exception:
                                        pass
                                    try:
                                        _final_title = await _generate_title_bg(bg_db, conv_row, _cap_content)
                                        if _final_title:
                                            try:
                                                if _bg_buf.done_data is None:
                                                    _bg_buf.done_data = {}
                                                _bg_buf.done_data["title"] = _final_title
                                            except Exception:
                                                pass
                                    except Exception:
                                        logger.exception("Background title generation failed (post-disconnect path)")
                                    logger.info("Background save succeeded on attempt %d (post-disconnect)", attempt + 1)
                                    return
                            except Exception:
                                import traceback as _tb
                                _tb.print_exc()
                                await asyncio.sleep(2.0)
                        logger.error("Background save failed after 3 attempts")
                    try:
                        _bg = asyncio.get_running_loop().create_task(_bg_save())
                        _track_bg_task(_bg, _cap_conversation_id, "post_disconnect_save")
                    except Exception:
                        pass
                    _state.response_saved = True
                    return

                # Resolve the final assistant message id. The variable
                # must be BOUND (None) BEFORE the save attempt: a save
                # failure (e.g. FK violation when the conversation was
                # deleted mid-stream) falls through to
                # _finalize_and_emit_done below, and an unbound
                # reference would crash the whole SSE generator
                # without a done/error event — the client loses the
                # stream (and with the old broadcast-before-append
                # ordering, part of the displayed answer with it)
                # while the backend keeps the answer in the buffer.
                assistant_message_id: str | None = None
                try:
                    sanitized_content = _cap_content if _cap_content else _build_content_fallback("", _cap_tool_results)
                    # When the deathmatch terminal status message
                    # (partial_complete/human_gate/paused) was already
                    # persisted with text + deliverables, an empty
                    # fallback bubble would only duplicate its
                    # attachments — skip it (conv aadb26a3 ending).
                    _skip_empty_final = (
                        not sanitized_content
                        and _state._dm_terminal_status_saved
                    )
                    if sanitized_content or (_cap_tool_results and not _skip_empty_final):
                        # 渐进落库（2026-10-06）：streaming 在途行原地更新
                        # 翻 final，不插入重复行。
                        assistant_message = await _save_assistant_terminal(
                            db,
                            conversation_id=conversation_id,
                            content=sanitized_content,
                            reasoning=_cap_reasoning or None,
                            tool_results=_cap_tool_results,
                            tool_calls=_cap_tool_calls,
                            context_info=None,
                            status="final",
                            row_id=getattr(
                                getattr(_state, "_stream_buf", None),
                                "streaming_message_id", None),
                            interjection_anchor=interjection_anchor_from_buffer(
                                getattr(_state, "_stream_buf", None)),
                            interjection_items=interjection_items_from_buffer(
                                getattr(_state, "_stream_buf", None)),
                        )
                        conversation.updated_at = datetime.utcnow()
                        await db.commit()
                        await db.refresh(assistant_message)
                        _state.response_saved = True
                        _state.message_count += 1
                        assistant_message_id = assistant_message.id

                        chat_session.message_count = _state.message_count
                        await db.commit()

                        try:
                            await _state._stream_buf.mark_complete(str(assistant_message_id))
                        except Exception:
                            pass
                    else:
                        # 空终态不落库——但渐进落库的 streaming 空行
                        # 必须清掉，否则会残留无主的在途行。
                        # 2026-11-02（C5）：改用与启动 sweep 同源的判据
                        # （`_delete_empty_streaming_row`），且**只在有本 run
                        # 行 id 时**按 id 精确删除——旧的「按会话批量删所有
                        # content 为空的 streaming 行」会误删并发 run 的证据行。
                        try:
                            from app.api.chat_persistence import _delete_empty_streaming_row as _del_empty
                            _clean_row_id = getattr(_state._stream_buf, "streaming_message_id", None)
                            if _clean_row_id:
                                await _del_empty(db, _clean_row_id)
                                await db.commit()
                            else:
                                logger.debug(
                                    "empty terminal: no own streaming row id; "
                                    "leaving cleanup to boot sweep"
                                )
                        except Exception:
                            logger.debug("empty streaming row cleanup failed", exc_info=True)
                        try:
                            await _state._stream_buf.mark_complete(None)
                        except Exception:
                            pass
                        async for done_event in _finalize_and_emit_done(
                            assistant_message_id=None,
                            final_content="",
                            final_tool_results_json=None,
                            fallback=True,
                        ):
                            yield done_event
                        return
                except Exception as save_exc:
                    logger.exception("Failed to save tool-loop assistant message: %s", save_exc)
                    _spawn_interrupted_save(_state._stream_buf)

                try:
                    async for done_event in _finalize_and_emit_done(
                        assistant_message_id=assistant_message_id,
                        final_content=_state.full_response,
                        final_tool_results_json=_state.tool_results_json,
                        fallback=assistant_message_id is None,
                    ):
                        yield done_event
                except Exception as done_exc:
                    logger.exception("Failed to emit done event: %s", done_exc)
                    yield {
                        "event": "done",
                        "data": json.dumps({
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "title": conversation.title if conversation else None,
                            "tool_results": None,
                            "done": True,
                            "fallback": assistant_message_id is None,
                        }),
                    }
                return

            if "_failed" in event:
                if not _state.client_disconnected:
                    yield {"event": "error", "data": json.dumps({"error": f"Agent管线故障: {event['_failed']}"})}
                return

            if _state.client_disconnected:
                continue

            if "part_started" in event:
                # F1-1: push new timeline part (v2 clients). Payload is
                # wrapped in the type key — the frontend parser sniffs
                # data payload keys and ignores the `event:` line.
                yield {"event": "part_started", "data": json.dumps({"part_started": event["part_started"]}, ensure_ascii=False)}

            elif "part_delta" in event:
                # F1-1: incremental append to an existing part (v2 clients)
                yield {"event": "part_delta", "data": json.dumps({"part_delta": event["part_delta"]}, ensure_ascii=False)}

            elif "part_updated" in event:
                # F1-1: structured state transition for an existing part
                yield {"event": "part_updated", "data": json.dumps({"part_updated": event["part_updated"]}, ensure_ascii=False)}

            elif "reasoning_content" in event:
                # PHASE 5: forward the phase marker ("final" for
                # _final_thinking / grace-call output) so the
                # frontend can route iteration vs final reasoning.
                _payload = {"reasoning_content": scrub_reasoning_meta(
                    event["reasoning_content"])}
                if event.get("phase"):
                    _payload["phase"] = event["phase"]
                yield {"event": "reasoning_content", "data": json.dumps(_payload)}

            elif "content" in event:
                yield {"event": "content", "data": json.dumps({"content": event["content"]})}

            elif "content_segment" in event:
                yield {"event": "content_segment", "data": json.dumps({"segment_content": event["content_segment"]})}

            elif "tool_call" in event:
                tc = event["tool_call"]
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "agent_step": {
                            "name": tc.get("name", "unknown"),
                            "title": _TOOL_STEP_TITLES.get(tc.get("name", ""), f"工具调用: {tc.get('name', '')}"),
                            "content": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                            "step_type": "tool_call",
                        },
                        "done": False,
                    }),
                }
                yield {"event": "tool_call", "data": json.dumps(tc)}
                _state.message_count += 1

            elif "tool_result" in event:
                tr = event["tool_result"]
                yield {"event": "tool_result", "data": json.dumps(tr)}

            elif "attachments" in event:
                # Accumulated download-card set (conv fbf5779b: the
                # per-call subsets previously emitted here replaced
                # the whole set on the client, so the live count was
                # wrong — the tool loop now streams the accumulated
                # set instead).
                yield {
                    "event": "message",
                    "data": json.dumps({"attachments": event["attachments"], "done": False}),
                }

            elif "iteration" in event:
                yield {"event": "iteration", "data": json.dumps(event["iteration"])}

            elif "ping" in event:
                yield {"event": "ping", "data": json.dumps({"ping": True})}

            elif "audit_reset" in event:
                # Silent-QC reset sync (conv 827a6f78 turn-B): not a
                # rendered item — the client uses it to drop the
                # rejected draft text from its timeline/accumulators.
                yield {"event": "audit_reset", "data": json.dumps({"audit_reset": True})}

            elif "agent_step" in event:
                yield {"event": "message", "data": json.dumps({"agent_step": event["agent_step"], "done": False})}

            elif "context_info" in event:
                yield {"event": "message", "data": json.dumps({"context_info": event["context_info"], "done": False})}

            elif "interjection_committed" in event:
                # 插话 commit-echo：loop 迭代边界注入用户插话后由
                # producer 落库并转发至此 —— 前端据此确认乐观气泡。
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "interjection_committed": event["interjection_committed"],
                        "done": False,
                    }),
                }

            elif "error" in event:
                yield {"event": "error", "data": json.dumps({"error": event["error"]})}

            elif "deathmatch_verdict" in event:
                _dm = event["deathmatch_verdict"]
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "deathmatch_verdict": _dm,
                        "done": False,
                    }),
                }

            elif "permission_request" in event:
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "permission_request": event["permission_request"],
                        "done": False,
                    }),
                }

            try:
                if not _state.client_disconnected and await req.is_disconnected():
                    _state.client_disconnected = True
                    try:
                        await _state._stream_buf.set_sse_active(False)
                    except Exception:
                        pass
                    logger.warning("Client disconnected during tool loop, agent continues in background")
            except Exception:
                pass

    except asyncio.CancelledError:
        if not _agent_task.done():
            _sse_cancelled.set()
            try:
                await _state._stream_buf.set_sse_active(False)
            except Exception:
                pass
            logger.warning("SSE cancelled but agent loop still running — it will complete and self-save results")
        raise


async def _chat_event_generator(_state, **kw):
    _sync_chat_globals()
    _reserved_agent_state = kw.get('_reserved_agent_state')
    conversation_id = kw.get('conversation_id')
    db = kw.get('db')
    current_user = kw.get('current_user')
    conversation = kw.get('conversation')
    skill_files = kw.get('skill_files')
    latest_user_query = kw.get('latest_user_query')
    assistant = kw.get('assistant')
    wire_provider_type = kw.get('wire_provider_type')
    coordinator_llm = kw.get('coordinator_llm')
    conversation_messages = kw.get('conversation_messages')
    _username_local = kw.get('_username_local')
    search_query = kw.get('search_query')
    _wire_provider_type = kw.get('_wire_provider_type')
    req = kw.get('req')
    llm_service = kw.get('llm_service')
    skill_content = kw.get('skill_content')
    chat_session = kw.get('chat_session')
    request = kw.get('request')
    _user_id_local = kw.get('_user_id_local')
    _finalize_and_emit_done = kw.get('_finalize_and_emit_done')
    pass
    _state.full_response = ""
    _state.full_reasoning = ""
    _state.tool_results_json = None
    # PHASE 2B: serialized OpenAI-style tool_calls list for persistence.
    tool_calls_json: str | None = None
    _cap_tool_calls: str | None = None
    agent_steps: list[dict] = []
    thinking_history: list[dict] = []
    sub_agent_outputs: dict[str, dict[str, str]] = {}
    task_plan_data: dict | None = None
    _state.response_saved = False
    _state.client_disconnected = False
    _state.detached_agent_task: asyncio.Task | None = None
    _state.sse_cancelled_flag: asyncio.Event | None = None

    def _spawn_interrupted_save(_buf_ref=None):
        """Schedule a detached background task that writes the partial
        assistant reply + regenerated title using a FRESH DB session.
        Used when the request's own `db` session becomes unusable (e.g.
        client disconnected mid-commit → asyncpg connection closed)."""
        interrupted_content = _state.full_response.strip() or (
            "回答在思考过程中被中断。已保存中间状态，您可以重新发送或追问以继续。"
        )
        _cap_reasoning = _state.full_reasoning
        _cap_tool_results = _state.tool_results_json
        _cap_conv_id = conversation_id
        _cap_already_saved = _state.response_saved
        _cap_search_query = search_query
        _cap_latest_user_query = latest_user_query
        _cap_assistant = assistant
        _cap_llm_service = llm_service

        async def _job():
            try:
                async with AsyncSessionLocal() as bg_db:
                    conv_row = (await bg_db.execute(
                        select(Conversation).where(Conversation.id == _cap_conv_id)
                    )).scalar_one_or_none()
                    if conv_row is None:
                        if _buf_ref:
                            try:
                                await _buf_ref.mark_complete(None)
                            except Exception:
                                pass
                        return
                    if not _cap_already_saved:
                        _loc_content, _loc_tr = await _localize_media_for_persist(
                            interrupted_content, _cap_tool_results,
                            _user_id_local, _username_local,
                        )
                        # 渐进落库：断连中断=非预期中断——streaming 在途行
                        # 原地更新并标 interrupted（前端「已中断」徽标）。
                        msg = await _save_assistant_terminal(
                            bg_db,
                            conversation_id=_cap_conv_id,
                            content=_loc_content,
                            reasoning=_cap_reasoning if _cap_reasoning else None,
                            tool_results=_loc_tr,
                            tool_calls=_cap_tool_calls,
                            context_info=None,
                            status="interrupted",
                            row_id=getattr(
                                _buf_ref, "streaming_message_id", None),
                            interjection_anchor=interjection_anchor_from_buffer(
                                _buf_ref),
                            interjection_items=interjection_items_from_buffer(_buf_ref),
                        )
                        conv_row.updated_at = datetime.utcnow()
                        await bg_db.commit()
                        await bg_db.refresh(msg)
                        if _buf_ref:
                            try:
                                await _buf_ref.mark_complete(str(msg.id))
                            except Exception:
                                pass
                    if conv_row.title == "新对话":
                        title_generator = TitleGeneratorService(
                            **agent_service.title_generator_kwargs(_cap_assistant, _cap_llm_service),
                            provider_type=wire_provider_type(_cap_llm_service, getattr(_cap_assistant, "provider_type", "deepseek") or "deepseek"),
                        )
                        _tq = _cap_search_query or _cap_latest_user_query or ""
                        gt = None
                        try:
                            gt = await title_generator.generate_title(
                                user_query=_tq,
                                assistant_response=interrupted_content,
                            )
                        except Exception:
                            import traceback as _tb
                            _tb.print_exc()
                        if not gt:
                            gt = await title_generator.get_fallback_title(_tq)
                        if not gt or gt == "新对话":
                            _src = (_tq or interrupted_content or "").strip()
                            gt = _src[:16] if _src else "未命名会话"
                        conv_row.title = gt
                        await bg_db.commit()
            except Exception:
                import traceback as _tb
                _tb.print_exc()

        try:
            loop = asyncio.get_running_loop()
            _bg = loop.create_task(_job())
            _track_bg_task(_bg, _cap_conv_id, "interrupt_save")
        except Exception:
            import traceback as _tb
            _tb.print_exc()



    # ---- Tool-calling loop path (AgentLoop is the only pipeline) ---
    _config = get_config()

    async def _release_setup_slot():
        """Free the 4.8 session-lock slot + stream buffer for a run that
        died before its detached agent task was spawned. The slot is
        claimed by reserve() at request start; if the SSE is cancelled
        (or setup throws) before task creation, nothing else ever
        unregisters it — the next /stream for this conversation is then
        rejected with conversation_busy (the stop → edit → resend → no
        answer bug). Running agents own both and clean up in their own
        CancelledError handler / finally, so this must only fire when no
        agent task was created."""
        if _reserved_agent_state is not None:
            try:
                await _agent_registry.release_dead_setup(conversation_id, _reserved_agent_state)
            except Exception:
                pass
        try:
            _dead_buf = await stream_buffer_manager.get_buffer(conversation_id, current_user.id)
            if _dead_buf is not None and _dead_buf.is_running:
                await _dead_buf.mark_complete(None)
        except Exception:
            pass

    async def _release_setup_slot_safely():
        # A second cancellation during the cleanup awaits would re-raise
        # CancelledError out of the handler and re-leak the slot; the
        # shield lets the cleanup run to completion.
        try:
            await asyncio.shield(_release_setup_slot())
        except asyncio.CancelledError:
            pass

    async def _recover_tool_loop():
        """Re-drive the tool loop after a setup-phase SSE abort.

        When the SSE is cancelled during setup, the detached agent task
        does not exist yet — the run would otherwise die permanently and
        the already-saved user message would never get an answer (conv
        b078987b, 2026-08-03: 20s-in visibilitychange abort → dead-setup
        release → no answer ever produced). Re-invoking ``_run_tool_loop``
        in a background task: setup re-runs with fresh sessions, the
        (still-ours) reservation is adopted, the agent loop runs, and on
        completion the relay's post-disconnect path self-saves with a
        fresh session. If the stop endpoint already released the slot
        (deliberate stop), ``adopt_reservation`` returns None and the
        recovery dies with conversation_superseded — correct stop
        semantics preserved.
        """
        try:
            _tool_loop_kw = {
                '_finalize_and_emit_done': _finalize_and_emit_done,
                '_reserved_agent_state': _reserved_agent_state,
                '_spawn_interrupted_save': _spawn_interrupted_save,
                '_user_id_local': _user_id_local,
                '_username_local': _username_local,
                '_wire_provider_type': _wire_provider_type,
                'assistant': assistant,
                'chat_session': chat_session,
                'conversation': conversation,
                'conversation_id': conversation_id,
                'conversation_messages': conversation_messages,
                'coordinator_llm': coordinator_llm,
                'current_user': current_user,
                'db': db,
                'latest_user_query': latest_user_query,
                'llm_service': llm_service,
                'req': req,
                'request': request,
                'search_query': search_query,
                'skill_content': skill_content,
                'skill_files': skill_files,
                'wire_provider_type': wire_provider_type,
            }
            async for _ev in _chat_run_tool_loop(_state, **_tool_loop_kw):
                pass  # drain — no SSE client; agent self-saves on completion
        except asyncio.CancelledError:
            # The recovery task itself was cancelled (e.g. app shutdown).
            # Release the adopted slot so the conversation is never
            # wedged behind a dead running-claim (the cleanup loop only
            # reaps PROVISIONAL entries; an adopted one would leak
            # conversation_busy forever). Identity-checked: never pops a
            # slot re-reserved by a newer request.
            await _release_setup_slot_safely()
        except Exception:
            logger.exception(
                "Setup-recovery tool loop failed for conversation %s",
                conversation_id,
            )
            await _release_setup_slot_safely()

    _recovery_task: asyncio.Task | None = None

    try:
        _tool_loop_kw = {
            '_finalize_and_emit_done': _finalize_and_emit_done,
            '_reserved_agent_state': _reserved_agent_state,
            '_spawn_interrupted_save': _spawn_interrupted_save,
            '_user_id_local': _user_id_local,
            '_username_local': _username_local,
            '_wire_provider_type': _wire_provider_type,
            'assistant': assistant,
            'chat_session': chat_session,
            'conversation': conversation,
            'conversation_id': conversation_id,
            'conversation_messages': conversation_messages,
            'coordinator_llm': coordinator_llm,
            'current_user': current_user,
            'db': db,
            'latest_user_query': latest_user_query,
            'llm_service': llm_service,
            'req': req,
            'request': request,
            'search_query': search_query,
            'skill_content': skill_content,
            'skill_files': skill_files,
            'wire_provider_type': wire_provider_type,
        }
        async for event in _chat_run_tool_loop(_state, **_tool_loop_kw):
            yield event
    except asyncio.CancelledError:
        # Stop/abort during setup: CancelledError (BaseException in 3.8+)
        # bypasses `except Exception` below. The loop-phase handler inside
        # _run_tool_loop already deals with the agent-running case; here
        # the run died pre-handoff. Releasing the slot AND killing the run
        # leaves the saved user message permanently unanswered — instead
        # re-drive the tool loop detached so the answer is produced and
        # self-saved. The slot is NOT released here: the recovery run
        # adopts it (identity-checked); a deliberate stop released it
        # already, so the recovery aborts cleanly via supersession.
        if _state.detached_agent_task is None and _recovery_task is None:
            _slot_still_ours = False
            try:
                _slot_still_ours = (
                    _reserved_agent_state is not None
                    and _agent_registry.get_local(conversation_id) is _reserved_agent_state
                )
            except Exception:
                _slot_still_ours = False
            if _slot_still_ours:
                _recovery_task = asyncio.create_task(_recover_tool_loop())
                _track_bg_task(_recovery_task, conversation_id, "setup_recovery")
            else:
                # Stop endpoint already released the slot (deliberate
                # stop) — no recovery; keep the dead-setup release so the
                # conversation is not wedged.
                await _release_setup_slot_safely()
        raise
    except Exception:
        # 4.8 session lock: setup died before the reservation handoff —
        # release the slot (identity-checked, adopted-or-provisional), so
        # the conversation is never wedged behind a dead running-claim.
        if _state.detached_agent_task is None:
            await _release_setup_slot_safely()
        raise
    try:
        from app.services.proactive_learning_service import ProactiveLearningService
        _pls = ProactiveLearningService()
        await _pls.update_user_model(db, current_user.id)
    except Exception:
        pass


async def stream_chat_response(request, req, db, current_user):
    _sync_chat_globals()
    conversation_id = request.conversation_id
    assistant_id = request.assistant_id

    # 部署排空（2026-10-06，conv ae9aa092 P2）：drain 中的进程拒收新 run——
    # stop.sh 排空窗口内的新请求打给新进程/稍后重试。必须在任何写库（会话
    # 创建/用户消息落库/chat_session）之前：503 不得留下半拉子持久化状态。
    if _agent_registry.is_draining:
        raise HTTPException(
            status_code=503,
            detail="server is draining for restart — please retry in a few seconds",
            headers={"Retry-After": "5"},
        )

    # Capture user identity scalars UP FRONT: later code paths (media
    # localization in self-save / interrupted-save) run after the request
    # session may have rolled back, which EXPIRES current_user (rollback
    # expiry ignores expire_on_commit=False) — attribute access there would
    # raise DetachedInstanceError and silently drop the message being saved.
    _user_id_local: str = current_user.id
    _username_local: str | None = current_user.username

    # separate_v1 ownership arbitration (§7.1): an active delegated task owns
    # its execution conversation. Reject BEFORE any ChatSession/message write;
    # other conversations are unaffected.
    if conversation_id:
        from app.services.agent_task_submission_service import (
            active_separate_task_for_conversation,
        )

        active_task = await active_separate_task_for_conversation(db, conversation_id)
        if active_task is not None and str(active_task.user_id) == str(current_user.id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "task_active_steer_required",
                    "message": "该执行会话正由后台委派任务占用；请用补充要求（steer）或先取消任务",
                    "task_id": active_task.id,
                    "status": active_task.status,
                    "steer_url": f"/api/agent-tasks/{active_task.id}/messages",
                },
            )

    chat_session = ChatSession(
        user_id=current_user.id,
        conversation_id=None,
        assistant_id=assistant_id,
        started_at=datetime.utcnow(),
        message_count=0
    )
    db.add(chat_session)
    await db.flush()

    assistant = None
    if assistant_id:
        result = await db.execute(
            select(Assistant).where(
                Assistant.id == assistant_id,
                Assistant.user_id == current_user.id
            )
        )
        assistant = result.scalar_one_or_none()

    if conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == current_user.id
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation and not assistant and conversation.assistant_id:
            fallback_result = await db.execute(
                select(Assistant).where(
                    Assistant.id == conversation.assistant_id,
                    Assistant.user_id == current_user.id
                )
            )
            assistant = fallback_result.scalar_one_or_none()
            if assistant:
                assistant_id = conversation.assistant_id
        if not conversation:
            conversation = Conversation(
                user_id=current_user.id,
                title="新对话",
                assistant_id=assistant_id
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
            conversation_id = conversation.id
        elif assistant_id and conversation.assistant_id is None:
            conversation.assistant_id = assistant_id
            await db.commit()
    else:
        conversation = Conversation(
            user_id=current_user.id,
            title="新对话",
            assistant_id=assistant_id
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        conversation_id = conversation.id

    chat_session.conversation_id = conversation_id
    await db.commit()

    # W16 设备执行模式：执行环境创建即定（D-6，docs/PLAN_device_execution_mode.md §6.1）。
    await _resolve_execution_target_for_conversation(
        db, current_user, conversation, getattr(request, "execution_target", None)
    )

    # 4.8 session lock: at most one active agent per conversation. A second
    # POST while an agent is still running would spawn a concurrent loop
    # writing to the same conversation (double-saved messages, polluted
    # stream state, racing DB writes). reserve() atomically claims the slot
    # BEFORE the user message insert — closing the check-then-act race
    # between guard and agent start. Cross-worker liveness is covered by the
    # buffer check (stale/dead-worker running-claims are distrusted there).
    _reserved_agent_state = await _agent_registry.reserve(conversation_id)
    _agent_busy = _reserved_agent_state is None
    if not _agent_busy:
        try:
            _existing_buf = await stream_buffer_manager.get_buffer(conversation_id, current_user.id)
            _agent_busy = _existing_buf is not None and _existing_buf.is_running
        except Exception:
            _agent_busy = False
        if _agent_busy:
            # Buffer says a run is alive (possibly on another worker) — the
            # local reservation we just took must be released. Identity-checked
            # release that never touches shared state (the live run's
            # cross-worker snapshots belong to the other worker).
            if _reserved_agent_state is not None:
                await _agent_registry.release_reservation(conversation_id, _reserved_agent_state)
                _reserved_agent_state = None
    if _agent_busy:
        logger.info("Rejected concurrent /stream for conversation %s (agent already running)", conversation_id)

        async def _busy_events():
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "该会话已有正在进行的回答，请等待其完成或先停止。",
                    "code": "conversation_busy",
                    "conversation_id": conversation_id,
                }, ensure_ascii=False),
            }

        return EventSourceResponse(_busy_events())

    user_message = None  # 仅「新用户消息」轮（普通/编辑）绑定；regenerate 不产生新消息
    if request.regenerate_from_message_id:
        try:
            conversation_messages = await _trim_messages_for_regeneration(
                db,
                conversation_id=conversation_id,
                regenerate_from_message_id=request.regenerate_from_message_id,
                user_id=current_user.id,
            )
            # 重新生成 = 该会话的最新操作（2026-09-26 用户指令）：解除拖拽钉住
            # （sort_order 置 0），使其在 (sort_order asc, 最近活动 desc) 排序下回到
            # 所在分组/时间分类最上方；活动时间键随新助手消息落库后的
            # MAX(messages.created_at)（全角色口径，conversation.py）推进。
            # updated_at 由 onupdate 自动推进，不在这里显式写（A4.9 R1 M-5：排序
            # 不变式不允许 updated_at 显式驱动）。
            # 值守卫（余留清零波）：sort_order 已为 0 时跳过赋值——同值照发
            # 事件但跳 onupdate，会产生带旧 ts 的幻影事件被 LWW 等值判 STALE（§3.1）。
            if conversation.sort_order:
                conversation.sort_order = 0
            await db.commit()
        except BaseException:
            # 同编辑路径：setup 失败不占会话槽位（shield 防二次取消复漏）。
            if _reserved_agent_state is not None:
                try:
                    await asyncio.shield(_agent_registry.release_reservation(
                        conversation_id, _reserved_agent_state))
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                _reserved_agent_state = None
            raise
    elif request.edit_message_id:
        try:
            conversation_messages = await _trim_messages_for_edit(
                db,
                conversation_id=conversation_id,
                edit_message_id=request.edit_message_id,
                user_id=current_user.id,
            )
            # Save the new user message
            user_message = Message(
                conversation_id=conversation_id,
                role="user",
                content=request.messages[-1].content
            )
            conversation.updated_at = datetime.utcnow()
            # 继续旧会话后置顶（2026-09-15）：新用户消息 = 会话成为「最新活动」。
            # sort_order 置 0 使其在 (sort_order asc, 最近用户消息 desc) 排序下跳到
            # 所在分组/时间分类最上方（前端乐观置顶 + 刷新后由本写入保持一致）。
            # 值守卫（对齐 regenerate 分支 §3.1）：已为 0 跳过赋值，免幻影 STALE 事件。
            if conversation.sort_order:
                conversation.sort_order = 0
            db.add(user_message)
            await db.commit()
            # reload 也在保护圈内：此前若失败（DB 抖动/取消），删+建已落库、
            # 槽位必须释放，否则 180s provisional TTL 内重试被 conversation_busy 挡。
            conversation_messages = await _load_conversation_messages(db, conversation_id, limit=200)
        except BaseException:
            # 编辑 setup 失败（404/400 校验、DB 异常、取消）不占会话槽位：
            # identity-checked 释放，不碰共享态；shield 防二次取消在释放途中
            # 再抛 CancelledError 复漏槽位（同 _release_setup_slot_safely 口径）。
            if _reserved_agent_state is not None:
                try:
                    await asyncio.shield(_agent_registry.release_reservation(
                        conversation_id, _reserved_agent_state))
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                _reserved_agent_state = None
            raise
    else:
        user_message = Message(
            conversation_id=conversation_id,
            role="user",
            content=request.messages[-1].content
        )
        # Serialize the first message with execution-target binding/sync updates.
        # The row lock makes the no-message check and subsequent write atomic
        # with the other execution-target writers that use the same lock.
        locked_conversation = await db.execute(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == current_user.id,
            )
            .with_for_update()
        )
        conversation = locked_conversation.scalar_one()
        conversation.updated_at = datetime.utcnow()
        conversation.sort_order = 0
        db.add(user_message)
        await db.commit()
        conversation_messages = await _load_conversation_messages(db, conversation_id, limit=200)

    latest_user_query = next(
        (message.content for message in reversed(conversation_messages) if message.role == "user"),
        request.messages[-1].content if request.messages else "",
    )

    # §5.4 澄清检测：全 agentic —— 每条新用户消息无条件 fire-and-forget 异步 LLM
    # 判定（2026-09-20 用户指令：删除关键词预分类闸门 `detect_signal`；漏检=纠正
    # 永不处理，成本暂不考虑。LLM 自行判定 is_correction，非纠正则零业务写入）。
    # regenerate 是对旧轮的重新生成，不重复检测（也不存在新 user_message）。
    try:
        from app.core.config import get_config as _gc_clar
        from app.services.memory_runtime_state import memory_runtime_enabled as _mem_rt_clar
        _cfg_clar = _gc_clar()
        if (
            _mem_rt_clar(_cfg_clar)
            and not request.regenerate_from_message_id
            and (latest_user_query or "").strip()
        ):
            from app.services.memory_clarification_service import process_clarification
            _clar_uid = current_user.id
            _clar_conv = conversation_id
            _clar_msg_id = getattr(user_message, "id", None)
            _clar_text = latest_user_query

            async def _clarification_bg():
                try:
                    from app.db.database import AsyncSessionLocal as _ASL
                    async with _ASL() as _cdb:
                        await process_clarification(
                            _cdb, _clar_uid, _clar_text,
                            conversation_id=_clar_conv, message_id=_clar_msg_id)
                except Exception:
                    logger.debug("background clarification failed", exc_info=True)

            _ct = asyncio.create_task(_clarification_bg())
            _clarification_tasks.add(_ct)
            _ct.add_done_callback(_clarification_tasks.discard)
    except Exception:
        logger.debug("clarification scheduling failed", exc_info=True)

    # DEATHMATCH lifecycle handling
    if request.deathmatch_action:
        dm_mgr = DeathmatchManager(conversation)
        if request.deathmatch_action == "start":
            # D3 (2026-08-31 autonomy wave): re-enabling deathmatch with a
            # PARKED task (paused / human_gate / partial_complete, or a
            # mid-grilling park) RESUMES that task instead of restarting
            # from grilling. Only a done round or a fresh conversation
            # proceeds to intent classification / a new grilling round.
            if conversation.deathmatch_status in ("paused", "human_gate", "partial_complete", "grilling") and (
                conversation.deathmatch_goal
                or conversation.deathmatch_grilling_complete
                or conversation.deathmatch_status == "grilling"
            ):
                _status_before_resume = conversation.deathmatch_status
                if conversation.deathmatch_status == "partial_complete":
                    dm_mgr.resume_from_partial()
                else:
                    await _resume_deathmatch_from_paused(dm_mgr, db, latest_user_query)
                logger.info(
                    "Deathmatch re-enabled: resumed parked task for conversation %s (status=%s)",
                    conversation_id, _status_before_resume,
                )
                await db.commit()
            # If a previous deathmatch round has completed, preserve its compressed
            # summary before deciding whether to start a fresh grilling phase.
            else:
                if conversation.deathmatch_status == "done" and not conversation.deathmatch_context_summary:
                    await dm_mgr.compress_conversation_context(db)
                # When the user explicitly starts deathmatch after a completed round,
                # use the sub-agent intent classifier to decide whether this is a new
                # task (NEW_ROUND/CLARIFY) or just a discussion of the previous result
                # (DISCUSS). This prevents re-entering grilling for messages like
                # "你觉得写得怎么样？".
                if (
                    conversation.deathmatch_status == "done"
                    and conversation.deathmatch_context_summary
                ):
                    intent = await dm_mgr.classify_intent(latest_user_query, db)
                    if intent == "NEW_ROUND":
                        await dm_mgr.compress_conversation_context(db)
                        dm_mgr.activate_grilling()
                    elif intent == "REVISE":
                        dm_mgr.begin_revision(latest_user_query)
                    elif intent == "CLARIFY":
                        dm_mgr.activate_grilling()
                    # DISCUSS: keep deathmatch off; normal mode will carry the summary.
                else:
                    # Starting deathmatch from normal mode (or a stale state). Capture
                    # the existing conversation context first so the grilling phase can
                    # see what was discussed before the mode switch.
                    if not conversation.deathmatch_context_summary:
                        await dm_mgr.compress_conversation_context(db)
                    dm_mgr.activate_grilling()
                await db.commit()
        elif request.deathmatch_action == "stop":
            # Compress context before exiting deathmatch so the conversation can
            # continue in normal mode with background context.
            if conversation.deathmatch_status in ("active", "done"):
                await dm_mgr.compress_conversation_context(db)
            dm_mgr.deactivate()
            await db.commit()
        elif request.deathmatch_action == "pause":
            dm_mgr.pause()
            await db.commit()
        elif request.deathmatch_action == "resume":
            await _resume_deathmatch_from_paused(dm_mgr, db, latest_user_query)
            await db.commit()
    elif request.deathmatch_mode:
        # Frontend says deathmatch is ON but no explicit action — ensure grilling
        # is active.  If the conversation is not already in "grilling" or
        # "active" goal-loop phase, force it into grilling so the user always
        # gets asked clarification questions first.
        if conversation.deathmatch_status == "active":
            # Goal loop is active but idle (between runs). A user message here
            # may be a side question about the results rather than a push to
            # continue. Classify intent: DISCUSS → pause the loop so the agent
            # just answers the question once instead of re-igniting the goal
            # loop; NEW_ROUND/CLARIFY → keep the loop active (current behavior).
            # Bare resume commands ("继续" etc.) never pause — they mean
            # "keep going", especially when the loop is zombie-active (the
            # runner died and the status just hasn't been reconciled yet).
            # The grilling-complete kickoff message ("目标已明确，请开始执行：…",
            # auto-sent by the frontend right after grilling completes) must
            # never be classified either — it IS the goal-loop start signal;
            # classifying it as DISCUSS paused the loop with 0 turns (conv
            # 51d74833).
            dm_mgr = DeathmatchManager(conversation)
            _q = latest_user_query.strip().lower()
            _is_resume_cmd = len(_q) <= 12 and any(
                h in _q for h in ("继续", "接着", "resume", "continue", "go on")
            )
            _is_goal_kickoff = latest_user_query.strip().startswith("目标已明确，请开始执行")
            if not _is_resume_cmd and not _is_goal_kickoff:
                intent = await dm_mgr.classify_intent(latest_user_query, db)
                if intent == "DISCUSS":
                    dm_mgr.pause(reason="user-discussion")
                    await db.commit()
                    logger.info(
                        "Deathmatch paused for discussion message in conversation %s",
                        conversation_id,
                    )
                elif intent == "REVISE":
                    # User dissatisfaction/rework while the loop is active:
                    # keep running — the message itself is corrective input.
                    logger.info(
                        "Deathmatch kept active for revision message in conversation %s",
                        conversation_id,
                    )
        elif conversation.deathmatch_status not in ("grilling", "active"):
            dm_mgr = DeathmatchManager(conversation)
            if conversation.deathmatch_status in ("paused", "human_gate"):
                # D2 (2026-08-31 autonomy wave): ANY user message while
                # paused/gated resumes the goal loop — supplemental content
                # becomes steering input for the next turn (补充即恢复).
                # Stopping is done via the mode toggle (stop action), not
                # via DISCUSS gating.
                await _resume_deathmatch_from_paused(dm_mgr, db, latest_user_query)
                logger.info(
                    "Deathmatch resumed from %s for conversation %s (any-message-resume)",
                    conversation.deathmatch_status, conversation_id,
                )
            elif conversation.deathmatch_status == "partial_complete":
                # "partial_complete" = the goal is mostly done but stalled.
                # User sending a message means "continue pushing toward the goal".
                # Resume WITHOUT resetting the stall counter so repeated stalls
                # escalate to human_gate.
                dm_mgr.resume_from_partial()
                logger.info(
                    "Deathmatch resumed from partial_complete for conversation %s (stall count preserved)",
                    conversation_id,
                )
            elif conversation.deathmatch_context_summary:
                # "done" or other non-active state with a prior summary: use the
                # sub-agent intent classifier to decide new round vs discuss.
                intent = await dm_mgr.classify_intent(latest_user_query, db)
                if intent == "NEW_ROUND":
                    await dm_mgr.compress_conversation_context(db)
                    dm_mgr.activate_grilling()
                elif intent == "REVISE":
                    # 2026-09-20 (conv a104fbc5): a critique/rework instruction
                    # on a completed round reopens execution instead of
                    # degenerating into a fact-check chat with no revision.
                    dm_mgr.begin_revision(latest_user_query)
                elif intent == "CLARIFY":
                    dm_mgr.activate_grilling()
                else:
                    # DISCUSS: keep deathmatch off, normal mode will carry summary.
                    pass
            else:
                # No prior summary yet: capture the existing normal-mode context
                # before entering grilling so the first round can reference it.
                await dm_mgr.compress_conversation_context(db)
                dm_mgr.activate_grilling()
            await db.commit()
    # Safety net: if conversation has deathmatch_mode=True from a previous
    # session but the status is stale (not grilling/active/paused/done), reactivate
    # grilling so the user always gets the clarification flow.  We exclude "done"
    # because a completed round is an intentional terminal state; re-entry must be
    # decided by the intent classifier above, not forced here.
    if (
        conversation.deathmatch_mode
        and conversation.deathmatch_status not in ("grilling", "active", "paused", "done", "human_gate", "partial_complete")
    ):
        dm_mgr = DeathmatchManager(conversation)
        dm_mgr.activate_grilling()
        await db.commit()
        # Reload conversation_messages after deathmatch state change
        conversation_messages = await _load_conversation_messages(db, conversation_id, limit=200)

    llm_service = agent_service.create_llm_service(assistant)

    # 模型网关（A4.9 复审 Critical-1）：wire 层 provider_type 以解析端点
    # 为准 —— model_alias 助手的 DB 旧列与新模型无关；legacy inline 端点
    # 携带旧列值，行为不变。嵌套闭包（_ensure_title 等）共享此值。
    from app.model_gateway.factory import wire_provider_type
    _wire_provider_type = wire_provider_type(
        llm_service, getattr(assistant, "provider_type", "deepseek") or "deepseek"
    )

    # P0 (2026-08-21, user requirement): coordinator/audit/aux LLM calls
    # inherit the assistant's model settings. Custom-model assistants get
    # their main client as coordinator; non-custom keep the operator's
    # global aux keys. The task-local aux override below makes every in-loop
    # AuxiliaryClient (classifiers/judges/error classify/compression) follow
    # the same rule — contextvars are task-scoped, so no explicit reset is
    # needed when the request task ends.
    coordinator_llm = agent_service.resolve_aux_model_context(assistant, llm_service)
    if assistant is not None and should_use_custom_model(assistant):
        from app.services.auxiliary_client import set_aux_llm_override
        set_aux_llm_override(llm_service)

    import re as _re
    skill_match = _re.search(r'\[skill:([a-zA-Z0-9_-]+)\]\s*(.*?)(?=\[skill:|$)', latest_user_query, _re.DOTALL)
    skill_content = None
    skill_files = None
    if skill_match:
        skill_name = skill_match.group(1)
        skill_args = skill_match.group(2) or ''
        try:
            # Unified dual-source resolver: system skills (backend/skills/*)
            # first, then user DB skills. See loop_improve.md Phase 1.3.
            resolved = await resolve_skill(skill_name, current_user)
            if resolved:
                skill_content = resolved["content"]
                if resolved.get("files"):
                    skill_files = resolved["files"]
                skill_marker = f"[skill:{skill_name}] {skill_args}".strip()
                replacement = f"[使用技能: {skill_name}]\n{skill_args}".strip()
                latest_user_query = latest_user_query.replace(skill_marker, replacement, 1)
                conversation_messages[-1].content = latest_user_query
            else:
                logger.warning(f"Skill '{skill_name}' not found for user {current_user.id}")
        except Exception as e:
            logger.error(f"Failed to load skill '{skill_name}': {e}")

    # Deterministic skill injection for uploaded files: when the user message
    # carries a [file-ref:...] marker and no explicit [skill:...] marker was
    # resolved, inject the file_parsing skill so the parsing workflow rules
    # are always present for file tasks (no reliance on model self-trigger).
    if not skill_content and _re.search(r'\[file-ref:', latest_user_query):
        try:
            resolved = await resolve_skill("file_parsing", current_user)
            if resolved:
                skill_content = resolved["content"]
                if resolved.get("files"):
                    skill_files = resolved["files"]
        except Exception as e:
            logger.error(f"Failed to load file_parsing skill: {e}")

    search_query, note_context_for_search = extract_search_query_and_note_context(latest_user_query)
    message_count = 1
    # P1-①（2026-10-04）：本轮注入的记忆 id（_memory_ctx 回填；终局步据此判采纳回写）
    _injected_mem_ids: list[str] = []
    _adoption_tasks: set = set()

    async def _ensure_title(refreshed_conversation, full_response: str) -> str | None:
        """Generate a title for a brand-new conversation and persist it.
        Always returns a non-default title when called on a '新对话' row, using
        the fallback derivation if the LLM call fails. Safe to call multiple
        times (a no-op when the title has already been updated)."""
        if not refreshed_conversation or refreshed_conversation.title != "新对话":
            return refreshed_conversation.title if refreshed_conversation else None
        try:
            # P0 / A4.9 Minor-7: provider-config assistants (qwen3.8_vllm
            # with an empty row-level URL) resolve through the main client —
            # the title LLM must not fall back to global deepseek.
            title_generator = TitleGeneratorService(
                **agent_service.title_generator_kwargs(assistant, llm_service),
                provider_type=_wire_provider_type,
            )
            _title_query = search_query or latest_user_query or ""
            generated_title: str | None = None
            try:
                generated_title = await title_generator.generate_title(
                    user_query=_title_query,
                    assistant_response=full_response or "",
                )
            except Exception:
                import traceback as _tb
                _tb.print_exc()
                generated_title = None

            if not generated_title:
                generated_title = await title_generator.get_fallback_title(_title_query)
            # Extra safety: never leave "新对话" if we got here.
            if not generated_title or generated_title == "新对话":
                _src = (_title_query or full_response or "").strip()
                if _src:
                    generated_title = _src[:16]
                else:
                    generated_title = "未命名会话"

            refreshed_conversation.title = generated_title
            await db.commit()
            return generated_title
        except Exception:
            import traceback as _tb
            _tb.print_exc()
            return refreshed_conversation.title if refreshed_conversation else None

    async def _finalize_and_emit_done(
        *,
        assistant_message_id: str | None,
        final_content: str,
        final_tool_results_json: str | None,
        fallback: bool = False,
    ):
        """Unified terminal step: regenerate title if needed, push done event
        with the FINAL title (so the frontend never has to wait for a separate
        title_update event to display it), then optionally emit title_update
        for existing client-side listeners. Used by every exit path."""
        refreshed = None
        try:
            conv_result = await db.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            refreshed = conv_result.scalar_one_or_none()
        except Exception:
            refreshed = conversation

        final_title = await _ensure_title(refreshed, final_content)

        # P1-① 采纳反馈闭环（fire-and-forget，fail-open）：最终回答引用了本轮
        # 注入的记忆证据（名/别名命中）→ 权重/边权回写（memory_adoption_service）。
        if _injected_mem_ids and (final_content or "").strip():
            try:
                from app.services.memory_adoption_service import spawn_answer_adoption
                _task = spawn_answer_adoption(
                    current_user.id, list(_injected_mem_ids), final_content)
                _adoption_tasks.add(_task)
                _task.add_done_callback(_adoption_tasks.discard)
            except Exception:
                logger.debug("adoption scheduling failed (fail-open)", exc_info=True)

        # Compress tool_results for the SSE done event. The full payload is
        # already persisted in the DB; a huge string (> 30 KB with browser
        # page content) would cause frontend JSON.parse to fail silently and
        # skip the done event entirely, leaving no assistant bubble rendered.
        # Instead of truncating (which breaks JSON), strip the large
        # agent_steps and keep only results/rounds needed for citation rendering.
        safe_tool_results = final_tool_results_json
        if final_tool_results_json and len(final_tool_results_json) > 8192:
            try:
                tr_obj = json.loads(final_tool_results_json)
                slim = {k: v for k, v in tr_obj.items() if k != "agent_steps"}
                safe_tool_results = json.dumps(slim, ensure_ascii=False)
                if len(safe_tool_results) > 8192:
                    slim["results"] = slim.get("results", [])[:20]
                    safe_tool_results = json.dumps(slim, ensure_ascii=False)
                if len(safe_tool_results) > 8192:
                    for r in slim.get("results", []):
                        if isinstance(r.get("result"), str) and len(r["result"]) > 500:
                            r["result"] = r["result"][:500] + "...[truncated]"
                    safe_tool_results = json.dumps(slim, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                safe_tool_results = json.dumps({"results": [], "error": "tool_results too large, stripped for SSE"}, ensure_ascii=False)

        payload = {
            "conversation_id": conversation_id,
            "message_id": assistant_message_id,
            "title": final_title,
            "tool_results": safe_tool_results,
            "done": True,
        }
        if fallback:
            payload["fallback"] = True
        yield {"event": "done", "data": json.dumps(payload)}

        # Keep the legacy title_update event for clients that still listen
        # to it — but it is purely cosmetic now, since `done` already
        # carries the final title.
        try:
            if not await req.is_disconnected():
                yield {"event": "message", "data": json.dumps({
                    "title_update": {
                        "conversation_id": conversation_id,
                        "title": final_title,
                    },
                })}
        except Exception:
            pass


    await db.commit()
    try:
        db.expunge(conversation)
    except Exception:
        pass
    await db.close()

    _state = _ChatStreamState()
    _locals = locals()
    for _name in ('_dm_terminal_status_saved', '_injected_mem_ids', '_stream_buf', 'client_disconnected', 'detached_agent_task', 'full_reasoning', 'full_response', 'message_count', 'response_saved', 'sse_cancelled_flag', 'tool_results_json'):
        if _name in _locals:
            setattr(_state, _name, _locals[_name])
    return EventSourceResponse(_chat_event_generator(
        _state,
        **{k: _locals[k] for k in ('_reserved_agent_state', 'conversation_id', 'db', 'current_user', 'conversation', 'skill_files', 'latest_user_query', 'assistant', 'wire_provider_type', 'coordinator_llm', 'conversation_messages', '_username_local', 'search_query', '_wire_provider_type', 'req', 'llm_service', 'skill_content', 'chat_session', 'request', '_user_id_local', '_finalize_and_emit_done') if k in _locals},
    ))

# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent-loop compression glue (P5.2 move-only, 2026-10-18)."""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_config
from app.services.agent_loop_context import _resolve_context_length
from app.services.context_compressor import estimate_request_tokens_rough

logger = logging.getLogger(__name__)

config = get_config()


class CompressionMixin:
    def _ensure_compressor(self) -> None:
        """Lazily build the ContextCompressor with the same config defaults
        used at loop top (threshold, protected margins, context length, and
        deathmatch's more aggressive profile). Shared by the token-threshold
        trigger and the 遵循词 (canary) trigger."""
        if self._compressor is not None:
            return
        from app.services.context_compressor import ContextCompressor
        # A4.9 Important-2: llm=None — the compressor's AuxiliaryClient then
        # resolves the P0 task-local override (custom-model assistants) or
        # the operator's global [agent.auxiliary] compression_model key
        # (non-custom assistants). Passing coordinator_llm unconditionally
        # bypassed the global key for every non-custom deployment.
        self._compressor = ContextCompressor(quiet=True)
        compression_cfg = config.agent_compression
        if compression_cfg:
            self._compressor.threshold_percent = float(compression_cfg.get("threshold_percent", 0.65))
            self._compressor.protect_last_tokens = int(compression_cfg.get("protect_last_tokens", 20000))
            self._compressor.protect_first_n = int(compression_cfg.get("protect_first_n", 3))
        # Set the compressor's context_length from config so the
        # compression threshold matches the actual model context window.
        _ctx_len = _resolve_context_length(self.llm)
        if _ctx_len > 0:
            self._compressor.update_context_length(_ctx_len)
        # Deathmatch mode: use more aggressive compression settings to
        # handle high iteration counts (up to 9999 turns). Lower the
        # threshold so compression triggers sooner, and protect more
        # recent context so the agent doesn't lose the plan/goal.
        if self.deathmatch_manager and self.deathmatch_manager.is_goal_active:
            self._compressor.threshold_percent = min(self._compressor.threshold_percent, 0.5)
            self._compressor.protect_last_tokens = max(self._compressor.protect_last_tokens, 30000)
        self._attach_compressor_delivery(self._compressor)

    def _attach_compressor_delivery(self, compressor: Any) -> None:
        """D4：给 compressor 挂 vibeweaver 投递根/会话（压缩完成点投递）。

        根 = workspace_path（投递服务自会做 vibeweaver-active 判定）；会话键供
        pendingResume 的下一轮一次性消费。fail-open。
        """
        try:
            if not getattr(compressor, "delivery_root", None) and self.workspace_path:
                compressor.delivery_root = self.workspace_path
            if not getattr(compressor, "delivery_conv_id", ""):
                compressor.delivery_conv_id = getattr(self, "_delivery_conv_id", "") or ""
            compressor.delivery_active = bool(getattr(self, "vibeweaver_active", True))
            # W1-3（2026-10-23）：压缩摘要派生 taint 记账钩子（fail-open）
            if not getattr(compressor, "derived_provenance_hook", None):
                compressor.derived_provenance_hook = self._record_compaction_provenance
        except Exception:
            pass

    async def _record_compaction_provenance(self, conversation_id: str) -> None:
        """W1-3：压缩摘要 = 派生记忆——按 ledger 继承最严 taint 记 artifact
        账（artifact_type='compaction'）。fail-open。"""
        try:
            if not conversation_id:
                return
            from app.db.database import AsyncSessionLocal
            from app.services.context_continuity_service import (
                _record_derived_provenance)
            async with AsyncSessionLocal() as db:
                await _record_derived_provenance(db, str(conversation_id), "compaction")
                await db.commit()
        except Exception:
            logger.debug("compaction provenance recording failed-open", exc_info=True)

    async def _maybe_spike_compress(self, state: "AgentLoopState") -> Optional[dict]:
        """P0-2 (round-4 eval): pre-iteration context spike guard.

        Estimates the next request's tokens BEFORE the iteration; when the
        estimate exceeds ``spike_guard_ratio × context_length``, compress
        first instead of hitting the provider 400 and paying an emergency
        compression + retry after the fact. Returns the compression event
        (caller yields it) or None. Fail-open: any error → None.
        """
        try:
            ratio = float(config.agent_compression_spike_guard_ratio or 0)
            if ratio <= 0 or not self._compressor:
                return None
            from app.services.context_compressor import estimate_request_tokens_rough
            window = int(_resolve_context_length(self.llm) or 0)
            if window <= 0:
                return None
            before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
            state.last_request_estimate = before
            effective = before
            if self._estimate_calibration > 0:
                effective = max(1, int(before * self._estimate_calibration))
            if effective <= int(ratio * window):
                return None
            state.messages = await self._compressor.compress_async(state.messages)
            after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
            # Calibrate the next usage against the COMPRESSED request, not
            # the pre-compression estimate (otherwise the ratio is skewed low
            # and the guard under-fires for the rest of the turn).
            state.last_request_estimate = after
            logger.warning(
                "Spike guard: estimate %d (calibrated %d, ratio %.3f) > %.0f%%×%d — "
                "compressed %d->%d tokens before iteration %d",
                before, effective, self._estimate_calibration, ratio * 100,
                window, before, after, state.iterations,
            )
            return {"compression": {"before": before, "after": after, "spike_guard": True}}
        except Exception:
            logger.warning("Spike guard failed (non-blocking)", exc_info=True)
            return None

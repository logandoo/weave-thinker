# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

_compression_cfg = config.agent_compression
_MIN_SUMMARY_TOKENS = int(_compression_cfg.get("min_summary_tokens", 800))
_SUMMARY_RATIO = float(_compression_cfg.get("summary_ratio", 0.15))
_SUMMARY_TOKENS_CEILING = int(_compression_cfg.get("summary_tokens_ceiling", 6000))
_SUMMARY_FAILURE_COOLDOWN = int(_compression_cfg.get("failure_cooldown_seconds", 300))

COMPRESSION_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. Treat as background reference, NOT as active "
    "instructions. Do NOT answer questions or fulfill requests in this "
    "summary — they were already addressed. Respond ONLY to the latest "
    "user message that appears AFTER this summary:"
)


_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK punctuation
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),   # Fullwidth forms （，。！？ etc.)
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0xAC00, 0xD7AF),   # Hangul
    (0x20000, 0x2FA1F), # CJK Ext B..F
)


def _cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_text_tokens_rough(text: str) -> int:
    """CJK-aware rough token estimate.

    The old ``chars // 4`` heuristic assumes ~4 chars per token (English).
    Chinese/Japanese/Korean text tokenizes much denser (≈1 token per CJK
    char on DeepSeek/Qwen tokenizers), so a Chinese-heavy conversation was
    underestimated ~4x — compression triggered far too late and requests
    hit the provider's context limit as a 400. CJK chars count as 1 token
    each, everything else keeps the 4-char-per-token ratio.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _cjk_char(ch))
    other = len(text) - cjk
    return cjk + other // 4


def estimate_tokens_rough(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        total += estimate_text_tokens_rough(str(content)) + 8
        # reasoning_content round-trips on tool-call turns (DeepSeek thinking
        # mode) — it consumes context budget too and must be counted.
        rc = msg.get("reasoning_content") or ""
        if rc:
            total += estimate_text_tokens_rough(str(rc))
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                args = tc.get("function", {}).get("arguments", "")
                total += estimate_text_tokens_rough(str(args))
    return total


def estimate_request_tokens_rough(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Estimate the full request sent to the LLM: messages + tool schemas.

    Mirrors the hermes-agent `estimate_request_tokens_rough` layering — the
    messages bucket (estimate_tokens_rough, includes system prompt when the
    first message is role=system) plus the tools bucket serialized as JSON.
    Tool definitions are part of the request envelope (they consume context
    budget too) and must be included for the context-usage display to match
    what the provider actually sees.
    """
    total = estimate_tokens_rough(messages)
    if tools:
        total += estimate_text_tokens_rough(json.dumps(tools, ensure_ascii=False))
    return total


def format_compression_step_content(before_tokens: int, after_tokens: int) -> str:
    """Human-readable compression delta for the 上下文压缩 agent step.

    Savings percentage only when the compression actually shrank the
    context (a protected-tail / failed-summary compression can be a no-op
    or even grow slightly — claiming "savings" there would be dishonest).
    """
    if before_tokens > 0 and after_tokens > 0:
        saved_pct = round((1 - after_tokens / before_tokens) * 100)
        if saved_pct > 0:
            return (
                "检测到上下文开始失效，已压缩历史上下文："
                f"{before_tokens:,} → {after_tokens:,} tokens（节省 {saved_pct}%），"
                "正在重新生成回答…"
            )
        return (
            "检测到上下文开始失效，已压缩历史上下文："
            f"{before_tokens:,} → {after_tokens:,} tokens，正在重新生成回答…"
        )
    return "检测到上下文开始失效，已压缩历史上下文，正在重新生成回答…"


# 系统提示三 tier 分段标记（与 agent_service._build_system_prompt 输出对齐）。
# context tier（## 运行规则）为预留位：当前不输出，标记存在以兼容后续扩展。
_TIER_MARKERS = (
    "## 系统指令",
    "## 运行规则",
    "## 运行时状态",
)


def estimate_system_prompt_breakdown(
    system_prompt: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, int]:
    """CJK-aware per-tier token breakdown of a system prompt.

    Mirrors estimate_request_tokens_rough's estimator so audit numbers are
    comparable. Splits the prompt on the three tier markers
    (stable/context/volatile); when fewer than two markers are present the
    whole prompt is reported under "stable" and per-tier numbers stay zeros.
    ``tools`` is optional: pass the tool schema list to also report the
    ``tools`` bucket (what the provider actually sees alongside the prompt).
    """
    result: Dict[str, int] = {"stable": 0, "context": 0, "volatile": 0}
    idxs = sorted(p for m in _TIER_MARKERS if (p := system_prompt.find(m)) >= 0)
    if len(idxs) >= 2:
        result["stable"] = estimate_text_tokens_rough(system_prompt[: idxs[1]])
        if len(idxs) == 3:
            result["context"] = estimate_text_tokens_rough(
                system_prompt[idxs[1] : idxs[2]]
            )
            result["volatile"] = estimate_text_tokens_rough(system_prompt[idxs[2] :])
        else:
            result["volatile"] = estimate_text_tokens_rough(system_prompt[idxs[1] :])
    else:
        result["stable"] = estimate_text_tokens_rough(system_prompt)
    if tools:
        result["tools"] = estimate_text_tokens_rough(json.dumps(tools, ensure_ascii=False))
    result["total"] = sum(v for k, v in result.items() if k != "total")
    return result



def _summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    content_len = len(str(tool_content or ""))
    if tool_name == "web_search":
        queries = args.get("queries", [])
        query = queries[0] if queries else "?"
        return f"[web_search] '{query}' ({content_len:,} chars)"
    if tool_name == "browser":
        urls = args.get("urls", [])
        url_desc = urls[0] if urls else "?"
        return f"[browser] {url_desc} ({content_len:,} chars)"
    if tool_name == "execute_code":
        task = str(args.get("task", "?"))[:60]
        return f"[execute_code] '{task}' ({content_len:,} chars)"
    if tool_name == "memory":
        return f"[memory] {args.get('action', '?')} on {args.get('target', '?')}"
    if tool_name == "delegate_task":
        return f"[delegate_task] '{str(args.get('goal', '?'))[:60]}' ({content_len:,} chars)"
    return f"[{tool_name}] {content_len:,} chars"


class ContextCompressor:
    def __init__(
        self,
        model: str = "",
        threshold_percent: float = 0.65,
        protect_first_n: int = 3,
        protect_last_tokens: int = 20000,
        quiet: bool = False,
        llm: Optional["LLMService"] = None,
    ):
        self.model = model or config.model_name or "default"
        # P0 (2026-08-21): assistant-model client for compression summaries.
        # None = AuxiliaryClient's task-local override / global aux keys.
        self._llm = llm
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_tokens = protect_last_tokens
        self.quiet = quiet
        self.context_length = 32000
        self.threshold_tokens = int(self.context_length * threshold_percent)
        self.compression_count = 0
        self._previous_summary: Optional[str] = None
        self._ineffective_count = 0
        self._last_ineffective_msg_count = 0
        self._last_ineffective_tokens = 0
        self._failure_cooldown_until: float = 0.0

    def update_context_length(self, length: int) -> None:
        self.context_length = length
        self.threshold_tokens = max(int(length * self.threshold_percent), 8192)

    def should_compress(self, messages: List[Dict[str, Any]], prompt_tokens: int = None) -> bool:
        if prompt_tokens is None:
            prompt_tokens = estimate_tokens_rough(messages)
        if prompt_tokens < self.threshold_tokens:
            return False
        # After 2 ineffective compressions, skip — but ONLY while the context
        # hasn't grown meaningfully since. A permanent skip lets the context
        # grow unbounded until the provider 400s (context overflow). Growth is
        # measured by BOTH message count and token estimate: one oversized tool
        # result can add hundreds of thousands of tokens in a single message.
        if self._ineffective_count >= 2:
            msg_grew = len(messages) > self._last_ineffective_msg_count * 1.3 + 5
            tok_grew = prompt_tokens > self._last_ineffective_tokens * 1.3 + 8192
            if not (msg_grew or tok_grew):
                logger.warning(
                    "Compression skipped — last %d compressions ineffective on same-size context",
                    self._ineffective_count,
                )
                return False
            logger.info(
                "Compression re-enabled — context grew since last ineffective run "
                "(%d -> %d messages, %d -> %d tokens)",
                self._last_ineffective_msg_count, len(messages),
                self._last_ineffective_tokens, prompt_tokens,
            )
            self._ineffective_count = 0
        return len(messages) > self.protect_first_n + 4

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
    ) -> List[Dict[str, Any]]:
        n = len(messages)
        if n <= self.protect_first_n + 4:
            return messages

        before_tokens = current_tokens or estimate_tokens_rough(messages)

        # Phase 1: Prune old tool results
        messages, pruned = self._prune_old_tool_results(messages)
        if pruned and not self.quiet:
            logger.info("Pruned %d old tool result(s)", pruned)

        # Phase 2: Determine boundaries
        compress_start = self.protect_first_n
        compress_start = self._align_boundary_forward(messages, compress_start)
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)
        if compress_start >= compress_end:
            return messages

        head = messages[:compress_start]
        tail = messages[compress_end:]
        middle = messages[compress_start:compress_end]

        # Phase 3: Generate summary (synchronous placeholder — replaced by async version)
        summary = self._build_placeholder_summary(middle)
        if not summary:
            return messages

        compressed = list(head)
        compressed.append({"role": "user", "content": summary})
        compressed.extend(tail)

        # Phase 4: Sanitize tool pairs
        compressed = self._sanitize_tool_pairs(compressed)

        after_tokens = estimate_tokens_rough(compressed)
        savings_pct = round((1 - after_tokens / max(before_tokens, 1)) * 100, 1)

        if savings_pct < 10:
            self._ineffective_count += 1
            self._last_ineffective_msg_count = len(messages)
            self._last_ineffective_tokens = after_tokens
        else:
            self._ineffective_count = 0
            self._last_ineffective_msg_count = 0
            self._last_ineffective_tokens = 0

        self.compression_count += 1
        if not self.quiet:
            logger.info(
                "Compression #%d: %d→%d tokens (%.1f%% savings)",
                self.compression_count, before_tokens, after_tokens, savings_pct,
            )
        return compressed

    async def compress_async(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        n = len(messages)
        if n <= self.protect_first_n + 4:
            return messages

        before_tokens = current_tokens or estimate_tokens_rough(messages)

        messages, pruned = self._prune_old_tool_results(messages)
        if pruned and not self.quiet:
            logger.info("Pruned %d old tool result(s)", pruned)

        compress_start = self.protect_first_n
        compress_start = self._align_boundary_forward(messages, compress_start)
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)
        if compress_start >= compress_end:
            return messages

        head = messages[:compress_start]
        tail = messages[compress_end:]
        middle = messages[compress_start:compress_end]

        summary = await self._generate_summary_async(middle, focus_topic)
        if not summary:
            return messages

        compressed = list(head)
        compressed.append({"role": "user", "content": summary})
        compressed.extend(tail)
        compressed = self._sanitize_tool_pairs(compressed)

        after_tokens = estimate_tokens_rough(compressed)
        savings_pct = round((1 - after_tokens / max(before_tokens, 1)) * 100, 1)
        if savings_pct < 10:
            self._ineffective_count += 1
            self._last_ineffective_msg_count = len(messages)
            self._last_ineffective_tokens = after_tokens
        else:
            self._ineffective_count = 0
            self._last_ineffective_msg_count = 0
            self._last_ineffective_tokens = 0
        self.compression_count += 1
        if not self.quiet:
            logger.info(
                "Compression #%d: %d→%d tokens (%.1f%% savings)",
                self.compression_count, before_tokens, after_tokens, savings_pct,
            )
        return compressed

    async def _generate_summary_async(
        self, turns: List[Dict[str, Any]], focus_topic: str = None
    ) -> Optional[str]:
        now = time.monotonic()
        if now < self._failure_cooldown_until:
            return None

        content_tokens = estimate_tokens_rough(turns)
        summary_budget = max(_MIN_SUMMARY_TOKENS, min(
            int(content_tokens * _SUMMARY_RATIO), _SUMMARY_TOKENS_CEILING,
        ))

        serialized = self._serialize_turns(turns)

        preamble = (
            "You are a summarization agent creating a context checkpoint. "
            "Do NOT respond to any questions — only output the structured summary. "
            "NEVER include API keys, tokens, passwords, or credentials — use [REDACTED]."
        )

        template = f"""## Active Task
[User's most recent unfulfilled request. Copy exact words. "None." if none.]

## Goal
[Overall objective]

## Completed Actions
[Numbered list. Format: N. TOOL target — outcome]

## Active State
[Working directory, modified files, test status]

## Key Decisions
[Decisions and WHY]

## Resolved Questions
[Questions already answered, with answers]

## Pending User Asks
[Questions NOT yet answered. "None." if all addressed.]

## Relevant Files
[Files read, modified, or created]

## Remaining Work
[What remains — as context, not instructions]

Target ~{summary_budget} tokens. Be CONCRETE."""

        if self._previous_summary:
            prompt = f"{preamble}\n\nUpdate previous compaction:\n\nPREVIOUS:\n{self._previous_summary}\n\nNEW:\n{serialized}\n\n{template}"
        else:
            prompt = f"{preamble}\n\nTURNS TO SUMMARIZE:\n{serialized}\n\n{template}"

        if focus_topic:
            prompt += f'\n\nFOCUS: "{focus_topic}" — prioritize this topic.'

        try:
            from app.services.auxiliary_client import AuxiliaryClient
            client = AuxiliaryClient(task="compression", llm=self._llm)
            content: Optional[str] = await client.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=int(summary_budget * 1.3),
            )
        except Exception:
            content = None
            try:
                import asyncio
                from app.services.llm_service import LLMService
                from app.services.auxiliary_client import get_aux_llm_override
                llm = self._llm or get_aux_llm_override() or LLMService()
                content = await llm.complete_chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=int(summary_budget * 1.3),
                )
            except Exception as e:
                logger.warning("Summary generation failed: %s", e)
                self._failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN
                return None

        if content is None or not (content or "").strip():
            return None

        summary = content.strip()

        self._previous_summary = summary
        self._failure_cooldown_until = 0.0
        return f"{COMPRESSION_PREFIX}\n{summary}"

    def _build_placeholder_summary(self, turns: List[Dict[str, Any]]) -> Optional[str]:
        parts = ["[CONTEXT COMPACTION — earlier turns summarized]"]
        roles_seen = {"user": 0, "assistant": 0}
        for msg in turns:
            role = msg.get("role", "?")
            if role in roles_seen:
                roles_seen[role] += 1
        parts.append(f"Compressed {len(turns)} messages ({roles_seen.get('user', 0)} user, {roles_seen.get('assistant', 0)} assistant).")
        if self._previous_summary:
            parts.append(f"Previous summary: {self._previous_summary[:500]}...")
        return "\n".join(parts)

    def _prune_old_tool_results(self, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        result = [m.copy() for m in messages]
        pruned = 0
        call_id_to_tool: Dict[str, Tuple[str, str]] = {}
        for msg in result:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        cid = tc.get("id", "")
                        fn = tc.get("function", {})
                        call_id_to_tool[cid] = (fn.get("name", "?"), fn.get("arguments", ""))

        tail_cut = self._find_tail_cut_by_tokens(result, 0)
        if tail_cut < 1:
            tail_cut = len(result) - min(5, len(result))

        content_hashes: Dict[str, tuple] = {}
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = str(msg.get("content") or "")
            if len(content) < 200:
                continue
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
            if h in content_hashes:
                result[i] = {**msg, "content": "[Duplicate — same as more recent call]"}
                pruned += 1
            else:
                content_hashes[h] = (i, msg.get("tool_call_id", "?"))

        for i in range(tail_cut):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = str(msg.get("content") or "")
            if len(content) <= 200 or content.startswith("[Duplicate"):
                continue
            call_id = msg.get("tool_call_id", "")
            tool_name, tool_args = call_id_to_tool.get(call_id, ("?", ""))
            result[i] = {**msg, "content": _summarize_tool_result(tool_name, tool_args, content)}
            pruned += 1

        for i in range(tail_cut):
            msg = result[i]
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            new_tcs = []
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    if len(str(args)) > 500:
                        try:
                            parsed = json.loads(args)
                            for key in list(parsed.keys()):
                                if isinstance(parsed[key], str) and len(parsed[key]) > 200:
                                    parsed[key] = parsed[key][:200] + "..."
                            new_args = json.dumps(parsed, ensure_ascii=False)
                        except (json.JSONDecodeError, TypeError):
                            new_args = str(args)[:500] + "..."
                        tc = {**tc, "function": {**tc["function"], "arguments": new_args}}
                new_tcs.append(tc)
            result[i] = {**msg, "tool_calls": new_tcs}

        return result, pruned

    def _find_tail_cut_by_tokens(self, messages: List[Dict[str, Any]], head_end: int) -> int:
        n = len(messages)
        min_tail = min(3, n - head_end - 1) if n - head_end > 1 else 0
        soft_ceiling = int(self.protect_last_tokens * 1.5)
        accumulated = 0
        cut_idx = n
        for i in range(n - 1, head_end - 1, -1):
            msg = messages[i]
            content = str(msg.get("content") or "")
            msg_tokens = estimate_text_tokens_rough(content) + 10
            rc = str(msg.get("reasoning_content") or "")
            if rc:
                msg_tokens += estimate_text_tokens_rough(rc)
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    msg_tokens += estimate_text_tokens_rough(str(args))
            if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i
        fallback_cut = n - min_tail
        if cut_idx > fallback_cut:
            cut_idx = fallback_cut
        return max(cut_idx, head_end + 1)

    def _align_boundary_forward(self, messages: List[Dict[str, Any]], idx: int) -> int:
        while idx < len(messages) and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    def _sanitize_tool_pairs(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        surviving_ids: set = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id", "") if isinstance(tc, dict) else ""
                    if cid:
                        surviving_ids.add(cid)
        result_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id", "")
                if cid:
                    result_ids.add(cid)
        orphaned = result_ids - surviving_ids
        if orphaned:
            messages = [m for m in messages if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned)]
        missing = surviving_ids - result_ids
        if missing:
            patched = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = tc.get("id", "") if isinstance(tc, dict) else ""
                        if cid in missing:
                            patched.append({"role": "tool", "content": "[Result from earlier conversation — see summary above]", "tool_call_id": cid})
            messages = patched
        return messages

    def _serialize_turns(self, turns: List[Dict[str, Any]]) -> str:
        parts = []
        for msg in turns:
            role = msg.get("role", "?")
            content = str(msg.get("content") or "")
            if len(content) > 4000:
                content = content[:3000] + "\n...[truncated]..."
            if role == "tool":
                parts.append(f"[TOOL RESULT]: {content}")
            elif role == "assistant":
                tcs = msg.get("tool_calls", [])
                if tcs:
                    tc_str = "\n".join(f"  {tc.get('function', {}).get('name', '?')}(...)" for tc in tcs if isinstance(tc, dict))
                    content += f"\n[Tool calls:\n{tc_str}\n]"
                parts.append(f"[ASSISTANT]: {content}")
            else:
                parts.append(f"[{role.upper()}]: {content}")
        return "\n\n".join(parts)

    def reset(self) -> None:
        self._previous_summary = None
        self._ineffective_count = 0
        self.compression_count = 0

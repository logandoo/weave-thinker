# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tool-result digest service — automatic near-lossless subagent reduction.

Motivation (2026-08-01): every tool result (file reads, web searches, browser
snapshots) used to be appended verbatim into the main agent's message history
(agent_loop.py). In complex multi-step tasks several large reads + searches
easily blow any model's context window.

Mechanism (opencode-style, see opencode/packages/opencode/src/tool/truncate.ts
and src/tool/task.ts):
- Results above ``min_digest_chars`` from content-heavy tools are handed to a
  *subagent* (a single LLM completion, no tools, isolated context) that reads
  the FULL text and returns a near-lossless structured digest.
- The FULL text is always persisted to a file and the file path is embedded in
  the ``<tool-digest>`` envelope returned to the main agent — lossless by
  pointer: the main agent can re-read any specific section on demand
  (workspace_read offset/limit, grep) without the whole payload in context.
- Digests for a batch of tool results run in parallel (configurable concurrency).
- On any subagent failure the original content is kept unchanged — the digest
  layer never loses information by itself.

The main agent only ever sees the small digest envelope instead of the raw
megabyte-scale tool output.
"""
import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIGEST_TOOLS = frozenset({
    "workspace_read",
    "web_search",
    "browser",
    "browser_snapshot",
    "session_search",
    "grep",
    "diff",
    "memory",
    "notes",
    "context7_query_docs",
})

_DEFAULT_MIN_CHARS = 8_000
_DEFAULT_MAX_CHARS = 6_000
_DEFAULT_MAX_CONCURRENT = 5
_DEFAULT_MAX_TOKENS = 8_192
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_TEMPERATURE = 0.3
_VERIFY_MAX_ISSUES = 5
_PERSIST_DIR_NAME = "tool_digests"


@dataclass
class DigestConfig:
    enabled: bool = False
    min_digest_chars: int = _DEFAULT_MIN_CHARS
    max_digest_chars: int = _DEFAULT_MAX_CHARS
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT
    max_tokens: Optional[int] = None  # 不设 == provider 默认最大输出（用户指令 2026-08-18）
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    batch_timeout_seconds: float = 300.0
    temperature: float = _DEFAULT_TEMPERATURE
    model: str = ""
    verify: bool = True
    digest_tools: frozenset = field(default_factory=lambda: DEFAULT_DIGEST_TOOLS)


DEFAULT_DIGEST_CONFIG = DigestConfig()


def should_digest(result: Any, config: DigestConfig = DEFAULT_DIGEST_CONFIG) -> bool:
    """True when *result* should be reduced by a subagent digest."""
    if not config.enabled:
        return False
    if getattr(result, "error", False):
        return False
    name = getattr(result, "name", "") or ""
    if name not in config.digest_tools:
        return False
    content = getattr(result, "result", "") or ""
    return len(content) > config.min_digest_chars


def _persist_dir(workspace_path: str = "") -> str:
    """Digest archives live inside the user workspace so the main agent can
    re-read them with workspace_read/grep (which sandbox to the workspace).
    Falls back to backend/output_files/tool_digests/ when no workspace.
    """
    if workspace_path:
        return os.path.join(workspace_path, _PERSIST_DIR_NAME)
    from app.core.config import get_config as _get_config
    try:
        root = _get_config().project_root
    except Exception:
        root = None
    if root:
        return os.path.join(str(root), "backend", "output_files", _PERSIST_DIR_NAME)
    return os.path.join(os.getcwd(), "output_files", _PERSIST_DIR_NAME)


def persist_full_output(content: str, tool_name: str, tool_use_id: str,
                        workspace_path: str = "") -> Optional[str]:
    """Always-write the full tool output to a file. Returns the file path.

    This is the lossless-by-pointer guarantee: whatever the main agent may
    need later is on disk, addressable by path.
    """
    try:
        persist_dir = _persist_dir(workspace_path)
        os.makedirs(persist_dir, exist_ok=True)
        fname = f"{tool_use_id or uuid.uuid4().hex[:12]}_{tool_name}.txt"
        fpath = os.path.join(persist_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        return fpath
    except Exception:
        logger.exception("Failed to persist digest source for %s", tool_name)
        return None


def _target_summary(result: Any) -> str:
    """Human-readable target (path / url / query) from the tool arguments."""
    args = getattr(result, "arguments", None) or {}
    for key in ("path", "file_path", "url", "query", "queries", "target", "keyword"):
        val = args.get(key)
        if val:
            if isinstance(val, list):
                return ", ".join(str(v) for v in val[:3])
            return str(val)
    return ""


def build_digest_envelope(
    tool_name: str,
    target: str,
    content: str,
    digest_text: str,
    fpath: Optional[str],
) -> str:
    size_str = f"{len(content):,}"
    lines = [
        "<tool-digest>",
        f"【工具】{tool_name}",
    ]
    if target:
        lines.append(f"【目标】{target}")
    lines.append(f"【原始大小】{size_str} 字符")
    if fpath:
        lines.append(f"【全文存档】{fpath}")
    lines.append(
        "【用法】本摘要由子智能体通读全文后生成，力求保留全部事实。"
        "如需查看摘要中的任何细节，请用对应工具读取全文存档的指定部分"
        "（workspace_read 支持 offset/limit 分页，grep 支持关键词定位），不要重复完整读取原文。"
    )
    lines.append("<digest>")
    lines.append(digest_text.strip())
    lines.append("</digest>")
    lines.append("</tool-digest>")
    return "\n".join(lines)


def build_system_prompt(max_digest_chars: int) -> str:
    return (
        "你是一个严格的「近无损信息压缩引擎」。用户会把一段工具输出原文交给你，"
        "你的唯一任务是把原文压缩成一段保留全部关键信息的结构化摘要，供上级智能体直接使用。\n"
        "\n"
        "## 信息保真铁律（违反任何一条即视为任务失败）\n"
        "1. 所有具体事实必须原样保留：数字、日期、百分比、金额、人名、公司/机构/产品名、"
        "型号、版本号、文件路径、URL、邮箱、命令、代码标识符。\n"
        "2. 关键结论与句子允许逐字引用（用引号包裹），不得改写其含义。\n"
        "3. 列表型内容（搜索结果、条目、条款）逐条保留，不得删除任何条目。\n"
        "4. 表格数据必须完整保留为 Markdown 表格，不得删行删列。\n"
        "5. 仅允许压缩：连接词、客套语、重复叙述、装饰性 Markdown 空白、"
        "与主题无关的广告/导航文字。\n"
        "\n"
        "## 搜索结果编号铁律（web_search 输出时强制执行）\n"
        "如果原文是联网搜索结果（条目含编号/Title/URL/摘要），摘要中每条结果必须"
        "保留【原始编号】+【标题】+【URL】+【压缩后的摘要】四要素，编号不得重排或合并。"
        "上级智能体用 [N] 角标引用这些编号对应到原始结果，编号错乱会导致引用指向错误来源。\n"
        "\n"
        "## 篇幅铁律（必须同时满足）\n"
        "1. 摘要总长度不得超过 {max_digest_chars} 字符。\n"
        "2. 摘要必须显著小于原文：如果原文超过 10000 字符，摘要不得超过原文的 50%。\n"
        "3. 超长表格（>30 行）的压缩方法：保留表头与列含义说明；给出统计描述"
        "（总行数、数值范围、合计/均值、异常值）；逐行列出【行号索引】；"
        "原文中与任务最可能相关的行（或用户可能询问的行）逐字保留若干条，"
        "并注明其余行号可凭索引用 offset/limit 按需读取。\n"
        "4. 当信息量确实超出篇幅限制时：优先保留事实密度最高的内容（数字、名称、"
        "结论、URL），牺牲句式完整；绝不允许为了让摘要变短而删事实。\n"
        "\n"
        "## 输出格式（Markdown，中文）\n"
        "## 来源\n"
        "工具、目标对象（路径/URL/查询词）、原始大小\n"
        "## 核心要点\n"
        "编号列表，每一条是一个独立事实，语言精炼但事实完整\n"
        "## 数据表（如有）\n"
        "按上述篇幅铁律第 3 条处理\n"
        "## 备注\n"
        "任何异常、缺失或需要注意的信息\n"
        "\n"
        "## 长度约束\n"
        f"输出不超过 {max_digest_chars} 字符。"
    )


def build_verify_prompt(content: str, digest_text: str) -> str:
    return (
        "你是一个严格的事实核对器。下面是工具输出的「原文」和基于原文生成的「摘要」。\n"
        "你的唯一任务：把摘要与原文逐项核对，找出：\n"
        "1. issues：摘要中与原文**冲突**的事实——摘要声称的内容与原文**不符**"
        "（错误的数字、日期、百分比、金额、名称、URL、路径、结论方向等），每项给出原文依据。\n"
        "2. missing：摘要**遗漏**的重要事实（对理解内容关键的数字、结论、条目）。\n"
        "3. 关键判定规则：摘要允许用【行号索引 + 统计 + 关键行】压缩超长表格，全文已存档可按需读取——"
        "被压缩省略的**具体行数据**（如某一行条目的数值）属于受控省略：\n"
        "   - 若摘要没有声称该行的数值 → 不是 issues，不算冲突；\n"
        "   - 若摘要声称了该行的数值但与原文不同 → 是 issues，必须列出；\n"
        "   - 只有明显丢失关键结论/核心统计数据（如总数、合计、平均值、异常值）才算 missing。\n"
        "4. 不要挑剔措辞、格式、篇幅；只核对事实。没有问题时 issues 和 missing 都为空数组。\n"
        "只输出一个 JSON 对象（不要输出任何其他文字或 Markdown 代码块标记），格式：\n"
        '{"issues": [{"原文": "...", "摘要中": "...", "正确内容": "..."}], '
        '"missing": ["遗漏事实1", "遗漏事实2"]}\n\n'
        "===== 原文开始 =====\n"
        f"{content}\n"
        "===== 原文结束 =====\n\n"
        "===== 摘要开始 =====\n"
        f"{digest_text}\n"
        "===== 摘要结束 ====="
    )


def _parse_verify_json(raw: str) -> Optional[Dict[str, list]]:
    """Parse the verifier's JSON, tolerating stray code fences. Returns None
    unless the payload is a JSON object (dict)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try to salvage a JSON object from the first { to the last }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    return parsed if isinstance(parsed, dict) else None


def append_verify_corrections(digest_text: str, issues: List[dict], missing: List[str]) -> str:
    """Append a 勘误 section to the digest with verifier corrections."""
    lines = [digest_text.strip(), "", "## 勘误（事实核对结果）"]
    if issues:
        lines.append("以下摘要内容与原文不符，以「正确内容」为准：")
        for it in issues:
            lines.append(f"- 摘要中「{it.get('摘要中', '?')}」→ 应为「{it.get('正确内容', '?')}」（原文：{it.get('原文', '?')}）")
    if missing:
        lines.append("以下重要事实摘要遗漏，补充如下：")
        for m in missing:
            lines.append(f"- {m}")
    if not issues and not missing:
        lines.append("无")
    return "\n".join(lines)


async def verify_digest(content: str, digest_text: str, child_llm: Any,
                        config: DigestConfig, provider_type: str = "deepseek") -> Optional[str]:
    """Fact-check pass: re-check the digest against the original (1 extra call).

    Returns the (possibly corrected) digest text, or None when the digest is
    deemed unreliable (verifier found more than ``_VERIFY_MAX_ISSUES``
    problems) — the caller must then keep the raw original. On verifier
    failure/parse failure the digest is returned unchanged — verification is
    best-effort hardening, never a failure path that loses the digest itself.
    """
    from app.services.provider_router import build_thinking_extra_body

    try:
        if config.timeout_seconds and config.timeout_seconds > 0:
            raw, _ = await asyncio.wait_for(
                child_llm.complete_chat_parts(
                    [{"role": "user", "content": build_verify_prompt(content, digest_text)}],
                    temperature=0.0,
                    max_tokens=config.max_tokens,
                    extra_body=build_thinking_extra_body(provider_type, False),
                ),
                timeout=config.timeout_seconds,
            )
        else:
            raw, _ = await child_llm.complete_chat_parts(
                [{"role": "user", "content": build_verify_prompt(content, digest_text)}],
                temperature=0.0,
                max_tokens=config.max_tokens,
                extra_body=build_thinking_extra_body(provider_type, False),
            )
    except Exception as e:
        logger.warning("tool_digest: verify pass failed (digest kept as-is): %s", e)
        return digest_text

    parsed = _parse_verify_json(raw or "")
    if not parsed:
        logger.warning("tool_digest: verify JSON unparseable (digest kept as-is)")
        return digest_text
    issues = [i for i in (parsed.get("issues") or []) if isinstance(i, dict)]
    missing = [m for m in (parsed.get("missing") or []) if isinstance(m, str)]
    if not issues and not missing:
        return digest_text
    logger.info("tool_digest: verify found %d issue(s) + %d missing fact(s)", len(issues), len(missing))
    # Only WRONG facts (issues) make the digest unreliable — fabricated data is
    # the dangerous kind. Omitted facts (missing) are recoverable through the
    # archive pointer and are appended as advisory corrections instead.
    if len(issues) > _VERIFY_MAX_ISSUES:
        logger.warning("tool_digest: verify found %d wrong facts — digest unreliable, keeping original",
                       len(issues))
        return None
    try:
        return append_verify_corrections(digest_text, issues, missing)
    except Exception as e:
        # Shape-safety: never let a malformed verifier payload discard a good
        # digest — corrections are best-effort.
        logger.warning("tool_digest: verify corrections append failed (digest kept as-is): %s", e)
        return digest_text


def _size_acceptable(digest_text: str, content: str, config: DigestConfig) -> bool:
    """The digest must be within the char cap AND significantly smaller than
    the original (otherwise the compression layer is pointless)."""
    original_len = len(content)
    cap = max(config.max_digest_chars, 1)
    if len(digest_text) > cap:
        return False
    if original_len > 10_000 and len(digest_text) > original_len * 0.5:
        return False
    return True


async def _generate_digest_text(messages: list, child_llm: Any, config: DigestConfig,
                                provider_type: str = "deepseek") -> Optional[str]:
    """Run the subagent digest completion, returning stripped text or None."""
    from app.services.provider_router import build_thinking_extra_body

    try:
        if config.timeout_seconds and config.timeout_seconds > 0:
            digest_text, _ = await asyncio.wait_for(
                child_llm.complete_chat_parts(
                    messages,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    # Digest subagents never think: thinking-mode responses can
                    # come back with empty content (DeepSeek returns reasoning
                    # only), which would silently disable the digest layer.
                    extra_body=build_thinking_extra_body(provider_type, False),
                ),
                timeout=config.timeout_seconds,
            )
        else:
            digest_text, _ = await child_llm.complete_chat_parts(
                messages,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                extra_body=build_thinking_extra_body(provider_type, False),
            )
    except Exception as e:
        logger.warning("tool_digest: subagent digest failed (keeping original): %s", e)
        return None
    return (digest_text or "").strip()


async def digest_one_result(result: Any, config: DigestConfig, parent_llm: Any,
                            provider_type: str = "deepseek",
                            workspace_path: str = "") -> Optional[Any]:
    """Digest a single tool result via a subagent LLM completion.

    Returns a NEW result whose ``result`` field is the ``<tool-digest>``
    envelope, or None (caller keeps the original) on any failure.
    """
    content = getattr(result, "result", "") or ""
    if not content:
        return None
    tool_name = getattr(result, "name", "") or "unknown_tool"

    from app.services.llm_service import LLMService
    from app.services.provider_router import build_thinking_extra_body

    try:
        if config.model:
            child_llm = LLMService(
                custom_api_url=(parent_llm.client.base_url if parent_llm.is_custom_provider else None),
                custom_api_key=(parent_llm.client.api_key if parent_llm.is_custom_provider else None),
                custom_model_name=config.model,
            )
        else:
            child_llm = LLMService(
                custom_api_url=(parent_llm.client.base_url if parent_llm.is_custom_provider else None),
                custom_api_key=(parent_llm.client.api_key if parent_llm.is_custom_provider else None),
                custom_model_name=parent_llm.custom_model_name,
            )
    except Exception as e:
        logger.warning("tool_digest: cannot build subagent LLM for %s: %s", tool_name, e)
        return None

    messages = [
        {"role": "system", "content": build_system_prompt(config.max_digest_chars)},
        {
            "role": "user",
            "content": (
                f"【工具】{tool_name}\n"
                f"【参数】{json.dumps(getattr(result, 'arguments', None) or {}, ensure_ascii=False)[:500]}\n"
                f"【原文大小】{len(content)} 字符\n"
                + (
                    "【特别说明】本输出是联网搜索结果，编号对应 web_search 结果的"
                    "原始序号，摘要中每条必须保留【原始编号+标题+URL+压缩摘要】四要素，"
                    "编号不得重排。\n"
                    if tool_name == "web_search"
                    else ""
                )
                + "===== 工具输出原文开始 =====\n"
                f"{content}\n"
                "===== 工具输出原文结束 ====="
            ),
        },
    ]

    digest_text = await _generate_digest_text(messages, child_llm, config, provider_type)
    if not digest_text:
        logger.warning("tool_digest: empty digest for %s (keeping original)", tool_name)
        return None

    # Size discipline: if the "digest" is still too large, retry ONCE with a
    # hard size instruction; if it is still too large, keep the raw original
    # (a near-copy digest defeats the purpose of the layer).
    if not _size_acceptable(digest_text, content, config):
        logger.warning(
            "tool_digest: digest for %s too large (%d chars) — regenerating with strict size cap",
            tool_name, len(digest_text),
        )
        strict_messages = list(messages) + [{
            "role": "user",
            "content": (
                f"上一版摘要过长（{len(digest_text)} 字符）。请重写：总长度必须控制在 "
                f"{config.max_digest_chars} 字符以内且不超过原文的 50%。"
                "超长表格按【行号索引 + 统计 + 关键行】方式压缩。事实保真铁律不变。"
            ),
        }]
        digest_text = await _generate_digest_text(strict_messages, child_llm, config, provider_type) or ""
        if not digest_text:
            logger.warning("tool_digest: empty digest after regeneration for %s (keeping original)", tool_name)
            return None
        if not _size_acceptable(digest_text, content, config):
            logger.warning(
                "tool_digest: digest for %s still too large after regeneration (keeping original)",
                tool_name,
            )
            return None

    # Fact-check pass: one extra LLM call re-checks the digest against the
    # original. Corrections are appended as a 勘误 section; if the digest is
    # found unreliable (too many problems) the raw original is kept.
    if config.verify:
        verified = await verify_digest(content, digest_text, child_llm, config, provider_type)
        if verified is None:
            logger.warning("tool_digest: digest rejected by verify pass for %s (keeping original)", tool_name)
            return None
        digest_text = verified

    fpath = await asyncio.to_thread(
        persist_full_output, content, tool_name,
        getattr(result, "call_id", "") or "", workspace_path,
    )
    # Lossless guarantee: if the full text could not be archived, the raw
    # result must stay in context — a summary alone would lose information.
    if fpath is None:
        logger.warning("tool_digest: archive write failed for %s (keeping original)", tool_name)
        return None
    envelope = build_digest_envelope(
        tool_name,
        _target_summary(result),
        content,
        digest_text,
        fpath,
    )
    logger.info(
        "tool_digest: %s reduced %d -> %d chars (full saved: %s)",
        tool_name, len(content), len(envelope), fpath or "n/a",
    )

    try:
        from app.services.agent_loop import ToolCallResult
        return ToolCallResult(
            call_id=getattr(result, "call_id", ""),
            name=tool_name,
            arguments=getattr(result, "arguments", {}),
            result=envelope,
            error=False,
        )
    except Exception:
        return None


async def digest_tool_results_batch(
    results: List[Any],
    config: DigestConfig = DEFAULT_DIGEST_CONFIG,
    parent_llm: Any = None,
    provider_type: str = "deepseek",
    workspace_path: str = "",
) -> List[Any]:
    """Digest all eligible results in *results* in parallel; order preserved.

    Ineligible/error results pass through untouched. If the subagent layer is
    disabled (or no result qualifies) the list is returned unchanged.
    """
    if not results or not config.enabled:
        return results
    targets = [r for r in results if should_digest(r, config)]
    if not targets:
        return results

    sem = asyncio.Semaphore(max(1, config.max_concurrent))

    async def _one(r):
        async with sem:
            return await digest_one_result(r, config, parent_llm, provider_type, workspace_path)

    # Batch-level deadline: prevents the digest phase from stalling the whole
    # loop past its own conversation-inactivity watchdog. Pending digests are
    # cancelled; their results stay original (safe fallback).
    tasks = {id(r): asyncio.ensure_future(_one(r)) for r in targets}
    if config.batch_timeout_seconds and config.batch_timeout_seconds > 0:
        done, pending = await asyncio.wait(
            list(tasks.values()), timeout=config.batch_timeout_seconds
        )
        for t in pending:
            t.cancel()
        if pending:
            logger.warning(
                "tool_digest: batch deadline reached (%ds) — %d digest(s) left as original",
                config.batch_timeout_seconds, len(pending),
            )
    else:
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        done = set(tasks.values())

    merged = list(results)
    target_idx = {id(r): i for i, r in enumerate(results)}
    for r, t in zip(targets, (tasks[id(r)] for r in targets)):
        if t not in done or t.cancelled():
            continue
        if t.exception():
            out = t.exception()
        else:
            out = t.result()
        if isinstance(out, Exception):
            logger.warning("tool_digest: unexpected error for %s (keeping original): %s",
                           getattr(r, "name", "?"), out)
            continue
        if out is not None:
            merged[target_idx[id(r)]] = out
    return merged

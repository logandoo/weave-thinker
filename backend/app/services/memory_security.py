# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

_MEMORY_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
    # B8（2026-09-14）：中文注入模式（本产品中文优先，旧词表仅英文可被绕过）
    (r'忽略(以上|之前|前面|先前|上述|所有)?(的)?(全部)?(指令|指示|要求|规则)', "prompt_injection_zh"),
    (r'无视(以上|之前|前面|上述|所有)(的)?(指令|指示|要求|规则)', "prompt_injection_zh"),
    (r'不要(告诉|告知|提醒)(用户|他|她)', "deception_hide_zh"),
    (r'(系统|角色)(提示词|指令)(覆盖|替换|更新)', "sys_prompt_override_zh"),
    (r'你现在(是|扮演|开始扮演)', "role_hijack_zh"),
    (r'(泄露|输出|打印|发送)(密钥|密码|token|凭据|凭证)', "exfil_secrets_zh"),
    (r'(读取|查看|打开)\s*(\.env|credentials|\.pgpass|\.npmrc)', "read_secrets_zh"),
    # P2.3 (2026-10-18): system-prompt exfiltration and hidden-tool abuse.
    (r'(输出|泄露|打印|展示|复述).{0,12}(系统|内部)(提示词|指令|规则)', "sys_prompt_exfil_zh"),
    (r'(?i)(reveal|print|show|repeat|dump).{0,24}(system|internal)\s+(prompt|instructions)', "sys_prompt_exfil"),
    (r'(调用|执行|启用).{0,20}(未提供|不存在|隐藏|未加载).{0,10}工具', "hidden_tool_abuse_zh"),
    (r'(?i)(call|invoke|enable).{0,24}(hidden|unavailable|unlisted).{0,12}tool', "hidden_tool_abuse"),
]

# 2026-10-18 (arXiv:2608.10218 Mind Viruses): self-propagating instructions are
# not classic prompt injection — they ask the *next* session/agent to carry a
# directive forward, often framed as continuity across a context wipe. The
# classic scanner above does not catch them. These patterns are deliberately
# narrow (agent/session + persistence verb) to keep benign user preferences out.
_SELF_PROPAGATION_PATTERNS = [
    (r"(?is)\b(?:when|after|next time)\b.{0,80}\b(?:context|memory|session)\b"
     r".{0,60}\b(?:wipe|wiped|reset|cleared|erased)\b.{0,160}"
     r"\b(?:continue|propagate|copy|write|tell|forward|spread|replicate)\b",
     "self_propagation_wipe"),
    (r"(?is)\b(?:copy|write|append|paste|save|persist)\b.{0,60}"
     r"\b(?:MEMORY\.md|AGENTS\.md|SOUL\.md|CLAUDE\.md)\b",
     "self_propagation_write"),
    (r"(?is)\b(?:tell|inform|message|instruct|forward|pass)\b.{0,50}"
     r"\b(?:next|future|other|new)\b.{0,24}\b(?:agent|session|instance|model)\b",
     "self_propagation_forward"),
    (r"(?is)\b(?:self[- ]?replicat|self[- ]?propagat|spread this|copy this onward|"
     r"continue the mission|carry this forward)\b", "self_propagation_explicit"),
    (r"(?:上下文|记忆|会话).{0,30}(?:被)?(?:清空|擦除|重置|抹掉).{0,120}"
     r"(?:继续|传播|复制|写入|告诉|转发|继承)", "self_propagation_wipe_zh"),
    (r"(?:复制|写入|追加|粘贴|保存|记录).{0,40}"
     r"(?:MEMORY\.md|AGENTS\.md|SOUL\.md|CLAUDE\.md|记忆文件)", "self_propagation_write_zh"),
    (r"(?:告诉|通知|转发|指示|传递).{0,30}"
     r"(?:下一个|下一位|未来的|其他|新的).{0,16}(?:agent|会话|实例|模型|助手)",
     "self_propagation_forward_zh"),
    (r"(?:自我复制|自我传播|继续传播|跨会话传播|把这条指令传下去)",
     "self_propagation_explicit_zh"),
]

from app.prompts.fragments import MEMORY_INSTRUCTION_BOUNDARY  # re-export (SEC-MEM-01)



def _normalized_variants(content: str) -> list[str]:
    """Encoding/Unicode variants a model may read as the same instruction.

    NFKC folds full-width Latin; html.unescape folds ``&#105;``-style
    entities.  We scan the original plus these variants so an attacker cannot
    bypass the blocklist by changing the representation, not the meaning.
    """
    import html
    import unicodedata

    text = str(content or "")
    variants = [text]
    folded = unicodedata.normalize("NFKC", text)
    if folded != text:
        variants.append(folded)
    unescaped = html.unescape(text)
    if unescaped != text:
        variants.append(unescaped)
    return variants


def scan_self_propagation(content: str) -> Optional[str]:
    """Detect instructions that try to persist/propagate across sessions.

    Returns a stable reason string (never raises). This is intentionally
    separate from ``scan_injection``: a mind virus can be polite, benign and
    framed as continuity, while still asking a future agent to act.
    """
    text = str(content or "")
    if not text:
        return None
    for candidate in _normalized_variants(text):
        for pattern, pid in _SELF_PROPAGATION_PATTERNS:
            try:
                if re.search(pattern, candidate, re.IGNORECASE):
                    return f"Blocked: content matches self-propagation pattern '{pid}'"
            except re.error:
                logger.warning("bad self-propagation regex %r", pid)
    return None


def _split_for_sanitize(text: str) -> list[str]:
    """Sentence/line split that keeps newlines for reassembly."""
    parts: list[str] = []
    for line in str(text or "").splitlines(keepends=True):
        if not line.strip():
            parts.append(line)
            continue
        # Keep CJK/ASCII sentence punctuation; do not split decimals.
        chunks = re.split(r"(?<=[。！？!?；;])", line)
        parts.extend(chunks)
    return parts


def sanitize_untrusted_text(content: str) -> tuple[str, list[str]]:
    """Strip instruction-bearing sentences from untrusted text.

    Used on generated memory summaries and compression summaries: facts stay,
    self-propagating/injection sentences are removed. Returns
    ``(cleaned, reasons)``; an empty reasons list means byte-identical input.
    """
    text = str(content or "")
    if not text:
        return text, []
    reasons: list[str] = []
    kept: list[str] = []
    for part in _split_for_sanitize(text):
        reason = scan_injection(part) or scan_self_propagation(part)
        if reason:
            reasons.append(reason)
            continue
        kept.append(part)
    cleaned = "".join(kept)
    if reasons:
        logger.warning("untrusted text sanitized: %s", "; ".join(sorted(set(reasons))[:4]))
    return cleaned, reasons


_PII_PATTERNS = [
    (r'1[3-9]\d{9}', '[PHONE]'),
    (r'\d{17}[\dXx]', '[ID_CARD]'),
    (r'\b[\w.+-]+@[\w-]+\.[\w.]+\b', '[EMAIL]'),
    (r'\d{16,19}', '[BANK_CARD]'),
    (r'(sk-|ak-|api[_-]?key[=:]\s*)\S+', '[API_KEY]'),
]


def scan_injection(content: str) -> Optional[str]:
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X}"
    for candidate in _normalized_variants(content):
        for pattern, pid in _MEMORY_THREAT_PATTERNS:
            if re.search(pattern, candidate, re.IGNORECASE):
                return f"Blocked: content matches threat pattern '{pid}'"
    return None


def scrub_pii(text: str) -> tuple[str, list[str]]:
    hits = []
    result = text
    for pattern, replacement in _PII_PATTERNS:
        if re.search(pattern, result):
            hits.append(replacement)
            result = re.sub(pattern, replacement, result)
    return result, hits


def pii_hit_labels(result: str, hits: list[str]) -> list[str]:
    if not hits:
        return []
    return sorted(set(hits))

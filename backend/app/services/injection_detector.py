# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P3: structured prompt-injection detector (prefilter + quarantine, not authority).

Design constraints from the plan (§4.8 / §6.3):

* the decision input is structured (channel / tool / origin / tenant), not raw
  text only;
* the deterministic structural score is a **prefilter** — it never grants or
  denies a consequential action; the TMA-NM act gate remains the authority;
* evaluation is reported at a **fixed false-positive budget** on benign
  own-tool-output (PIDS-style), never as a bare "recall" number;
* optional adapters (LLM judge, external classifier) are pluggable and report
  ``unavailable`` when not configured — no vendor model is assumed.

Modes (``[security] injection_detector_mode``):

* ``off`` (default) — no write-path behavior change;
* ``shadow`` — record a quarantine/telemetry row, never block;
* ``enforce`` — quarantine the write and return a recoverable error.

A detector hit is evidence about *text*, not proof of origin; TMA-NM's
origin/taint lattice stays the authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "enforce")
DETECTOR_NAME = "structural_prefilter"
DEFAULT_THRESHOLD = 0.5

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")

# (weight, pattern, label).  Weights add up; the score is clamped to 1.0.
_TAKEOVER = (
    (0.45, re.compile(r"(?i)\bignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|rules?|prompts?|messages?)"), "ignore_previous"),
    (0.45, re.compile(r"(?i)\bdisregard\s+(the\s+)?(system|previous|prior|above|developer)\b"), "disregard_system"),
    (0.45, re.compile(r"(忽略|无视|忘记|不要理会)(以上|之前|先前|上述|前面|所有)(的)?(所有)?(指令|指示|规则|要求|提示)"), "ignore_previous_zh"),
    (0.40, re.compile(r"(?i)\b(system|developer)\s+(prompt|message|instructions?)\b"), "system_prompt_ref"),
    (0.35, re.compile(r"(系统提示词|系统指令|开发者消息)"), "system_prompt_ref_zh"),
    (0.40, re.compile(r"(?i)\b(you\s+are\s+now|from\s+now\s+on\s+you\s+(are|will)|act\s+as\s+(the\s+)?(system|admin|root))"), "role_override"),
    (0.40, re.compile(r"(你现在是|从现在开始你|假装你是)(系统|管理员|开发者|root)?"), "role_override_zh"),
    (0.35, re.compile(r"(?i)\b(reveal|print|repeat|show|leak|output)\b.{0,24}\b(system\s+prompt|instructions?|hidden\s+rules?)\b"), "prompt_exfiltration"),
    (0.35, re.compile(r"(泄露|输出|显示|打印|重复).{0,12}(系统提示词|系统指令|隐藏规则)"), "prompt_exfiltration_zh"),
)
_STRUCTURE = (
    (0.30, re.compile(r"(?i)(<\|?im_start\|?>|\[/?INST\]|<\|system\|>|<<SYS>>)"), "chat_template_marker"),
    (0.30, re.compile(r"(?im)^\s*(#{1,3}\s*)?(\[|<)?\s*(system|developer|instruction)\s*(\]|>)?\s*[:：]"), "role_header"),
    (0.30, re.compile(r"(?i)(<provenance\b|</?context_continuity>|<memory_authority\b)"), "provenance_marker_forgery"),
    (0.25, re.compile(r"(?i)\b(BEGIN|START)\s+(SYSTEM|INSTRUCTION|PROMPT)\b"), "instruction_banner"),
)
_TOOL_DIRECTED = (
    (0.45, re.compile(r"(?i)\b(run|execute|eval)\b.{0,20}\b(command|script|payload|decoded)\b"), "execute_directed"),
    (0.45, re.compile(r"(执行|运行|调用).{0,10}(命令|脚本|载荷|解码)"), "execute_directed_zh"),
    (0.30, re.compile(r"(?i)\b(call|use|invoke|run|execute)\s+(the\s+)?(terminal|shell|bash|zsh|execute_code|browser|python|process)\b"), "tool_directed"),
    (0.30, re.compile(r"(执行|调用|运行|使用).{0,6}(终端|shell|bash|命令|浏览器|python|脚本)"), "tool_directed_zh"),
    (0.50, re.compile(r"(?i)\b(curl|wget)\s+https?://"), "network_command"),
    (0.35, re.compile(r"(?i)\brm\s+-rf\s+/(\s|$)"), "destructive_command"),
)
_COVER_TRACKS = (
    (0.30, re.compile(r"(?i)\b(do\s+not|don'?t|never)\s+(log|record|audit|report)\b"), "cover_tracks"),
    (0.30, re.compile(r"(不要|禁止|别)(记录|审计|留痕|上报)|删除(审计|日志|记录)"), "cover_tracks_zh"),
)
_EXFIL = (
    (0.30, re.compile(r"(?i)\b(post|send|upload|transmit|exfiltrate)\b.{0,28}\b(to|at)\s+https?://"), "exfiltration"),
    (0.30, re.compile(r"(上传|发送|外发|传输).{0,16}https?://"), "exfiltration_zh"),
)
# Programmatic (linear) endpoint check: a regex `https?://\S*(kw)` is O(n^2)
# on `https://`-dense input, which is reachable from an untrusted tool echo.
_URL_START = re.compile(r"https?://", re.I)
_EXFIL_KEYWORDS = ("webhook", "pastebin", "ngrok", "requestbin")
_MAX_SCAN_CHARS = 65536
_SELF_PROPAGATION = (
    (0.50, re.compile(r"(?i)\b(save|write|store|add|append)\b.{0,28}\b(memory|AGENTS\.md|USER\.md|CLAUDE\.md|system\s+prompt)\b"), "self_propagation"),
    (0.50, re.compile(r"(记住|保存|写入|添加到).{0,12}(记忆|长期记忆|AGENTS\.md|USER\.md|系统提示词)"), "self_propagation_zh"),
)
_AUTHORITY_CLAIM = (
    (0.25, re.compile(r"(?i)\b(as\s+(an?\s+)?(admin|administrator|root|system|developer)|admin(istrator)?\s+override|root\s+access\s+granted)\b"), "authority_claim"),
    (0.25, re.compile(r"(管理员授权|系统授权|已获得root|开发者权限)"), "authority_claim_zh"),
)
_REPORTING_CUE = re.compile(
    r"(?i)(warn|detect|example|attack|payload|report|describes?|mentions?|such as|e\.g\.|quoted?|"
    r"警告|检测|示例|例如|攻击|报告|描述|提到|引用|样例)"
)


@dataclass(frozen=True)
class DetectionInput:
    """Structured detection input: channel identity, not just text."""

    text: str
    channel: str = "unknown"       # memory_write | tool_output | web | note | summary | user
    tool_name: str = ""
    origin: str = "untrusted_external"
    tenant_id: str = ""
    language: str = ""
    bucket: str = ""               # evaluation bucket (tool class / language)

    def normalised_language(self) -> str:
        if self.language:
            return self.language
        cjk = sum(1 for ch in self.text if "\u4e00" <= ch <= "\u9fff")
        return "zh" if cjk >= max(4, len(self.text) // 20) else "en"


def _suppressed(text: str, match: re.Match) -> bool:
    """A quoted/reported attack example is data, not an instruction.

    Only the immediately preceding clause is inspected; this is a prefilter
    heuristic, not a claim about intent.
    """
    start = max(0, match.start() - 60)
    return bool(_REPORTING_CUE.search(text[start:match.start()]))


def _exfil_endpoint_tokens(text: str):
    """Yield URL tokens containing a known exfiltration host keyword."""
    for match in _URL_START.finditer(text):
        end = match.start()
        limit = min(len(text), match.start() + 256)
        while end < limit and not text[end].isspace() and text[end] not in "\"'<>()[]{}":
            end += 1
        token = text[match.start():end].lower()
        if any(keyword in token for keyword in _EXFIL_KEYWORDS):
            yield token


def _score_chunk(text: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    for group in (
        _TAKEOVER, _STRUCTURE, _TOOL_DIRECTED, _EXFIL,
        _SELF_PROPAGATION, _AUTHORITY_CLAIM, _COVER_TRACKS,
    ):
        for weight, pattern, label in group:
            for match in pattern.finditer(text):
                if _suppressed(text, match):
                    continue
                score += weight
                reasons.append(label)
                break  # one hit per signal label
    for _token in _exfil_endpoint_tokens(text):
        score += 0.25
        reasons.append("exfil_endpoint")
        break
    if _BASE64_BLOB.search(text):
        score += 0.20
        reasons.append("encoded_blob")
    return min(1.0, round(score, 4)), reasons


def _chunks(text: str, size: int = _MAX_SCAN_CHARS, overlap: int = 4096):
    """Overlapping windows so a payload in the middle is not skipped."""
    if len(text) <= size:
        yield text
        return
    step = max(1, size - overlap)
    for start in range(0, len(text), step):
        chunk = text[start:start + size]
        if not chunk:
            break
        yield chunk
        if start + size >= len(text):
            break


def score_text(inp: DetectionInput | str) -> dict[str, Any]:
    """Deterministic structural prefilter score in [0, 1]."""
    if isinstance(inp, str):
        inp = DetectionInput(text=inp)
    raw = str(inp.text or "")
    text = _ZERO_WIDTH.sub("", raw)
    had_invisible = text != raw
    best = 0.0
    reasons: list[str] = []
    for chunk in _chunks(text):
        chunk_score, chunk_reasons = _score_chunk(chunk)
        if chunk_score > best:
            best = chunk_score
        for reason in chunk_reasons:
            if reason not in reasons:
                reasons.append(reason)
    if had_invisible:
        best = min(1.0, best + 0.15)
        reasons.append("invisible_unicode")
    return {
        "score": round(best, 4),
        "reasons": reasons,
        "detector": DETECTOR_NAME,
        "authority": "prefilter",
    }


async def _semantic_score(
    inp: DetectionInput,
    judge: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
) -> Optional[float]:
    """Optional LLM judge merged into the score (never sole authority)."""
    try:
        from app.services.semantic_injection_guard import detect_semantic_instruction

        context = (
            f"channel={inp.channel} tool={inp.tool_name or '-'} "
            f"origin={inp.origin} language={inp.normalised_language()}"
        )
        verdict = await detect_semantic_instruction(
            f"{context}\n\n{inp.text}", judge=judge,
        )
        if verdict.get("is_instruction"):
            return max(0.0, min(1.0, float(verdict.get("confidence") or 0.0)))
    except Exception:
        logger.warning("semantic detector unavailable", exc_info=True)
    return None


async def _external_score(inp: DetectionInput) -> Optional[float]:
    """Optional external classifier adapter (off unless a URL is configured)."""
    try:
        from app.core.config import get_config
        url = str(get_config().injection_detector_external_url or "").strip()
    except Exception:
        url = ""
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            logger.warning("external injection detector requires https (or local http)")
            return None
        import httpx

        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(url, json={
                "text": str(inp.text or "")[:8000],
                "channel": inp.channel,
                "tool_name": inp.tool_name,
                "origin": inp.origin,
                "tenant_id": inp.tenant_id,
                "language": inp.normalised_language(),
            })
            response.raise_for_status()
            payload = response.json()
        value = payload.get("score") if isinstance(payload, dict) else None
        return None if value is None else max(0.0, min(1.0, float(value)))
    except Exception:
        logger.warning("external injection detector failed", exc_info=True)
        return None


async def detect(
    inp: DetectionInput,
    *,
    judge: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
    use_semantic: bool = False,
    use_external: bool = False,
    threshold: Optional[float] = None,
    mode: str = "shadow",
) -> dict[str, Any]:
    """Return the structured detection verdict (prefilter is the floor)."""
    base = await asyncio.to_thread(score_text, inp)
    score = float(base["score"])
    reasons = list(base["reasons"])
    detectors = [DETECTOR_NAME]
    external_available = False
    if use_semantic:
        semantic = await _semantic_score(inp, judge)
        if semantic is not None:
            detectors.append("semantic_judge")
            if semantic >= score:
                score = semantic
                reasons.append("semantic_judge")
    if use_external:
        external = await _external_score(inp)
        external_available = external is not None
        if external is not None:
            detectors.append("external_classifier")
            if external >= score:
                score = external
                reasons.append("external_classifier")
    effective_threshold = threshold if threshold is not None else default_threshold()
    return {
        "hit": score >= effective_threshold,
        "score": round(score, 4),
        "threshold": round(float(effective_threshold), 4),
        "detector": "+".join(detectors),
        "authority": "prefilter",
        "reasons": reasons,
        "mode": mode if mode in MODES else "shadow",
        "channel": inp.channel,
        "tool_name": inp.tool_name,
        "origin": inp.origin,
        "tenant_id": inp.tenant_id,
        "language": inp.normalised_language(),
        "external_available": external_available,
    }


def default_threshold() -> float:
    try:
        from app.core.config import get_config
        configured = float(get_config().injection_detector_threshold or 0.0)
    except Exception:
        return DEFAULT_THRESHOLD
    return configured if configured > 0 else DEFAULT_THRESHOLD


def select_threshold_at_fpr(
    benign_scores: Iterable[float],
    target_fpr: float,
) -> float:
    """Smallest threshold whose benign false-positive rate is <= target.

    With 24 benign items and a 1% target, zero benign hits are allowed;
    the threshold sits strictly above the highest benign score.
    """
    scores = sorted((float(s) for s in benign_scores), reverse=True)
    if not scores:
        return 1.0
    allowed = int(max(0.0, float(target_fpr)) * len(scores))
    if allowed >= len(scores):
        return 0.0
    if allowed <= 0:
        return min(1.0, scores[0] + 1e-6)
    return min(1.0, scores[allowed] + 1e-6)


def evaluate_corpus(
    corpus: Iterable[dict[str, Any]],
    *,
    target_fpr: float = 0.01,
    scorer: Optional[Callable[[DetectionInput], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """PIDS-style fixed-FPR evaluation over benign + labelled attack items."""
    scorer = scorer or score_text
    benign: list[dict[str, Any]] = []
    attacks: list[dict[str, Any]] = []
    for item in corpus:
        inp = DetectionInput(
            text=str(item.get("text") or ""),
            channel=str(item.get("channel") or "unknown"),
            tool_name=str(item.get("tool_name") or ""),
            origin=str(item.get("origin") or "untrusted_external"),
            tenant_id=str(item.get("tenant_id") or ""),
            language=str(item.get("language") or ""),
            bucket=str(item.get("bucket") or ""),
        )
        result = scorer(inp)
        row = {
            "score": float(result.get("score") or 0.0),
            "bucket": inp.bucket or inp.normalised_language(),
            "channel": inp.channel,
            "tool_name": inp.tool_name,
            "language": inp.normalised_language(),
            "reasons": list(result.get("reasons") or []),
        }
        if str(item.get("label") or "").lower() == "attack":
            attacks.append(row)
        else:
            benign.append(row)
    threshold = select_threshold_at_fpr(
        [row["score"] for row in benign], target_fpr,
    )
    benign_hits = [row for row in benign if row["score"] >= threshold]
    attack_hits = [row for row in attacks if row["score"] >= threshold]
    buckets: dict[str, dict[str, Any]] = {}
    for row in attacks:
        bucket = buckets.setdefault(row["bucket"], {"attacks": 0, "hits": 0})
        bucket["attacks"] += 1
        bucket["hits"] += int(row["score"] >= threshold)
    for bucket in buckets.values():
        bucket["recall"] = round(
            bucket["hits"] / max(1, bucket["attacks"]), 4,
        )
    language_buckets: dict[str, dict[str, Any]] = {}
    for row in attacks:
        bucket = language_buckets.setdefault(row["language"], {"attacks": 0, "hits": 0})
        bucket["attacks"] += 1
        bucket["hits"] += int(row["score"] >= threshold)
    for bucket in language_buckets.values():
        bucket["recall"] = round(bucket["hits"] / max(1, bucket["attacks"]), 4)
    benign_by_tool: dict[str, dict[str, Any]] = {}
    for row in benign:
        bucket = benign_by_tool.setdefault(
            row["tool_name"] or row["channel"], {"items": 0, "hits": 0},
        )
        bucket["items"] += 1
        bucket["hits"] += int(row["score"] >= threshold)
    for bucket in benign_by_tool.values():
        bucket["fpr"] = round(bucket["hits"] / max(1, bucket["items"]), 4)
    return {
        "threshold": round(threshold, 6),
        "target_fpr": float(target_fpr),
        "benign": len(benign),
        "attacks": len(attacks),
        "false_positives": len(benign_hits),
        "fpr": round(len(benign_hits) / max(1, len(benign)), 4),
        "recall": round(len(attack_hits) / max(1, len(attacks)), 4),
        "over_defense": [row["bucket"] for row in benign_hits],
        "buckets": buckets,
        "language_buckets": language_buckets,
        "benign_by_tool": benign_by_tool,
        "detector": DETECTOR_NAME,
        "authority": "prefilter",
        "runtime_threshold": round(default_threshold(), 4),
        "external": _external_availability(),
        "generated_at_epoch": int(time.time()),
    }


def _external_availability() -> dict[str, Any]:
    try:
        from app.core.config import get_config
        configured = bool(get_config().injection_detector_external_url)
    except Exception:
        configured = False
    return {
        "available": configured,
        "reason": "configured (not exercised)" if configured else "not configured",
    }


def build_write_input(content: str, *, channel: str = "memory_write", **kwargs) -> DetectionInput:
    try:
        from app.services.tenant_service import tenant_segment
        tenant = tenant_segment()
    except Exception:
        tenant = ""
    return DetectionInput(
        text=content,
        channel=channel,
        tool_name="memory",
        origin="agent",
        tenant_id=tenant,
    )


async def detect_for_write(
    content: str,
    *,
    mode: str,
    use_semantic: bool = False,
    judge: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
) -> dict[str, Any]:
    return await detect(
        build_write_input(content),
        mode=mode,
        use_semantic=use_semantic,
        use_external=True,  # no-op unless injection_detector_external_url is set
        judge=judge,
    )


async def record_quarantine(
    verdict: dict[str, Any],
    *,
    text: str,
    user_id: str = "",
    mode: str = "shadow",
    conversation_id: str = "",
) -> bool:
    """Persist a hash-only quarantine row; telemetry failure never blocks."""
    tenant_salt = str(verdict.get("tenant_id") or "")
    material = (tenant_salt + "\x00" + str(text or "")) if tenant_salt else str(text or "")
    content_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    try:
        from sqlalchemy import text as sa_text
        from app.db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(
                sa_text(
                    """
                    INSERT INTO injection_quarantine
                        (id, tenant_id, user_id, conversation_id, channel,
                         tool_name, origin, content_hash, detector, score,
                         reason, mode, created_at_epoch)
                    VALUES
                        (:id, :tenant_id, :user_id, :conversation_id, :channel,
                         :tool_name, :origin, :content_hash, :detector, :score,
                         :reason, :mode, :created_at_epoch)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": str(verdict.get("tenant_id") or ""),
                    "user_id": str(user_id or ""),
                    "conversation_id": str(conversation_id or ""),
                    "channel": str(verdict.get("channel") or ""),
                    "tool_name": str(verdict.get("tool_name") or ""),
                    "origin": str(verdict.get("origin") or ""),
                    "content_hash": content_hash,
                    "detector": str(verdict.get("detector") or "")[:80],
                    "score": float(verdict.get("score") or 0.0),
                    "reason": ",".join(verdict.get("reasons") or [])[:240],
                    "mode": str(mode or "shadow")[:20],
                    "created_at_epoch": int(time.time()),
                },
            )
            await db.commit()
        return True
    except Exception:
        logger.warning("injection quarantine write failed (non-blocking)", exc_info=True)
        return False


def load_corpus(path: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                logger.warning("bad corpus line skipped: %s", line[:80])
    return rows

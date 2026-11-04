# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Typed output contracts for JSON prompts (P1.4, plan 2026-10-18).

Prompt text and its parser are one versioned object: a wording change that
breaks the JSON shape must fail a contract test, not production.  Every model
here tolerates unknown/extra fields (providers vary) but validates the fields
the caller actually consumes.  ``parse_contract`` classifies failures so the
caller can retry once and then apply a bounded fallback *without* silently
swallowing the error.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TModel = TypeVar("TModel", bound=BaseModel)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class ContractErrorKind:
    EMPTY = "empty"
    NON_JSON = "non_json"
    TRUNCATED = "truncated"
    SCHEMA = "schema"
    TYPE = "type"


@dataclass(frozen=True)
class ContractFailure:
    kind: str
    message: str
    raw: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "message": self.message}


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def extract_json_value(raw: str) -> Tuple[Optional[Any], Optional[ContractFailure]]:
    """Extract the first JSON object/array.  Distinguishes truncated JSON.

    The contract deliberately accepts prose around the object (models do
    that), but a missing closing brace is classified as ``truncated`` so the
    caller can use a brevity retry instead of treating it as a schema error.
    """
    text = str(raw or "").strip()
    if not text:
        return None, ContractFailure(ContractErrorKind.EMPTY, "empty response")
    text = _strip_fence(text)
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    # Prefer an object container: prose often contains bracketed citations
    # such as "[1]" before/after the JSON, so choosing the earliest '[' would
    # span prose and lose a valid verdict.  Objects are the contract shape;
    # arrays are only the fallback when no '{' exists.
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    if start < 0:
        return None, ContractFailure(ContractErrorKind.NON_JSON, "no JSON value found", text[:400])
    decoder = json.JSONDecoder()
    try:
        value, _end = decoder.raw_decode(text[start:])
        return value, None
    except json.JSONDecodeError as exc:
        kind = (ContractErrorKind.TRUNCATED
                if "Unterminated" in str(exc) or "Expecting" in str(exc)
                else ContractErrorKind.NON_JSON)
        return None, ContractFailure(kind, f"invalid JSON: {exc}", text[start:start + 400])


def parse_contract(
    model: Type[TModel],
    raw: str,
    *,
    default: Optional[TModel] = None,
) -> Tuple[Optional[TModel], Optional[ContractFailure]]:
    """Parse ``raw`` into ``model``; returns ``(value, failure)``.

    A caller must handle the failure explicitly (retry / fallback / raise).
    There is no silent default here: pass ``default`` only when the caller has
    a named fallback policy.
    """
    value, failure = extract_json_value(raw)
    if failure is not None:
        return default, failure
    if not isinstance(value, dict):
        return default, ContractFailure(
            ContractErrorKind.TYPE, f"expected object, got {type(value).__name__}")
    try:
        return model.model_validate(value), None
    except ValidationError as exc:
        return default, ContractFailure(ContractErrorKind.SCHEMA, str(exc), raw[:400])


def parse_contract_with_repair(
    model: Type[TModel],
    raw: str,
    repair: Callable[[str, ContractFailure], str],
    *,
    default: Optional[TModel] = None,
) -> Tuple[Optional[TModel], Optional[ContractFailure], bool]:
    """One bounded repair retry (plan P1.4).

    ``repair`` is supplied by the caller (usually an LLM call with a brevity /
    schema hint).  Returns ``(value, failure, repaired)``.  On a second
    failure the original failure is returned and must be logged by the caller.
    """
    value, failure = parse_contract(model, raw, default=default)
    if failure is None:
        return value, None, False
    try:
        repaired_raw = repair(raw, failure)
    except Exception as exc:  # noqa: BLE001 - repair failure is reported, not hidden
        return default, ContractFailure(
            ContractErrorKind.SCHEMA, f"repair call failed: {exc}", raw[:400]), False
    value2, failure2 = parse_contract(model, repaired_raw, default=default)
    if failure2 is None:
        return value2, None, True
    return default, failure2, False


class _Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class AuditVerdict(_Contract):
    # Audit replies carry provider-specific extras (unsupported_claims, etc.);
    # keep them so the caller's existing dict consumers do not lose data.
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    # Empty (not "accept") when missing: _verdict_of treats an absent verdict
    # as reject, matching the legacy {"ok": false} shape.  Defaulting to
    # "accept" would silently ship an unreviewed draft.
    verdict: str = ""
    problem: str = ""
    reason: str = ""
    needs_evidence: Any = False
    compact: Any = False


class JudgeVerdict(_Contract):
    verdict: str = "continue"  # done|continue|wait|blocked|ask
    reason: str = "no reason provided"
    compact: bool = False
    taxonomy: Optional[str] = None
    wait_seconds: Optional[int] = None


class VerifierVerdict(_Contract):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    status: str = ""
    issues: list = Field(default_factory=list)
    retry_instruction: str = ""
    confidence: float = 0.5
    continuity_brief: str = ""
    diagnosis: str = ""
    verified: bool = False
    reason: str = ""
    missing: list[str] = Field(default_factory=list)


class TitleResult(_Contract):
    title: str


class ScheduleResult(_Contract):
    schedule_type: str
    amount: Optional[int] = None
    unit: Optional[str] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    dow: Optional[int] = None
    delay_amount: Optional[int] = None
    delay_unit: Optional[str] = None
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    duration_seconds: Optional[int] = None


class CoordinatorDecision(_Contract):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    needs_search: bool = False
    focus: str = ""
    coding_task: Optional[bool] = None
    direct_reply: bool = False
    route: str = ""
    expects_tools: bool = False
    search_required: Optional[bool] = None
    creative_turn: bool = False


class ClaimVerdict(_Contract):
    verdict: str = ""
    reason: str = ""
    claim: str = ""


class ScoutResult(_Contract):
    ids: list = Field(default_factory=list)
    keywords: list = Field(default_factory=list)


class ErrorClassification(_Contract):
    category: Optional[str] = None
    reason: str = ""


class MemoryExtraction(_Contract):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    facts: list[str] = Field(default_factory=list)
    concepts: list[dict] = Field(default_factory=list)
    entities: list[dict] = Field(default_factory=list)


class QueryPlan(_Contract):
    queries: list[Any] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    date_range: Optional[str] = None


class GrillingQuestions(_Contract):
    questions: list[dict] = Field(default_factory=list)


class PlanStep(_Contract):
    id: Any = ""
    description: Any = ""
    expected_output: Any = ""
    boundary: Any = None
    tools: list[Any] = Field(default_factory=list)


class PlanResult(_Contract):
    steps: list[PlanStep] = Field(default_factory=list)
    goal: str = ""


class TaxonomyResult(_Contract):
    taxonomy: Optional[str] = None
    reason: str = ""


CONTRACTS: Dict[str, Type[BaseModel]] = {
    "audit.verdict": AuditVerdict,
    "deathmatch.judge": JudgeVerdict,
    "deathmatch.verifier": VerifierVerdict,
    "title.generation": TitleResult,
    "schedule.parse": ScheduleResult,
    "coordinator.route": CoordinatorDecision,
    "claim.verify": ClaimVerdict,
    "audit.scout": ScoutResult,
    "error.classify": ErrorClassification,
    "memory.extraction": MemoryExtraction,
    "search.plan": QueryPlan,
    "deathmatch.grilling": GrillingQuestions,
    "deathmatch.plan": PlanResult,
}


def contract_for(prompt_id: str) -> Optional[Type[BaseModel]]:
    return CONTRACTS.get(prompt_id)

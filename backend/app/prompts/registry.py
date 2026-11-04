# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompt registry: metadata only; no runtime text storage beyond specs."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from app.prompts.spec import PromptSpec

_SPECS: Dict[str, PromptSpec] = {}


def register(spec: PromptSpec) -> PromptSpec:
    if not spec.id or not spec.version or not spec.owner:
        raise ValueError("PromptSpec requires id/version/owner")
    if spec.id in _SPECS and _SPECS[spec.id] != spec:
        raise ValueError(f"prompt id already registered: {spec.id}")
    _SPECS[spec.id] = spec
    return spec


def get(prompt_id: str) -> Optional[PromptSpec]:
    return _SPECS.get(prompt_id)


def require(prompt_id: str) -> PromptSpec:
    spec = get(prompt_id)
    if spec is None:
        raise KeyError(f"unregistered prompt: {prompt_id}")
    return spec


def all_specs() -> List[PromptSpec]:
    return sorted(_SPECS.values(), key=lambda s: s.id)


def manifest() -> List[dict]:
    from app.prompts.compiler import estimate_tokens

    out = []
    for spec in all_specs():
        text = spec.render()
        content_hash = spec.content_hash()
        stable_hash = __import__("hashlib").sha256(
            spec.stable_text().encode("utf-8", errors="replace")
        ).hexdigest()
        if not text and spec.flags.get("builder_hash"):
            content_hash = stable_hash = str(spec.flags["builder_hash"])
        out.append({
            "id": spec.id,
            "version": spec.version,
            "owner": spec.owner,
            "hash": content_hash,
            "stable_hash": stable_hash,
            "sections": [s.id for s in spec.ordered_sections()],
            "rule_ids": [rid for s in spec.sections for rid in s.rule_ids],
            "output_schema": spec.output_schema,
            "flags": spec.flags,
            "deprecated": spec.deprecated,
            "locale": spec.locale,
            "used_by": list(spec.used_by),
            "text_chars": len(text),
            "estimated_tokens": estimate_tokens(text),
        })
    return out


def deprecation_violations(today: str) -> List[str]:
    return [
        f"{spec.id}: deprecated, remove_after={spec.deprecated.get('remove_after')}"
        for spec in all_specs()
        if spec.is_deprecated_past_due(today)
    ]


def unused_prompts() -> List[str]:
    return [spec.id for spec in all_specs() if not spec.used_by]

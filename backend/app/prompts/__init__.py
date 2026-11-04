# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompt package public surface (no business text here)."""
from app.prompts.fragments import (
    CROSS_TURN_CITATION_NOTE,
    MEMORY_INSTRUCTION_BOUNDARY,
    MEMORY_RELEVANCE_BOUNDARY,
    MEMORY_BOUNDARY_SECTIONS,
    TOOL_AVAILABILITY_NOTE,
    UNTRUSTED_DATA_PREAMBLE,
    spotlight,
)
from app.prompts.spec import PromptSection, PromptSpec
from app.prompts.compiler import (
    CompiledPrompt,
    PromptCompileError,
    check_conflicts,
    compile_prompt,
    estimate_tokens,
    lint_spec,
    lint_text,
)
from app.prompts.registry import (
    all_specs,
    deprecation_violations,
    get,
    manifest,
    register,
    require,
    unused_prompts,
)

# Importing the catalog registers every production prompt id (idempotent).
from app.prompts import catalog as _catalog  # noqa: E402,F401

__all__ = [
    "CROSS_TURN_CITATION_NOTE", "MEMORY_INSTRUCTION_BOUNDARY",
    "MEMORY_RELEVANCE_BOUNDARY", "MEMORY_BOUNDARY_SECTIONS",
    "TOOL_AVAILABILITY_NOTE", "UNTRUSTED_DATA_PREAMBLE", "spotlight",
    "PromptSection", "PromptSpec",
    "CompiledPrompt", "PromptCompileError", "check_conflicts", "compile_prompt",
    "estimate_tokens", "lint_spec", "lint_text",
    "all_specs", "deprecation_violations", "get", "manifest", "register",
    "require", "unused_prompts",
]

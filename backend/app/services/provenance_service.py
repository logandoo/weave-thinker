# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P1 S1: provenance-lite markers for derived context.

This is not a full IFC/TMA authority system.  It attaches machine-readable
origin/trust metadata to derived blocks (resume packs/summaries) so prompts,
telemetry and future authority checks can distinguish:
  - user/tool-derived text (untrusted data)
  - system/registry text (trusted instructions)
The marker is structural, not a keyword classifier.
"""
from __future__ import annotations

import re
from html import escape


def provenance_marker(origin: str, *, trust: str = "untrusted",
                      derived_from: str = "") -> str:
    attrs = [f'origin="{escape(str(origin), quote=True)}"',
             f'trust="{escape(str(trust), quote=True)}"']
    if derived_from:
        attrs.append(f'derived_from="{escape(str(derived_from), quote=True)}"')
    return "<provenance " + " ".join(attrs) + ">"


def neutralize_markup(text: str) -> str:
    """Neutralize tag-like tokens that could close/spoof our wrapper."""
    return re.sub(r"</?(?:provenance|context_continuity)\b", "＜provenance", str(text), flags=re.IGNORECASE)


def tag_derived(text: str, origin: str, *, derived_from: str = "") -> str:
    if not text:
        return text
    return (
        provenance_marker(origin, derived_from=derived_from)
        + "\n" + neutralize_markup(text)
        + "\n</provenance>"
    )

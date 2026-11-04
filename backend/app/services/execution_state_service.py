# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""S1 (2026-10-18, arXiv:2606.06090 MAGE): execution-state tree, v1.

Scope is deliberately narrow: the deathmatch plan is and remains the single
source of truth for step status. This service adds the missing MAGE operations
around it — a raw trace node, a compressed subgoal summary, a maintenance check,
and a *branch* that isolates a failed subtree. It is default-off and never
writes plan status.

For normal chat v1 is not enabled; the 40-message continuity service handles
that surface instead (avoiding two competing ledgers).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ACTIVE = "active"
DONE = "done"
FAILED = "failed"
ABANDONED = "abandoned"


@dataclass
class StateNode:
    id: str
    conversation_id: str
    kind: str  # trace | summary | branch
    parent_id: str = ""
    branch_id: str = "main"
    step_id: str = ""
    status: str = ACTIVE
    summary: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    failure_reason: str = ""
    error_class: str = ""
    created_at: float = field(default_factory=time.time)


def render_plan_hints(plan: Optional[Dict[str, Any]], budget_chars: int = 800) -> str:
    """S1 integration: expose failed/retry branches without duplicating status."""
    steps = list((plan or {}).get("steps") or []) if isinstance(plan, dict) else []
    lines: List[str] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        status = str(s.get("status") or "")
        recovery = str(s.get("recovery") or "idle")
        reason = str(s.get("failure_reason") or s.get("boundary") or "")
        if status in ("failed", "blocked", "abandoned") or recovery not in ("idle", ""):
            lines.append(
                f"- {str(s.get('id') or '-')}: status={status or '?'} recovery={recovery}"
                + (f" | {reason[:200]}" if reason else "")
            )
    if not lines:
        return ""
    return (
        "<execution_state_hints>\n"
        "失败/重试分支提示（不要重复已失败路径；计划状态仍是唯一权威）:\n"
        + "\n".join(lines[:6])
        + "\n</execution_state_hints>"
    )[:budget_chars]


class ExecutionStateTree:
    """Minimal in-memory tree; DB persistence is caller-owned (v1)."""

    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self.nodes: Dict[str, StateNode] = {}
        self.active_branch = "main"

    def grow(self, *, kind: str = "trace", step_id: str = "", summary: str = "",
             evidence_refs: Optional[List[str]] = None, parent_id: str = "",
             branch_id: Optional[str] = None) -> StateNode:
        node = StateNode(
            id=str(uuid.uuid4()),
            conversation_id=self.conversation_id,
            kind=kind,
            parent_id=parent_id,
            branch_id=branch_id or self.active_branch,
            step_id=step_id,
            summary=str(summary or "")[:800],
            evidence_refs=list(evidence_refs or [])[:20],
        )
        self.nodes[node.id] = node
        return node

    def compress(self, node_ids: List[str], *, step_id: str = "",
                 summary: str = "") -> Optional[StateNode]:
        children = [self.nodes[n] for n in node_ids if n in self.nodes]
        if not children:
            return None
        # A summary node is attached to the branch of its first child; children
        # stay in the tree (revise can still inspect them) but are no longer on
        # the active path.
        node = self.grow(
            kind="summary", step_id=step_id or children[0].step_id,
            summary=summary, parent_id=children[0].parent_id,
            branch_id=children[0].branch_id,
            evidence_refs=[n.id for n in children],
        )
        for child in children:
            child.status = DONE
        return node

    def maintain(self, node_id: str, *, missing: Optional[List[str]] = None) -> Dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None:
            return {"ok": False, "reason": "node_missing"}
        missing = [str(x)[:160] for x in (missing or []) if str(x).strip()]
        if missing:
            node.status = FAILED
            node.failure_reason = "missing: " + "; ".join(missing[:6])
            return {"ok": False, "missing": missing[:6]}
        node.status = DONE
        return {"ok": True, "missing": []}

    def revise(self, boundary_id: str, *, reason: str = "",
               error_class: str = "") -> Optional[StateNode]:
        boundary = self.nodes.get(boundary_id)
        if boundary is None:
            return None
        # Mark the failed subtree abandoned (children by active branch prefix).
        branch = boundary.branch_id
        for node in self.nodes.values():
            if node.branch_id == branch and node.created_at >= boundary.created_at:
                node.status = ABANDONED
        new_branch = f"branch-{uuid.uuid4().hex[:8]}"
        marker = self.grow(
            kind="branch", step_id=boundary.step_id,
            summary=str(reason or "revise")[:400],
            parent_id=boundary.parent_id,
            branch_id=new_branch,
            evidence_refs=[boundary.id],
        )
        marker.failure_reason = str(reason or "")[:400]
        marker.error_class = str(error_class or "")[:60]
        self.active_branch = new_branch
        return marker

    def active_path(self) -> List[StateNode]:
        """Summary nodes + trace nodes on the active branch, oldest first."""
        path = [
            n for n in self.nodes.values()
            if n.branch_id == self.active_branch and n.status in (ACTIVE, DONE)
        ]
        path.sort(key=lambda n: n.created_at)
        return path

    def render(self, budget_chars: int = 2000) -> str:
        lines = ["<execution_state>"]
        for node in self.active_path():
            if node.kind == "summary":
                lines.append(f"- [完成] {node.step_id or '-'}: {node.summary[:300]}")
            elif node.kind == "branch":
                lines.append(f"- [分支] {node.step_id or '-'}: {node.summary[:200]}")
        abandoned = [
            n for n in self.nodes.values()
            if n.kind == "branch" and n.failure_reason
        ]
        if abandoned:
            lines.append("失败分支提示（不要重复）:")
            for n in abandoned[-3:]:
                lines.append(
                    f"- {n.step_id or '-'}: {n.summary[:160]}"
                    + (f" ({n.error_class})" if n.error_class else "")
                )
        lines.append("</execution_state>")
        return "\n".join(lines)[:budget_chars]

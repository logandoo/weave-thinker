# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Historical rounds backfill (conv a3cfb421 / 42910da6, 2026-08-09).

Pre-fix chat.py stamped EVERY persisted round with the LAST web_search call's
queries (a single ``search_queries_used`` variable overwritten per tool_call
event). Fix edd0c3b/e765fe5 only affects NEW messages — existing rows keep
the wrong rounds. This module rewrites them in place from the persisted
``tool_calls`` JSON, which carries each call's real id + queries.

Alignment: the i-th web_search tool_call (in persisted order) corresponds to
the i-th round. Rounds beyond the call count are left untouched (a search
with no hits never appends a round). Everything is conservative — mismatched
inputs return the object unchanged.
"""
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _iter_ws_calls(tool_calls_json: Optional[str]):
    """Yield (call_id, queries) for each web_search tool_call in order."""
    if not tool_calls_json:
        return
    try:
        calls = json.loads(tool_calls_json)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(calls, list):
        return
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if fn.get("name") != "web_search":
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        queries = args.get("queries")
        if isinstance(queries, list):
            yield tc.get("id") or "", [str(q) for q in queries]


def backfill_rounds_queries(tool_results_obj: dict, tool_calls_json: Optional[str]) -> dict:
    """Rewrite each round's queries from the persisted tool_calls, in order.

    Returns the same dict object (mutated in place) for the no-op cases.
    """
    if not isinstance(tool_results_obj, dict):
        return tool_results_obj
    rounds = tool_results_obj.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        return tool_results_obj

    ws_calls = list(_iter_ws_calls(tool_calls_json))
    if not ws_calls:
        return tool_results_obj

    # Conservative alignment guard: rounds only exist for searches that
    # returned hits (a failed/empty search never appends a round), so when
    # the call count exceeds the round count the positional mapping would be
    # wrong (backfill scan found rounds=2/ws=3 etc.). Only rewrite when the
    # counts match exactly; mismatches are left untouched.
    if len(ws_calls) != len(rounds):
        logger.info(
            "Rounds backfill: skip msg — ws_calls=%d rounds=%d (count mismatch)",
            len(ws_calls), len(rounds),
        )
        return tool_results_obj

    replaced = 0
    for i, round_item in enumerate(rounds):
        _cid, queries = ws_calls[i]
        if isinstance(round_item, dict):
            round_item["queries"] = list(queries)
            replaced += 1
    if replaced:
        logger.info("Rounds backfill: rewrote %d/%d rounds from tool_calls", replaced, len(rounds))
    return tool_results_obj

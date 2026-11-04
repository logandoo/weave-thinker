# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent-loop coordinator mixin (P5.2 move-only, 2026-10-18)."""
from __future__ import annotations

import json
import logging
import re as _re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _coordinator_recent_messages(*a, **k):
    from app.services.agent_loop import _coordinator_recent_messages as f
    return f(*a, **k)


class CoordinatorMixin:
    async def _coordinate(
        self,
        messages: List[Dict[str, Any]],
    ) -> Optional[dict]:
        """Classify the user's latest message and optionally generate a direct
        reply.  Returns ``{"route": "direct_reply", "response": "..."}`` when
        the message can be answered without tools, ``{"route": "tool_loop",
        "response": ""}`` when tools are needed, or ``None`` when coordination
        should be skipped entirely (deathmatch, skill execution, errors).

        All intent judgment (routing, whether tools are needed, what the user
        actually wants this turn) is done by the coordinator LLM — no regex
        pre-classifiers. The LLM generalizes to arbitrary phrasings; regexes
        only cover the patterns someone happened to enumerate (user feedback
        2026-07-20: "无限多可能性的垃圾实现").
        """
        if self.deathmatch_manager is not None:
            return None

        last_user_msg = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break
        if not last_user_msg:
            return None

        stripped = last_user_msg.strip()
        if stripped.startswith("[skill:") or stripped.startswith("[使用技能:"):
            return None

        # Build compact context: coordinator system prompt + last 5 conversation
        # messages (skip the full agent system prompt to save tokens).
        # Only user/assistant text messages are forwarded — tool-role messages
        # require tool_call_id and assistant messages with tool_calls need the
        # matching tool responses; sending either verbatim makes strict APIs
        # (DeepSeek) reject the call with HTTP 400, silently disabling the
        # coordinator for the whole conversation (conv f75236d2, 2026-07-20).
        # conv a104fbc5（2026-09-21，用户指令）：原「前 800 字符」head-only
        # 使协调器看不到长粘贴消息尾部的诉求，focus 误读为「请审阅/补全表格」
        # ——改为逐字全文（超预算丢最旧整条），契约见
        # `_coordinator_recent_messages`。
        recent_msgs: List[Dict[str, Any]] = _coordinator_recent_messages(messages)

        identity_clause = ""
        if self._identity_context:
            identity_clause = (
                "如果下方提供了用户为你设置的身份/昵称信息，优先使用那个名称；"
                "没有时就以'Weave Thinker'自称。严禁编造不存在的名称。\n"
            )
        else:
            identity_clause = "没有用户自定义名称时就以'Weave Thinker'自称。严禁编造不存在的名称。\n"

        system_prompt = self._COORDINATOR_CLASSIFY_TEMPLATE.format(
            identity_clause=identity_clause
        )
        if self._identity_context:
            system_prompt += "\n\n用户记忆提供的身份/昵称信息（回答身份问题时参考）：\n" + self._identity_context

        coord_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ] + recent_msgs

        self._last_coord_messages = coord_messages

        raw = ""
        for _attempt in range(2):
            try:
                raw = await self._complete_json(
                    coord_messages, temperature=0.3
                )
                logger.warning("[SETUP-TRACE] coord _complete_json done len=%s attempt=%d", len(raw or ""), _attempt)
            except Exception:
                logger.warning("[SETUP-TRACE] coord _complete_json exc attempt=%d", _attempt)
                logger.exception("Coordinator LLM call failed, falling through to tool_loop")
                return None
            raw = (raw or "").strip()
            if raw:
                # Non-empty: check whether it is usable routing JSON. A plain
                # markdown answer (topic drift, e.g. conv 149ce886 2026-08-02:
                # coordinator answered the PREVIOUS question with [3]-citations
                # instead of routing) is NOT usable — retry once with a format
                # reminder instead of dropping the routing decision entirely.
                # Schema-validated: parseable JSON with a valid route field
                # counts as a routing decision; a drift answer that merely
                # EMBEDS some JSON literal (e.g. quoting an API response) does
                # NOT — it must also retry (review finding 2026-08-02).
                _js = raw.find("{")
                _je = raw.rfind("}") + 1
                _usable_routing = False
                if _js >= 0 and _je > _js:
                    try:
                        _probe = json.loads(raw[_js:_je])
                        if isinstance(_probe, dict):
                            _r = _probe.get("route")
                            _usable_routing = isinstance(_r, str) and _r in ("direct_reply", "tool_loop")
                    except json.JSONDecodeError:
                        pass
                if _usable_routing:
                    break
                if _attempt == 0:
                    coord_messages = coord_messages + [{
                        "role": "user",
                        "content": "提醒：你是路由器，不是在回答用户。只输出JSON，不要输出任何其他内容。",
                    }]
                    logger.warning("Coordinator returned non-JSON, retrying with format reminder: %s", raw[:80])
                    continue
                break
            # Empty completion — retry once with an explicit format reminder
            # (transient reasoning-budget exhaustion must not silently disable
            # the coordinator, conv 3bc79c4c 11:33).
            if _attempt == 0:
                coord_messages = coord_messages + [{
                    "role": "user",
                    "content": "提醒：你是路由器，不是在回答用户。只输出JSON，不要输出任何其他内容。",
                }]
                logger.info("Coordinator returned empty, retrying with format reminder")
        if not raw:
            return None

        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()

        # Extract JSON object from the response.
        result: Optional[Dict[str, Any]] = None
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                result = json.loads(raw[json_start:json_end])
            except json.JSONDecodeError:
                result = None
        if not isinstance(result, dict):
            # Salvage: the model answered the user DIRECTLY instead of routing
            # (conv 3bc79c4c, 2026-07-20 — coordinator produced a correct answer
            # to "你觉得你和其他 agent 最大的区别是什么" but no JSON; it was
            # discarded, the main agent ran unfocused and drifted back to the
            # stale eval-methods topic). A conversational direct answer is a
            # usable direct_reply — far better than no coordination at all.
            #
            # BUT never salvage output that looks like a (truncated) routing
            # object: a JSON fragment is a FAILED routing decision, not a
            # conversational answer (conv 1c959c11, 2026-07-24 — coordinator
            # retry returned 43 chars of truncated JSON after reasoning-budget
            # exhaustion; the fragment was salvaged as direct_reply and saved
            # as the user-visible reply).
            salvaged = raw.strip()
            looks_like_route_fragment = (
                salvaged.startswith("{")
                or salvaged.startswith("[")
                or '"route"' in salvaged[:120]
            )
            if (
                salvaged
                and len(salvaged) <= 500
                and not looks_like_route_fragment
                and not self._LEAK_PATTERNS.search(salvaged)
            ):
                logger.info("Coordinator answered directly (non-JSON), using as direct_reply")
                return {
                    "route": "direct_reply",
                    "response": salvaged,
                    "expects_tools": False,
                    "search_required": False,
                    "creative_turn": False,
                    # 纯对话直答轮=非工程轮（显式提及在调用方 OR 兜底）
                    "coding_task": False,
                    "focus": "",
                }
            logger.warning("Coordinator returned non-JSON: %s", raw[:200])
            # Truncated routing JSON (max_tokens cut the focus mid-word, e.g.
            # conv 149ce886 19:41: the JSON was valid until "…最新信息，exp").
            # A truncated routing object is still a routing DECISION — salvage
            # route + expects_tools so the fabrication guard and focus survive
            # instead of degrading to an unfocused run.
            if raw.strip().startswith("{") and '"route"' in raw[:120]:
                _route_m = _re.search(r'"route"\s*:\s*"([a-z_]+)"', raw)
                _exp_m = _re.search(r'"expects_tools"\s*:\s*(true|false)', raw)
                _search_m = _re.search(r'"search_required"\s*:\s*(true|false)', raw)
                _creative_m = _re.search(r'"creative_turn"\s*:\s*(true|false)', raw)
                _coding_m = _re.search(r'"coding_task"\s*:\s*(true|false)', raw)
                if _route_m:
                    logger.warning(
                        "Salvaging truncated coordinator JSON: route=%s expects_tools=%s search_required=%s",
                        _route_m.group(1), _exp_m.group(1) if _exp_m else None,
                        _search_m.group(1) if _search_m else None,
                    )
                    return {
                        "route": _route_m.group(1),
                        "response": "",
                        # tool_loop routing implies tools may be needed;
                        # default True so the fabrication guards stay
                        # armed even when truncation cut the flag.
                        "expects_tools": (
                            _exp_m.group(1) == "true"
                            if _exp_m
                            else (_route_m.group(1) == "tool_loop")
                        ),
                        "search_required": (
                            _search_m.group(1) == "true" if _search_m else None
                        ),
                        "creative_turn": (
                            _creative_m.group(1) == "true" if _creative_m else False
                        ),
                        # 截断可能吃掉本字段——缺省 None 让轮次门控回退启发式
                        "coding_task": (
                            _coding_m.group(1) == "true" if _coding_m else None
                        ),
                        "focus": "",
                    }
            return None

        try:
            from app.prompts.contracts import CoordinatorDecision, parse_contract

            _cd, _cd_failure = parse_contract(
                CoordinatorDecision, json.dumps(result, ensure_ascii=False))
            if _cd_failure is not None:
                logger.warning("coordinator contract failed: %s", _cd_failure.kind)
        except Exception:
            logger.debug("coordinator contract check skipped", exc_info=True)
        route = result.get("route", "tool_loop")
        expects_tools = result.get("expects_tools")
        if not isinstance(expects_tools, bool):
            expects_tools = None
        search_required = result.get("search_required")
        if not isinstance(search_required, bool):
            search_required = None
        creative_turn = result.get("creative_turn")
        if not isinstance(creative_turn, bool):
            creative_turn = False
        # W17 轮次门控主判据：三态（True/False/None）。None=未给出结论
        # （旧模板/怪值），调用方回退 vibeweaver_turn_gate 启发式。
        coding_task = result.get("coding_task")
        if not isinstance(coding_task, bool):
            coding_task = None
        focus = str(result.get("focus", "") or "").strip()[:600]

        logger.info(
            "Coordinator route=%s expects_tools=%s search_required=%s creative_turn=%s coding_task=%s focus=%s",
            route, expects_tools, search_required, creative_turn, coding_task, focus[:80],
        )
        payload = {
            "route": route,
            "response": "",
            "expects_tools": expects_tools,
            "search_required": search_required,
            "creative_turn": creative_turn,
            "coding_task": coding_task,
            "focus": focus,
        }
        return payload

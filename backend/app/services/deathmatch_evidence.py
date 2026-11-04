# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/deathmatch_service.py (C-03/C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``deathmatch_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = ('Any', 'Dict', 'ExecutionStateTree', 'List', 'Optional', '_consume', '_direction_family', '_main_provider_llm', '_sanitize_research_field', '_time', '_touch_json', '_truncate', 'asyncio', 'config', 'exc', 'factory', 'get_config', 'get_model_registry', 'logger', 'uuid', 'write_through_bg')

# Moved default value (was a DeathmatchManager class attribute); resolves at
# function-definition time, so it must live at module scope here.
_EVIDENCE_SEED_LIMIT = 3


def _sync_deathmatchevidence_globals() -> None:
    from app.services import deathmatch_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


def evidence_unit_taint(source: Any) -> str:
    """W1-3（记忆/长程 v2，2026-10-23）：证据单元派生 taint——已校验来源
    （verifier/judge）最严 agent；其余（工具输出/未知）= untrusted（B3）。"""
    src = str(source or "").strip().lower()
    return "agent" if src in ("verifier", "judge") else "untrusted"


class DeathmatchEvidenceMixin:
    def set_assistant_llm(self, llm: Any) -> None:
        """P0: route unconfigured judge/verifier calls through the assistant's
        model client (chat.py sets this per request/turn)."""
        _sync_deathmatchevidence_globals()
        self._assistant_llm = llm
    def _make_llm(self, *, model_override: str = "", fallback: bool = False) -> "LLMService":
        _sync_deathmatchevidence_globals()
        from app.model_gateway import factory
        from app.model_gateway.registry import get_model_registry
        registry = get_model_registry()

        def _main_provider_llm() -> "LLMService":
            # A4 重试目标恒为 [api] 主端点（is_custom=True 语义），与 judge
            # 端点配置无关。model_gateway 收口（2026-08-30）。
            return factory.build_llm_service(
                registry.get("main").with_overrides(is_custom=True)
            )

        if fallback:
            # P0: the retry target is "the main provider" — for a
            # custom-model assistant that IS the assistant's client.
            _al = getattr(self, "_assistant_llm", None)
            if _al is not None:
                return _al
            return _main_provider_llm()
        _jd = config.deathmatch_judge or {}
        _explicit_url = _jd.get("base_url") or ""
        _explicit_model = model_override or _jd.get("model_name") or ""
        if _explicit_url or _explicit_model:
            # Explicit per-assistant/deathmatch configuration wins (P0 例外).
            ep = registry.resolve("deathmatch.judge")
            if _explicit_model and _explicit_model != ep.model_name:
                ep = ep.with_overrides(model_name=_explicit_model)
            return factory.build_llm_service(ep)
        # P1-6: dedicated judge routing in config_model.toml [routing."deathmatch.judge"]. When the
        # deathmatch.judge routing names a NON-main alias, it is an explicit
        # dedicated setting (P0 例外 — differential verification: the judge
        # must not share the candidate's model). Detection is by alias NAME,
        # not endpoint value: the deepseek endpoint may coincide with the
        # main endpoint's url/model while still being a deliberate routing
        # choice away from the ASSISTANT's model (the actual candidate).
        # alias=main / no routing entry → P0 inheritance below.
        try:
            _routing = getattr(registry, "_routing", {}) or {}
            _target = _routing.get("deathmatch.judge")
            _alias = (
                str(_target.get("alias") or "") if isinstance(_target, dict)
                else str(_target or "")
            )
            # A4.9 r3 M2: a typo'd alias (not "main", unknown to the registry)
            # would silently resolve to the main endpoint — treat that as a
            # misconfiguration (log + P0 inheritance), never a silent
            # same-source judge disguised as dedicated.
            if _alias and _alias != "main":
                if _alias in getattr(registry, "_endpoints", {}):
                    return factory.build_llm_service(registry.resolve("deathmatch.judge"))
                logger.warning(
                    "deathmatch.judge routing alias %r not in registry endpoints — "
                    "falling back to P0 assistant inheritance "
                    "(fix config_model.toml [routing.\"deathmatch.judge\"])",
                    _alias,
                )
        except Exception:
            pass
        _al = getattr(self, "_assistant_llm", None)
        if _al is not None:
            # P0: judge/verifier unconfigured -> assistant's model client.
            return _al
        return _main_provider_llm()
    async def _llm_generate_once(self, llm, system_prompt: str, user_prompt: str, *, temperature: float, timeout: float) -> str:
        _sync_deathmatchevidence_globals()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        out = ""
        try:
            stream = llm.stream_chat_structured(messages, temperature=temperature, tools=None, extra_body={})

            async def _consume() -> str:
                _out = ""
                async for event in stream:
                    if event["type"] == "content":
                        _out += event["data"]
                    elif event["type"] == "error":
                        break
                return _out

            out = await asyncio.wait_for(_consume(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("PEVR LLM generate timed out after %.1fs", timeout)
        except Exception as exc:
            logger.warning("PEVR LLM generate failed: %s", exc)
        return out
    @staticmethod
    def _same_provider(a, b) -> bool:
        """True when two LLMService instances resolve to the same provider
        and model — the A4 fallback is skipped in that case to avoid a
        pointless double call on the same (deterministically failing) target."""
        _sync_deathmatchevidence_globals()
        try:
            from app.core.config import get_config
            cfg = get_config()
            a_model = (a.custom_model_name or cfg.model_name or "deepseek-v4-flash")
            b_model = (b.custom_model_name or cfg.model_name or "deepseek-v4-flash")
            a_url = getattr(getattr(a, "client", None), "base_url", None) or cfg.api_base_url
            b_url = getattr(getattr(b, "client", None), "base_url", None) or cfg.api_base_url
            return a_model == b_model and str(a_url) == str(b_url)
        except Exception:
            return False
    async def _llm_generate(self, llm, system_prompt: str, user_prompt: str, *, temperature: float = 0.2, timeout: float = 120.0) -> str:
        _sync_deathmatchevidence_globals()
        out = await self._llm_generate_once(llm, system_prompt, user_prompt, temperature=temperature, timeout=timeout)
        if not out.strip():
            # A4: retry once via the main provider before failing open —
            # a down/misconfigured judge/aux model must not silently degrade
            # the goal loop across turns (was: empty → caller treated as
            # partial/continue with no recovery). Skipped when the primary
            # already targets the main provider (same model+base_url), and
            # the retry uses a reduced timeout so the outer 300s judge
            # budget cannot be exceeded by the fallback (A4.9 Imp-4).
            try:
                fb_llm = self._make_llm(fallback=True)
                if not self._same_provider(llm, fb_llm):
                    fb_timeout = max(15.0, timeout / 2)
                    logger.info(
                        "deathmatch LLM generate empty/failed — retrying via main provider "
                        "(A4 fallback, timeout %.0fs)", fb_timeout,
                    )
                    out = await self._llm_generate_once(
                        fb_llm, system_prompt, user_prompt,
                        temperature=temperature, timeout=fb_timeout,
                    )
            except Exception as exc:
                logger.warning("deathmatch fallback LLM retry failed: %s", exc)
        return out
    def _record_evidence_unit(
        self,
        kind: str,
        source: str,
        text: str,
        *,
        step_id: str = "",
        attributed: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """W2 D1（ACG §3.1-3.3）：证据单元写点（与 _record_event 同款 fail-open
        + write-through）。

        单元形状 {id, kind, source, step_id, text(截 300), ts, attributed}。
        归因只认已校验来源声明：显式 step_id 即归属该需求（步骤）；缺失/歧义
        （attributed=False 或无 step_id）保持未归因——不用语义相似补。
        cap 100 保留最新。开关关闭时归档照记（归档不注入，测试钉死）。
        """
        _sync_deathmatchevidence_globals()
        try:
            sid = _truncate(str(step_id or ""), 60)
            if attributed is None:
                attr = bool(sid)
            else:
                # 歧义显式声明 → 保持未归因；无 step_id 也永不归因。
                attr = bool(attributed) and bool(sid)
            kind_norm = str(kind or "").upper()
            if kind_norm not in self._EVIDENCE_UNIT_KINDS:
                kind_norm = "OBSERVED"
            # fix-round Minors：未知来源标 "unknown"（不得默认冒充 judge）。
            _src_raw = str(source or "").strip().lower()
            _src_norm = _src_raw if _src_raw in ("verifier", "judge") else "unknown"
            unit: Dict[str, Any] = {
                "id": f"eu-{uuid.uuid4().hex[:12]}",
                "kind": kind_norm,
                "source": _src_norm,
                # W1-3：证据单元 = 派生记忆，taint 继承最严（工具输出/未知
                # = untrusted；verifier/judge = agent）
                "taint": evidence_unit_taint(_src_norm),
                "step_id": sid,
                "text": _truncate(str(text or ""), self._EVIDENCE_UNIT_TEXT_CHARS),
                "ts": _time.time(),
                "attributed": attr,
            }
            units = getattr(self._conv, "deathmatch_evidence_units", None)
            units = list(units) if isinstance(units, list) else []
            units.append(unit)
            self._conv.deathmatch_evidence_units = units[-self._EVIDENCE_UNITS_CAP:]
            from app.services.deathmatch_write_through import write_through_bg
            write_through_bg(
                self._conv,
                deathmatch_evidence_units=self._conv.deathmatch_evidence_units,
            )
            return unit
        except Exception as exc:
            logger.debug("evidence unit record failed: %s", exc)
            return None
    def _seed_evidence_units_round_robin(
        self, chain_source: str, limit: int = _EVIDENCE_SEED_LIMIT
    ) -> List[Dict[str, Any]]:
        """W2 B2（ACG 种子 round-robin）：对侧链已归档单元以轮转抽样 ≤limit 条
        交叉注入本链块——同一轮内 judge 与 verifier 两链各得交错种子。

        纯确定性：轮转偏移 = 轮次计数 % 候选数（无随机、无 LLM）。
        """
        _sync_deathmatchevidence_globals()
        try:
            other = "verifier" if str(chain_source) == "judge" else "judge"
            pool = [
                u for u in (getattr(self._conv, "deathmatch_evidence_units", None) or [])
                if isinstance(u, dict) and str(u.get("source") or "") == other
            ]
            if not pool or limit <= 0:
                return []
            offset = int(getattr(self._conv, "deathmatch_turns", 0) or 0) % len(pool)
            return [pool[(offset + i) % len(pool)] for i in range(min(limit, len(pool)))]
        except Exception as exc:
            logger.debug("evidence seed round-robin failed: %s", exc)
            return []
    def _build_evidence_units_block(
        self, chain_source: str = "judge", current_step_id: str = ""
    ) -> str:
        """W2 D1（ACG §3.4-3.5）：需求锚定证据单元块渲染（确定性，无 LLM）。

        - 需求锚定：按当前 step_id 分组（当前步 → 其他步 → 未归因）。
        - 来源标注；歧义/无归属标「未归因」（不用语义相似补归属）。
        - B2：对侧链单元轮转抽样 ≤3 条交叉注入（交错种子）。
        - 块级预算准入：块总长 > deathmatch_evidence_units_budget 时整块弃换
          1 行省略提示（不半截截断）。开关关闭 → 注入为空（归档不注入）。
        """
        _sync_deathmatchevidence_globals()
        if not config.deathmatch_evidence_units_enabled:
            return ""
        try:
            units = [
                u for u in (getattr(self._conv, "deathmatch_evidence_units", None) or [])
                if isinstance(u, dict)
            ]
            own = [u for u in units if str(u.get("source") or "") == str(chain_source)]
            merged: Dict[str, Dict[str, Any]] = {
                str(u.get("id") or i): u for i, u in enumerate(own)
            }
            for s in self._seed_evidence_units_round_robin(str(chain_source)):
                merged.setdefault(str(s.get("id") or ""), s)
            items = sorted(merged.values(), key=lambda u: float(u.get("ts") or 0.0))
            if not items:
                return ""
            if not current_step_id:
                for s in ((getattr(self._conv, "deathmatch_plan", None) or {}).get("steps") or []):
                    if str(s.get("status") or "") == "in_progress":
                        current_step_id = str(s.get("id") or "")
                        break
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for u in items:
                # 归因只认已校验声明：attributed=False/无 step_id → 未归因组。
                key = str(u.get("step_id") or "") if u.get("attributed") else ""
                groups.setdefault(key, []).append(u)
            order: List[str] = []
            if current_step_id and current_step_id in groups:
                order.append(current_step_id)
            order += [k for k in groups if k and k not in order]
            if "" in groups:
                order.append("")
            lines: List[str] = ["<evidence_units>"]
            for key in order:
                if key == "":
                    lines.append("未归因:")
                elif key == current_step_id:
                    lines.append(f"步骤 {key}（当前）:")
                else:
                    lines.append(f"步骤 {key}:")
                for u in groups[key]:
                    lines.append(
                        f"- [{str(u.get('kind') or 'OBSERVED')}"
                        f"|{str(u.get('source') or '?')}] {str(u.get('text') or '')}"
                    )
            lines.append("</evidence_units>")
            block = "\n".join(lines)
            # fix-round Minors：显式预算读取（`or 800` 会把合法 0 卷回 800——
            # 0=无预算，缺省/None→800 由 config 属性 is None 判断兜底）。
            budget = int(config.deathmatch_evidence_units_budget)
            if budget > 0 and len(block) > budget:
                # 整块入或整体弃（ACG §3.5）：超预算不半截截断。
                return (
                    "<evidence_units>（证据单元块超出预算 "
                    f"{budget} 字符，共 {len(items)} 条——整块省略）</evidence_units>"
                )
            return block
        except Exception as exc:
            logger.debug("evidence units block build failed: %s", exc)
            return ""
    def _record_event(self, event_type: str, **fields: Any) -> None:
        """W2c: append-only capped decision/event log (never raises)."""
        _sync_deathmatchevidence_globals()
        try:
            events = getattr(self._conv, "deathmatch_events", None) or []
            if not isinstance(events, list):
                events = []
            entry: Dict[str, Any] = {
                "type": str(event_type)[:60],
                "ts": _time.time(),
                "turn": self._conv.deathmatch_turns or 0,
            }
            for k, v in fields.items():
                key = str(k)[:30]
                if isinstance(v, str):
                    entry[key] = _truncate(v, 300)
                elif isinstance(v, (int, float, bool)) or v is None:
                    entry[key] = v
                elif isinstance(v, list):
                    entry[key] = [str(x)[:200] for x in v[:10]]
                else:
                    entry[key] = _truncate(str(v), 300)
            events.append(entry)
            cap = max(1, int(config.deathmatch_events_cap or 50))
            self._conv.deathmatch_events = events[-cap:]
            from app.services.deathmatch_write_through import write_through_bg
            write_through_bg(self._conv, deathmatch_events=self._conv.deathmatch_events)
        except Exception as exc:
            logger.debug("event record failed: %s", exc)
    def _record_step_transition(
        self, step_id: str, from_status: str, to_status: str, cause: str = ""
    ) -> None:
        """R5：步骤状态迁移结构化观测（step_id, from, to, cause）。"""
        _sync_deathmatchevidence_globals()
        self._record_event(
            "step_transition",
            step_id=str(step_id or ""),
            **{
                "from": str(from_status or ""),
                "to": str(to_status or ""),
                "cause": str(cause or ""),
            },
        )
    def _execution_state_or_none(self):
        _sync_deathmatchevidence_globals()
        if not bool(config.deathmatch.get("execution_state_enabled", False)):
            return None
        if self._execution_state is None:
            try:
                from app.services.execution_state_service import ExecutionStateTree
                self._execution_state = ExecutionStateTree(str(self._conv.id))
            except Exception:
                logger.debug("execution state init failed", exc_info=True)
                return None
        return self._execution_state
    def _set_step_status(
        self, step: Dict[str, Any], to_status: str, cause: str = "",
        force_event: bool = False,
    ) -> None:
        """R3+R5 单接缝：步骤状态迁移 = 写状态 + 迁移事件 + plan 列触脏。

        所有 ``step["status"] = ...`` 写点必须走本方法（诊断 2026-10-13
        机制 B：9 处裸原地赋值零脏标记，仅每-verdict merge 一条落库通道）。
        I3（评审）：真迁移同步推进 deathmatch_plan_version——前端以版本号
        判定 plan 新鲜度，不推进则「plan_version 更新时重播种」在生产链路
        永不触发。I1/B-1（评审）：stuck 步被重新拾起（pending→in_progress）
        即清零 gate_blocks 给足重试预算（D-12），任何拾起点统一生效。
        fix-round I2：force_event=True 时同态（old==to）也记迁移事件
        （local_patch 替换 pending→pending 不得缺面——A-N2）；版本推进/
        写-through 只随真迁移。"""
        _sync_deathmatchevidence_globals()
        old = str(step.get("status") or "pending")
        step["status"] = to_status
        if str(to_status) == "done":
            # 完成即解除可见卡住标记（R2：stuck 是过程态，不是终态属性）。
            step.pop("stuck", None)
        if str(to_status) == "in_progress" and old != "in_progress" and step.get("stuck"):
            step["gate_blocks"] = 0
        if old == str(to_status) and not force_event:
            return
        self._record_step_transition(str(step.get("id") or ""), old, str(to_status), cause)
        if old == str(to_status):
            return  # 同态 force_event：只补迁移事件（版本/写-through 随真迁移）
        # S1 (2026-10-18): derived execution-state updates. Fail-open; never
        # changes plan status or blocks the transition.
        try:
            _tree = self._execution_state_or_none()
            _sid = str(step.get("id") or "")
            if _tree is not None and _sid:
                if str(to_status) == "in_progress":
                    node = _tree.grow(
                        kind="trace", step_id=_sid,
                        summary=str(step.get("description") or "")[:300],
                    )
                    self._est_nodes.setdefault(_sid, []).append(node.id)
                elif str(to_status) == "done":
                    ids = self._est_nodes.pop(_sid, [])
                    if not ids:
                        ids = [_tree.grow(
                            kind="trace", step_id=_sid,
                            summary=str(step.get("description") or "")[:300],
                        ).id]
                    summary_node = _tree.compress(
                        ids, step_id=_sid,
                        summary=str(step.get("output_summary") or step.get("description") or "")[:600],
                    )
                    if summary_node is not None:
                        _tree.maintain(summary_node.id)
                elif old == "done" and str(to_status) == "pending":
                    ids = self._est_nodes.get(_sid) or []
                    boundary = ids[-1] if ids else _tree.grow(
                        kind="trace", step_id=_sid,
                        summary=str(step.get("description") or "")[:300],
                    ).id
                    _tree.revise(boundary, reason=str(cause or "step reopened")[:300])
        except Exception:
            logger.debug("execution state hook failed (fail-open)", exc_info=True)
        _previous_version = 0
        try:
            _previous_version = int(getattr(self._conv, "deathmatch_plan_version", 0) or 0)
            self._conv.deathmatch_plan_version = _previous_version + 1
        except Exception as exc:
            logger.debug("plan_version bump failed: %s", exc)
        _touch_json(self._conv, "deathmatch_plan")
        # B-8（D-15）：数据列写-through——步骤状态不依赖 merge 通道。
        from app.services.deathmatch_write_through import write_through_bg
        write_through_bg(
            self._conv,
            _expected_previous_plan_version=_previous_version,
            deathmatch_plan=getattr(self._conv, "deathmatch_plan", None),
            deathmatch_plan_version=getattr(self._conv, "deathmatch_plan_version", 0),
        )
    def _write_pause_packet(
        self,
        *,
        gate: str,
        question: str,
        options: Optional[List[Any]] = None,
        default_if_continue: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> None:
        """W2c: persist the PAUSED resume packet for every stop (vibeweaver
        PAUSED protocol): gate / question / options / default / state."""
        _sync_deathmatchevidence_globals()
        try:
            self._conv.deathmatch_pause_state = {
                "gate": str(gate)[:120],
                "question": str(question)[:400],
                "options": [str(o)[:200] for o in (options or [])][:3],
                "default_if_continue": str(default_if_continue)[:200],
                "state": state or {
                    "status": self._conv.deathmatch_status,
                    "turn": self._conv.deathmatch_turns or 0,
                    "plan_version": self._conv.deathmatch_plan_version or 0,
                },
                "ts": _time.time(),
            }
            self._record_event(
                "pause_packet", gate=gate, default=default_if_continue,
            )
        except Exception as exc:
            logger.debug("pause packet write failed: %s", exc)
    def _failed_directions(self) -> List[Dict[str, Any]]:
        _sync_deathmatchevidence_globals()
        raw = getattr(self._conv, "deathmatch_failed_directions", None) or []
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    def _record_failed_direction(self, reason: str, family: str = "") -> Dict[str, Any]:
        """W2b: record a failed direction (family-keyed); at/after the
        configured threshold it becomes forbidden for this goal."""
        _sync_deathmatchevidence_globals()
        fam = (family or _direction_family(reason))[:80]
        entries = self._failed_directions()
        hit: Optional[Dict[str, Any]] = None
        for e in entries:
            if str(e.get("family") or "") == fam:
                hit = e
                break
        if hit is None:
            hit = {
                "family": fam, "reason": str(reason)[:300],
                "count": 0, "forbidden": False,
            }
            entries.append(hit)
        hit["count"] = int(hit.get("count") or 0) + 1
        hit["reason"] = str(reason)[:300]
        hit["ts"] = _time.time()
        if hit["count"] >= max(1, int(config.deathmatch_failed_direction_forbidden_threshold or 3)):
            hit["forbidden"] = True
        self._conv.deathmatch_failed_directions = entries[-self._FAILED_DIRECTIONS_CAP:]
        self._record_event(
            "failed_direction", family=fam, count=hit["count"], forbidden=hit["forbidden"],
        )
        return hit
    def _failed_directions_block(self) -> str:
        _sync_deathmatchevidence_globals()
        items = [
            e for e in self._failed_directions()
            if int(e.get("count") or 0) > 0
        ]
        if not items:
            return ""
        lines = [
            "<failed_directions>",
            "已失败/被禁止的推进方向（禁止重复无新洞察的尝试；重试必须说明新洞察）：",
        ]
        for e in items[-8:]:
            mark = "⛔ 禁止" if e.get("forbidden") else f"❌ 已失败×{e.get('count')}"
            lines.append(f"- {mark} {str(e.get('reason') or e.get('family'))[:150]}")
        lines.append("</failed_directions>")
        return "\n".join(lines)
    def _retracted_claims(self) -> List[Dict[str, Any]]:
        _sync_deathmatchevidence_globals()
        raw = getattr(self._conv, "deathmatch_retracted_claims", None) or []
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    def _record_retracted_claim(
        self, summary: str, *, reason: str = "", source: str = "",
    ) -> Optional[Dict[str, Any]]:
        """记录一条被证伪的断言（去重、有界、never raises）。

        A4.9 I5：summary/reason 是 LLM 派生文本（分诊/仲裁/验证）——写入前
        压平控制符、剥尖括号（防 </retracted_claims> 标签逃逸）并截断。
        A4.9 M1：去重命中时把条目移到队尾，反复证伪的断言不会老化出
        渲染窗口（[-8:]）。
        """
        _sync_deathmatchevidence_globals()
        try:
            summary = _sanitize_research_field(summary, 200).replace("<", "").replace(">", "")
            reason = _sanitize_research_field(reason, 200).replace("<", "").replace(">", "")
            if not summary:
                return None
            entries = self._retracted_claims()
            for i, e in enumerate(entries):
                if str(e.get("summary") or "") == summary:
                    e["count"] = int(e.get("count") or 1) + 1
                    if reason:
                        e["reason"] = reason
                    e["ts"] = _time.time()
                    entries.append(entries.pop(i))  # 刷新到队尾（M1）
                    self._conv.deathmatch_retracted_claims = entries
                    return e
            entry = {
                "summary": summary,
                "reason": reason,
                "source": str(source or "")[:20],
                "count": 1,
                "turn": self._conv.deathmatch_turns or 0,
                "ts": _time.time(),
            }
            entries.append(entry)
            self._conv.deathmatch_retracted_claims = entries[-self._RETRACTED_CLAIMS_CAP:]
            self._record_event(
                "retracted_claim", summary=summary[:150], source=entry["source"],
            )
            return entry
        except Exception as exc:
            logger.debug("retracted claim record failed: %s", exc)
            return None
    def _retracted_claims_block(self) -> str:
        _sync_deathmatchevidence_globals()
        items = self._retracted_claims()
        if not items:
            return ""
        lines = [
            "<retracted_claims>",
            "以下断言/假设已被证伪（验证、实测或仲裁推翻），不得再作为依据——"
            "不得进入任何推理、产出或完成判定；如需相关结论必须重新取证。",
            "（注意：撤回针对的是**当时的声称及其失效依据**，不禁止基于全新证据"
            "重新认证同一事项。）",
        ]
        for e in items[-8:]:
            why = f"（证伪依据：{str(e.get('reason') or '')[:80]}）" if e.get("reason") else ""
            lines.append(f"- ❌ {str(e.get('summary') or '')[:150]}{why}")
        lines.append("</retracted_claims>")
        return "\n".join(lines)
    def _retract_reopened_step_claims(
        self, reopened: List[str], deleted: List[str], detail: str = "",
    ) -> None:
        """完整性违规重开步骤时撤回其「已完成/已交付」声称（G3 写入点）。

        A4.9 I3：同时把 settled_ledger 里对应 step_complete 条目标记为已撤回，
        否则「已定案不得翻案」与「已被证伪不得依据」会同时注入同一批提示词。
        A4.9 I4：撤回对象是**当时那条声称**（带轮次与失效依据），不是该步骤
        的永久封禁——重新产出后可基于新证据重新认证。
        """
        _sync_deathmatchevidence_globals()
        turn = self._conv.deathmatch_turns or 0
        files_part = ("；失效依据交付物：" + ", ".join(str(d) for d in (deleted or [])[:5])) if deleted else ""
        detail_part = f"{str(detail or '')[:80]}" if detail else ""
        for sid in (reopened or [])[:5]:
            sid = _sanitize_research_field(sid, 40)
            self._record_retracted_claim(
                f"第{turn}轮『步骤 {sid} 已完成/已交付』的声称",
                reason=_sanitize_research_field(
                    f"{detail_part}{files_part}".strip("；"), 200),
                source="integrity",
            )
        try:
            ledger = self._settled_ledger()
            changed = False
            for e in ledger:
                if str(e.get("step_id") or "") in set(reopened or []):
                    e["retracted"] = True
                    e["retracted_reason"] = _sanitize_research_field(
                        detail_part or "交付物已从工作区消失", 120)
                    changed = True
            if changed:
                self._conv.deathmatch_settled_ledger = ledger
        except Exception as exc:
            logger.debug("settled ledger retract-mark failed: %s", exc)

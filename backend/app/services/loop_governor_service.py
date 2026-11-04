# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""循环 Governor v2 — Tier A 通用运行时循环防线（常驻，与 vibeweaver 开关解耦）。

设计：`docs/DESIGN_20261025_loop_guard_v2.md`（双层 Governor；本模块 = Tier A 本体）。
共享底座：detectors 纯函数（`loop_detectors.py`）、事件总线（`agent_event_bus`）、
预算台账（`vibeweaver_audit_service.SessionObservation` 双轨计数）。

本波（P0+P1）能力：
- A6 三态模式 off|shadow|enforce（默认 shadow）+ 进程级逃生口 env ``WT_LOOP_GOVERNOR=off``；
- F4 execute_code 迭代预算退款封顶（每用户回合 ≤refund_max_per_turn）；
- A4 回合硬顶：墙钟（不可关兜底 3600s）+ 总迭代（默认 150）+ 单回合工具调用
  （≤100 不可关，qwen-code 先例）——配置只能调小不能调没；
- A1 StreamWatch：`watch_stream` 包裹 provider chunk 迭代器——观察移到消费侧
  （结构性消除「忘记发布」）+ 每 chunk 取消检查（修 F3 的 1800s 退化窗口）；
- A3 升级阶梯 Hint→Truncate→Terminate + 三态熔断（60s 冷却）；
- A5 安装解耦：`install()` 在 main.py 无条件挂载（audit/gate/stop-guard 仍随
  [vibeweaver] enabled）。

边界声明（设计 §A5 + A4.9 评审裁定）：voice 链路本期不覆盖（生成短、barge-in
天然打断）；**缓冲后审计的旁路**（audit salvage / best-of 选择器 / compressor
summary）不包 watch_stream——它们不向用户 live 流式，mid-stream 取消只会截断
内部缓冲产生畸形终稿，退化面由既有 send-audit 拒绝预算把关。governor 状态
per-process（多 worker 只覆盖单进程观察）。全部动作 fail-open：防线自身异常
绝不拖垮生成主链路。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from app.core.config import get_config

logger = logging.getLogger(__name__)

# 不可关硬顶（设计 §A4：kill switch 关的是检测与升级动作，硬顶是墙钟/调用数
# 上界，两者分层——「kill switch 全关也不会跑飞」）。
HARD_WALL_CLOCK_SECONDS = 3600.0
HARD_TOOL_CALLS = 100
DEFAULT_REFUND_MAX_PER_TURN = 10
DEFAULT_MAX_TOTAL_ITERATIONS = 150
BREAKER_COOLDOWN_SECONDS = 60.0
_BREAKER_MAX_ENTRIES = 1000

GOVERNOR_DISCLOSURE = (
    "检测到重复/失控生成，本回合已中断（loop governor v2）。以下为基于已有结果的部分回答。"
)


def governor_mode() -> str:
    """off | shadow | enforce（env WT_LOOP_GOVERNOR=off 无条件短路；配置异常
    fail-safe 到 shadow——检测在、动作无）。"""
    try:
        return get_config().agent_loop_governor_mode
    except Exception:
        return "shadow"


# ---------------- F4：退款封顶 + 回合硬顶 ----------------

def allow_execute_code_refund(state: Any, *, max_refunds: Optional[int] = None) -> bool:
    """execute_code 迭代预算退款门控（修 F4：每波退款 ⇒ 变参循环净零消耗）。

    返回 True = 授权退款且已计数（调用方负责执行 ``budget.refund()``）；
    超限返回 False（不计数、不退款）。"""
    if max_refunds is None:
        try:
            max_refunds = int(get_config().agent_loop_governor_refund_max_per_turn)
        except Exception:
            max_refunds = DEFAULT_REFUND_MAX_PER_TURN
    used = int(getattr(state, "loop_refunds", 0) or 0)
    if used >= max_refunds:
        return False
    state.loop_refunds = used + 1
    return True


def effective_turn_wall(cfg_seconds: float) -> float:
    """有效回合墙钟：cfg>0 → min(cfg, 3600)；cfg=0（关）→ 3600 兜底。
    不可关硬顶语义——配置只能调小不能调没。"""
    try:
        v = float(cfg_seconds)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return HARD_WALL_CLOCK_SECONDS
    return min(v, HARD_WALL_CLOCK_SECONDS)


def turn_hard_limits_hit(
    state: Any,
    *,
    wall_seconds_eff: float,
    now: float,
    max_total_iterations: Optional[int] = None,
) -> str:
    """回合硬顶判定（纯函数，时钟注入可测）。

    返回 "" | "wall" | "total_iterations" | "tool_calls"（优先级依次检查）。"""
    if max_total_iterations is None:
        try:
            max_total_iterations = int(get_config().agent_loop_governor_max_total_iterations)
        except Exception:
            max_total_iterations = DEFAULT_MAX_TOTAL_ITERATIONS
    started = float(getattr(state, "turn_started_at", 0.0) or 0.0)
    if started and wall_seconds_eff > 0 and (now - started) >= wall_seconds_eff:
        return "wall"
    if int(getattr(state, "iterations", 0) or 0) >= int(max_total_iterations):
        return "total_iterations"
    if int(getattr(state, "total_tool_calls", 0) or 0) >= HARD_TOOL_CALLS:
        return "tool_calls"
    return ""


# ---------------- A1/A3：run 上下文 + 升级阶梯 + 三态熔断 ----------------

@dataclass
class GovernorRunContext:
    """per-用户回合 governor 状态（watch_stream 与编排器共享的取消/收口信令）。"""

    conv_id: str
    seq: int = 0  # A4.9-I4：回合代际——旧回合 finally 不得弹掉新回合 ctx
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    ladder: Dict[str, int] = field(default_factory=dict)  # channel -> 已授权干预数
    terminated: bool = False
    interrupted: bool = False  # watch_stream 曾断流（遥测/披露用）
    disclosure: str = ""
    disclosure_emitted: bool = False  # R2-#124：披露恰一次（tail/边界双收口点去重）


_run_ctxs: "OrderedDict[str, GovernorRunContext]" = OrderedDict()
_MAX_RUN_CTXS = 200
_ctx_seq = itertools.count(1)


def register_run_ctx(conv_id: str) -> GovernorRunContext:
    """回合开始 → **新建** ctx（代际身份：A4.9-I4——复用同对象会让旧回合的
    引用与新 ctx 无法区分，seq 守卫失效）。旧条目直接覆盖（LRU 容量有界）。"""
    while len(_run_ctxs) >= _MAX_RUN_CTXS:
        _run_ctxs.popitem(last=False)
    ctx = GovernorRunContext(conv_id=conv_id, seq=next(_ctx_seq))
    _run_ctxs[conv_id] = ctx
    return ctx


def unregister_run_ctx(conv_id: str, seq: int = 0) -> None:
    """回合收尾释放。A4.9-I4：带 seq 时只释放**本回合**的 ctx——重连/重试
    场景下旧回合 finally 晚于新回合 register，无代际会误弹新 ctx。"""
    ctx = _run_ctxs.get(conv_id or "")
    if ctx is None:
        return
    if seq and ctx.seq != seq:
        return
    _run_ctxs.pop(conv_id or "", None)
    # A4.9-I3：试探回合无论 ladder 是否为空都要闭合 HALF-OPEN（按会话键扫）
    _breaker.on_turn_end(conv_id)


def get_run_ctx(conv_id: str) -> Optional[GovernorRunContext]:
    return _run_ctxs.get(conv_id or "")


def consume_terminate_disclosure(conv_id: str) -> Optional[str]:
    """R2-#124：Terminate 披露**恰一次**（tail 与边界/verdict 出口共用）。
    返回披露文案；未 terminated 或已发过 → None。"""
    ctx = _run_ctxs.get(conv_id or "")
    if ctx is None or not ctx.terminated or ctx.disclosure_emitted:
        return None
    ctx.disclosure_emitted = True
    return ctx.disclosure or GOVERNOR_DISCLOSURE


def register_turn(state: Any, conversation_id: str) -> None:
    """回合注册（幂等，R2-#126）：direct_reply 阶段与 `_run_loop` 入口共用。
    同回合重入不得重置台账/丢 terminated（A4.9-I2 自噬修复）——以
    `state.gov_ctx_seq` + ctx 身份判同回合，已注册即跳过。"""
    conv_id = str(conversation_id or "")
    seq = int(getattr(state, "gov_ctx_seq", 0) or 0)
    if seq:
        ctx = _run_ctxs.get(conv_id)
        if ctx is not None and ctx.seq == seq:
            return
    try:
        from app.services import vibeweaver_audit_service as _aw_reg

        _aw_reg.register_run(conv_id, state)
    except Exception:
        logger.debug("loop-guard register failed-open")
    try:
        state.turn_started_at = time.monotonic()
        state.gov_ctx_seq = register_run_ctx(conv_id).seq
    except Exception:
        logger.debug("loop-governor ctx register failed-open")


class _CircuitBreaker:
    """会话×通道 三态熔断（设计 §A3）：Terminate → OPEN；60s 冷却 →
    HALF-OPEN（放行一次试探）；试探回合正常收尾 → CLOSED。OPEN 期命中
    log-only（suppressed）。"""

    def __init__(self) -> None:
        self._opened: Dict[Tuple[str, str], float] = {}
        self._half_open: set = set()

    def open(self, key: Tuple[str, str], now: float) -> None:
        self._opened[key] = now
        self._half_open.discard(key)

    def is_open(self, key: Tuple[str, str], now: Optional[float] = None) -> bool:
        opened = self._opened.get(key)
        if opened is None:
            return False
        if (now if now is not None else time.monotonic()) - opened >= BREAKER_COOLDOWN_SECONDS:
            self._half_open.add(key)  # 冷却到期：HALF-OPEN 放行
            return False
        return True

    def on_turn_end(self, conv_id: str) -> None:
        """A4.9-I3：回合收尾的熔断整理——该会话所有 HALF-OPEN 键闭合
        （试探回合无论是否再犯都算「成功」），并对陈旧 OPEN 记录做容量淘汰
        （冷却早已过期的条目没有保留价值）。"""
        for key in [k for k in self._half_open if k[0] == conv_id]:
            self._half_open.discard(key)
            self._opened.pop(key, None)
        now = time.monotonic()
        if len(self._opened) > _BREAKER_MAX_ENTRIES:
            for key, ts in sorted(self._opened.items(), key=lambda kv: kv[1]):
                if now - ts >= BREAKER_COOLDOWN_SECONDS:
                    self._opened.pop(key, None)
                    self._half_open.discard(key)
                if len(self._opened) <= _BREAKER_MAX_ENTRIES:
                    break

    def clear(self) -> None:
        self._opened.clear()
        self._half_open.clear()


_breaker = _CircuitBreaker()


def _record_gov_event(conv_id: str, channel: str, finding: Dict[str, str], action: str) -> None:
    """遥测：logger + 现有 .vibeweaver state 通道（P3 波落 DB 表 loop_governor_events）。"""
    try:
        from app.services import vibeweaver_state
        from app.services.vibeweaver_audit_service import _guard_root

        root = _guard_root(conv_id)
        kind = str(finding.get("kind") or "")
        pattern = str(finding.get("pattern") or "")
        if action == "suppressed":
            vibeweaver_state.record_loop_guard_event(root, channel, suppressed=True)
        else:
            vibeweaver_state.record_loop_guard_event(root, channel, kind=kind, pattern=pattern)
        logger.info(
            "loop-governor: conv=%s channel=%s action=%s kind=%s pattern=%.60s",
            conv_id, channel, action, kind, pattern,
        )
    except Exception:
        pass


def escalate(
    conv_id: str,
    finding: Dict[str, str],
    channel: str,
    feedback: str,
    *,
    now: Optional[float] = None,
) -> str:
    """A3 升级阶梯（设计 §4.2）：同回合同通道 1=Hint（仅纠正指令）→
    2=Truncate（断流保留 partial）→ 回合预算耗尽再犯=Terminate（收口+披露+
    熔断）。shadow 只记不动作；off 直落。返回动作名（遥测/测试面）。

    预算唯一台账 = SessionObservation 双轨计数（request_interrupt，Tier A/B
    共享一本账，设计 §4.3-2「单一台账」）。"""
    mode = governor_mode()
    if mode == "off":
        return "off"
    if mode == "shadow":
        # G3（评审 B-③）：shadow 也闩锁 armed——同 episode 只记一次，防每
        # 256 字符重发 finding 的日志/遥测噪音（干净观察或新 msg 由 _guard_check 重武装）。
        # R2-#127：同时记 fired_msg_ids（与 enforce 路径对称）——否则 stale
        # fired 会把后续命中判成 new_turn 重复放行。
        from app.services import vibeweaver_audit_service as _aw_shadow
        _o = _aw_shadow._obs(conv_id)
        _o.armed[channel] = False
        _o.fired_msg_ids[channel] = _o.last_msg_id
        _record_gov_event(conv_id, channel, finding, "shadow")
        return "shadow"
    now_ts = now if now is not None else time.monotonic()
    key = (conv_id, channel)
    if _breaker.is_open(key, now_ts):
        _record_gov_event(conv_id, channel, finding, "suppressed")
        return "suppressed"
    ctx = _run_ctxs.get(conv_id) or register_run_ctx(conv_id)
    from app.services import vibeweaver_audit_service as aw

    obs = aw._obs(conv_id)
    turn_n, sess_m = aw._budget_limits()
    used_sess = obs.interventions.get(channel, 0)
    used_turn = obs.turn_interventions.get(channel, 0)
    if used_sess >= sess_m:
        # 会话防抖帽：跨回合累计耗尽 → log-only（设计 §A4 M）
        _record_gov_event(conv_id, channel, finding, "suppressed")
        return "suppressed"
    if used_turn >= turn_n:
        # 回合预算已烧完仍再犯 → Terminate（不静默续烧；Terminate 不占预算）。
        # tools 通道无「当前流」可断：置 cancel 会牺牲下一条流（空转迭代），
        # 收口由 ctx.terminated（迭代边界消费）承担。
        if channel != "tools":
            ctx.cancel.set()
        ctx.terminated = True
        ctx.disclosure = GOVERNOR_DISCLOSURE
        _breaker.open(key, now_ts)
        _record_gov_event(conv_id, channel, finding, "terminate")
        return "terminate"
    granted = aw.request_interrupt(conv_id, conv_id, feedback, channel)
    if not granted:
        _record_gov_event(conv_id, channel, finding, "suppressed")
        return "suppressed"
    # 单生成内 warn→abort（设计 §A3 防误报口径）要求同 episode 二次命中可升级：
    # 再武装观察闩锁，防抖改由双轨预算 + 熔断承担。
    obs.armed[channel] = True
    n = ctx.ladder.get(channel, 0) + 1
    ctx.ladder[channel] = n
    if n >= 2:
        # Truncate：断**当前流**（partial 保留在消费方累加器）；tools 通道
        # 无流可断，退化为纠正指令注入（收口仍由预算→Terminate 承担）。
        if channel != "tools":
            ctx.cancel.set()
        _record_gov_event(conv_id, channel, finding, "truncate")
        return "truncate"
    _record_gov_event(conv_id, channel, finding, "hint")
    return "hint"


# ---------------- A1 · StreamWatch ----------------

async def watch_stream(
    aiter: AsyncIterator[dict],
    *,
    conv_id: str,
    msg_id: str = "",
) -> AsyncIterator[dict]:
    """A1 StreamWatch：把 provider chunk 迭代器包进统一消费点（设计 §4.2）。

    - **观察在消费侧**：content/reasoning 增量喂观察通道——「忘记发布」这类
      结构性漏洞从此不可能出现（审计 F1 的根治形态）；
    - **取消在 chunk 粒度**：每 chunk 检查 run cancel 事件，命中即停止透传
      （断流 ≤1 chunk），已 yield 的事件保留在消费方累加器（partial 不静默
      清空）——把「打断」从「下一迭代边界」提前到「下一 chunk」（修 F3）；
    - 检测命中 → `escalate` 阶梯（Hint 只注入纠正；Truncate/Terminate 置
      cancel）；观察者一切异常 fail-open，防线绝不拖垮生成主链路。

    mode=off 或无 ctx 的旁路调用直通（零开销透传）。"""
    mode = governor_mode()
    if not conv_id or mode == "off":
        async for event in aiter:
            yield event
        return
    from app.services import vibeweaver_audit_service as aw

    # R2-#125（A4.9-I1）：入口捕获**本代** ctx——延迟收尾（GC/aclose）不得
    # 清掉新代 ctx 的 cancel（跨代信号丢失），cut 检查同样只看本代。
    _own_ctx = _run_ctxs.get(conv_id)

    try:
        async for event in aiter:
            try:
                if _own_ctx is not None and _own_ctx.cancel.is_set():
                    _own_ctx.interrupted = True
                    # A4.9-C3：cancel 一次性消费——Truncate 断的是**当前流**；
                    # 若不清除，本回合后续生成（纠正重试轮/final-thinking/grace）
                    # 会在首个 chunk 被静默掐死（空答）。回合级收口信号由
                    # ctx.terminated（迭代边界消费）承担。
                    _own_ctx.cancel.clear()
                    logger.info(
                        "loop-governor: stream interrupted mid-generation conv=%s msg=%s",
                        conv_id, msg_id,
                    )
                    return
                if isinstance(event, dict):
                    et = event.get("type")
                    if et == "content" or et == "reasoning":
                        ed = event.get("data")
                        if isinstance(ed, str) and ed:
                            ch = "reasoning" if et == "reasoning" else "text"
                            finding = aw.observe_text_delta(
                                conv_id, ed,
                                "reasoning" if et == "reasoning" else "content",
                                msg_id,
                            )
                            if finding:
                                fb = aw._intervention_text(finding, ch)
                                escalate(conv_id, finding, ch, fb)
            except Exception as exc:
                logger.debug("watch_stream observer failed-open: %s", exc)
            yield event
    finally:
        # G4（re-review Minor-1）：流自然结束/被消费者废弃时，本代**未被消费**
        # 的 cancel 随生成代失效——否则「置位恰在末 chunk 之后」会残掐下一条
        # 流的首 chunk（断流信号的作用对象已不存在）。R2-#125：只清本代。
        try:
            if (
                _own_ctx is not None
                and _run_ctxs.get(conv_id) is _own_ctx
                and _own_ctx.cancel.is_set()
            ):
                _own_ctx.cancel.clear()
        except Exception:
            pass


# ---------------- A5 · 安装解耦 ----------------

_gov_installed = False


async def _gov_tool_after(ctx: Any) -> Optional[Any]:
    """tool 通道观察（noop-bash → escalate）。A5：钩子挂 governor（常驻），
    不再随 vibeweaver audit 安装——关掉 vibeweaver 不再连产品防线一起关掉。
    A4.9-I2：mode=off 时**不观察**（判据 #110「不观察不动作」）。"""
    if ctx.tool_name != "terminal":
        return None
    if governor_mode() == "off":
        return None
    from app.services import vibeweaver_audit_service as aw

    command = str((ctx.args or {}).get("command") or "")
    obs_key = ctx.conversation_id or ctx.session_id
    finding = aw.observe_tool_call(obs_key, "terminal", command, msg_id=ctx.turn_key or "")
    if finding:
        fb = aw._intervention_text(finding, "tools")
        escalate(obs_key, finding, "tools", fb)
    return None


def install() -> Dict[str, Any]:
    """main.py 无条件挂载（mode off 为 per-call 短路——动态 kill switch 不需要
    重装钩子）。幂等。"""
    global _gov_installed
    summary = {"governor_tool_hooks": 0}
    if _gov_installed:
        return summary
    from app.services import hook_service

    hook_service.register_tool_hook("after", _gov_tool_after, name="loop_governor")
    _gov_installed = True
    summary["governor_tool_hooks"] = 1
    logger.info("loop governor v2 installed (mode=%s)", governor_mode())
    return summary

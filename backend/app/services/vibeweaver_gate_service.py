# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""vibeweaver 物理门禁 — vibeweaver-gate.js 的 Python 移植（P1 / A2）。

语义与 OpenCode 插件逐条对齐（共用 scripts/assert_artifacts.py 为唯一裁判）：

- vibeweaver-active 判定 = 工作区存在 ``tests/verification_log.md``。
- write/edit 后置检查：跑 ``tests/assert_artifacts.py``（4 种 flag 组合），
  blocking 失败 → GATE-BLOCKED（结果变红，完成声明被挡而非执行被停）；
  结构缺口 → GATE-WARNING（非阻断，追加到结果文本）。
- 证据修复豁免：写入路径首段为 tests/ / test/ / memory/ 的**不设门**
  （证据修复就是这些写入，拦截会造成首条日志死锁——gate.js 同款规则）。
- assert_artifacts.py 缺失 → 内联证据地板检查（≥1 条 `- iter N PASS/FAIL:`、
  acceptance.md 首行 ``> cap=5  stall=3x``、引用的截图非空），并提示从
  skill scripts/ 复制正典脚本。
- 停顿观察器：同文件连写 3 次且中间无新 ``iter N PASS`` → GATE-WARNING
  （事实报告，判断留给模型；状态 .vibeweaver/state.json 原子写）。
- 会话 idle 巡检：门禁红时留 warn 日志（不拦截）。
- 逃生口：``VIBEWEAVER_GATE=off`` 或 [vibeweaver] gate_enabled=false。

执行调用方：hook_service 的 tool.after（workspace_write/workspace_edit）
与 stop 钩子（idle 复检）。**fail-open**：本服务任何异常不得阻断写入。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config
from app.services import hook_service

logger = logging.getLogger(__name__)

STATE_DIR = ".vibeweaver"
STATE_FILE = "state.json"
STALL_RUN = 3
MAX_OPS = 20
FLAG_COMBOS: List[List[str]] = [[], ["--existing"], ["--backend-only"], ["--existing", "--backend-only"]]
BLOCKING_HINTS = ("verification_log", "acceptance", "cap=5", "screenshot", "iter ", "script/linux", "workflows")

_GATED_TOOLS = {"workspace_write", "workspace_edit"}
_ITER_ENTRY_RE = re.compile(r"^- iter \d+ (PASS|FAIL):", re.M)
_ACC_FIRSTLINE_RE = re.compile(r"^>\s*cap=5\s+stall=3", re.M)
_PNG_CITE_RE = re.compile(r"tests/(\S+\.png)")

_install_lock = asyncio.Lock()
_installed = False


# ---------- 基础 I/O ----------

def _safe_read(p: Path) -> str:
    try:
        if p.exists() and p.stat().st_size > 0:
            return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _size_of(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def find_project_root(starts: List[Optional[str]], bound: Optional[str] = None) -> Optional[Path]:
    """自起点向上找含 tests/verification_log.md 的目录（gate.js 同款）。

    A4.9 C2：`bound`（工作区根）为硬边界——命中点必须等于/位于 bound 之下。
    防止工作区没有 vibeweaver 产物时一路向上挂到安装根（服务端自身仓库的
    tests/verification_log.md），把执法层错误附着到多租户宿主上。bound 未给
    或不可解析时返回 None（fail-closed：不激活）。
    """
    if not bound:
        return None
    try:
        bound_resolved = Path(bound).resolve()
    except OSError:
        return None

    def _within(p: Path) -> bool:
        return p == bound_resolved or bound_resolved in p.parents

    for start in starts:
        if not start:
            continue
        try:
            d = Path(start).resolve()
        except OSError:
            continue
        if not _within(d):
            continue
        while True:
            if (d / "tests" / "verification_log.md").exists():
                return d
            if d == bound_resolved or d.parent == d:
                break
            d = d.parent
    return None


# ---------- 会话项目根注册表（W12 P1#6） ----------
# 子目录项目在工作区内不会被「仅从 workspace_path 出发」的 stop/RED-latch 搜索发现；
# tool.after 命中项目根时登记，stop/audit 优先复用（bound 校验 + 失活失效）。
_RECENT_ROOTS: Dict[str, Path] = {}
_RECENT_ROOTS_CAP = 200


def remember_project_root(key: str, root: Path) -> None:
    if not key:
        return
    if len(_RECENT_ROOTS) >= _RECENT_ROOTS_CAP and key not in _RECENT_ROOTS:
        for k in list(_RECENT_ROOTS.keys())[:50]:
            _RECENT_ROOTS.pop(k, None)
    _RECENT_ROOTS[key] = root


def recent_project_root(key: str) -> Optional[Path]:
    root = _RECENT_ROOTS.get(key or "")
    if root is None:
        return None
    if not (root / "tests" / "verification_log.md").exists():
        _RECENT_ROOTS.pop(key, None)
        return None
    return root


def resolve_session_root(
    workspace_path: Optional[str],
    key: str,
    starts: Optional[List[Optional[str]]] = None,
    prefer_hints: bool = False,
) -> Optional[Path]:
    """会话项目根解析（W12；评审修复：hint 优先）。

    - ``prefer_hints=True``（tool 调用，带写入文件路径提示）：按提示路径在 bound
      内搜索并登记——多项目工作区必须按**本次写入归属**裁决（注册表仅在无提示时
      兜底）；提示无命中 → None（不属于任何 active 项目 = 不设门，保持旧语义）。
    - ``prefer_hints=False``（stop / RED-latch 检查，无路径提示）：注册表优先，
      兜底从 workspace_path 向上搜索。bound 缺失 → fail-closed None。
    """
    bound = workspace_path or None
    if prefer_hints:
        # 相对路径按 bound 归一（tool schema 允许工作区相对路径；否则会按服务进程
        # CWD 解析而被 bound 过滤，静默绕过门禁——复评审 N1）。
        hint_paths: List[Optional[str]] = []
        for _s in (starts or []):
            if not _s:
                continue
            try:
                _p = Path(_s).expanduser()
                if not _p.is_absolute() and bound:
                    _p = Path(bound) / _p
                hint_paths.append(str(_p))
            except (OSError, ValueError):
                continue
        hinted = find_project_root(hint_paths, bound=bound)
        if hinted is not None:
            remember_project_root(key, hinted)
            return hinted
        if hint_paths:
            # 复审 A-I1：hint 可解析但不属于任何 active 项目 → None（不设门，
            # 保持旧语义）。不得回退注册表——否则 A 项目的 RED-latch/证据面会把
            # 尚未建档的 B 项目的首次协议写入误判拦截。
            return None
        # 无任何可用提示（git 等无路径类写工具）→ 回退注册表/工作区根（复评审 N1）。
    if not bound:
        return None
    root = recent_project_root(key)
    if root is not None:
        try:
            b = Path(bound).resolve()
            if root == b or b in root.parents:
                return root
        except OSError:
            pass
    root = find_project_root([bound], bound=bound)
    if root is not None:
        remember_project_root(key, root)
    return root


# ---------- assert_artifacts.py 驱动 ----------

def _ensure_assert_script(root: Path) -> Optional[Path]:
    """正典脚本缺失时从 vibeweaver 系统 skill 复制（gate.js 修复指引的
    服务端自动化——agent 无需手抄 29KB）。"""
    target = root / "tests" / "assert_artifacts.py"
    if target.exists():
        return target
    try:
        # W12 P0#2：冻结包资源根解析（bundled_first——可执行资产不得被数据目录副本投毒；
        # 旧实现直取 backend_root=用户配置目录，打包态物化恒失败）。
        src = get_config().resolve_resource(
            "skills", "vibeweaver", "scripts", "assert_artifacts.py", bundled_first=True
        )
    except Exception:
        return None
    if not src.is_file():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("vibeweaver gate: materialized tests/assert_artifacts.py into %s", root)
        return target
    except OSError as exc:
        logger.warning("vibeweaver gate: cannot materialize assert_artifacts.py: %s", exc)
        return None


async def run_assert(root: Path, runner: str) -> Tuple[bool, List[Dict[str, Any]]]:
    """跑正典断言脚本（4 flag 组合），返回 (ok, attempts)。runner=python3 绝对路径。"""
    attempts: List[Dict[str, Any]] = []
    script = root / "tests" / "assert_artifacts.py"
    for flags in FLAG_COMBOS:
        try:
            proc = await asyncio.create_subprocess_exec(
                runner, str(script), *flags,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip()
            if proc.returncode == 0:
                return True, [{"flags": flags, "output": output}]
            attempts.append({"flags": flags, "output": output or f"exit {proc.returncode}"})
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            attempts.append({"flags": flags, "output": "assert_artifacts.py timeout (15s)"})
        except OSError as exc:
            attempts.append({"flags": flags, "output": f"assert run failed: {exc}"})
    return False, attempts


def _failure_messages(attempts: List[Dict[str, Any]]) -> List[str]:
    seen = set()
    messages: List[str] = []
    for a in attempts:
        for line in (a.get("output") or "").splitlines():
            m = line.strip()
            if not m.startswith("- "):
                continue
            msg = m[2:]
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
    if not messages and attempts:
        messages.append((attempts[-1].get("output") or "")[:400])
    return messages


def _classify(messages: List[str]) -> Tuple[List[str], List[str]]:
    blocking, warnings = [], []
    for msg in messages:
        if any(h in msg for h in BLOCKING_HINTS):
            blocking.append(msg)
        else:
            warnings.append(msg)
    return blocking, warnings


def inline_check(root: Path) -> List[str]:
    """assert_artifacts.py 不可得时的证据地板（gate.js inlineCheck 移植）。"""
    failures: List[str] = []
    tests_dir = root / "tests"
    log = _safe_read(tests_dir / "verification_log.md")
    acc = _safe_read(tests_dir / "acceptance.md")
    if not _ITER_ENTRY_RE.search(log):
        failures.append("tests/verification_log.md has no `- iter N PASS/FAIL:` entries (COV-1)")
    if not _ACC_FIRSTLINE_RE.search(acc):
        failures.append("tests/acceptance.md missing first line `> cap=5  stall=3x` (COV-7)")
    for m in _PNG_CITE_RE.finditer(log + "\n" + acc):
        p = tests_dir / m.group(1)
        if _size_of(p) <= 0:
            failures.append(f"screenshot claimed but missing/empty: tests/{m.group(1)} (A4.4)")
    return failures


# ---------- 检查编排 ----------

async def check_gate_async(root: Path) -> Optional[Dict[str, Any]]:
    _ensure_assert_script(root)
    runner = shutil.which("python3")
    if (root / "tests" / "assert_artifacts.py").exists() and runner:
        ok, attempts = await run_assert(root, runner)
        if ok:
            return None
        blocking, warnings = _classify(_failure_messages(attempts))
        return {
            "blocking": blocking,
            "warnings": warnings,
            "attempts": ["[" + " ".join(a["flags"]) + "]" for a in attempts],
        }
    failures = inline_check(root)
    if failures:
        return {"blocking": failures, "warnings": [], "inline": True}
    return None


def count_passes(root: Path) -> int:
    log = _safe_read(root / "tests" / "verification_log.md")
    return len(re.findall(r"^- iter \d+ PASS:", log, re.M))


# ---------- 停顿观察器（事实报告，非裁决） ----------

def stall_observation(root: Path, file_rel: str) -> Optional[str]:
    """同文件 3 连写且无新 PASS → STALL 事实报告（D2：once-per-signature latch）。

    wave17 语义：`firedKey = "<file>@<pass_count>"` 同签名只报一次（每次命中都
    报是告警洪水，训练出告警盲视）；新 PASS 落地改变签名 → 自动重武装。
    走 `vibeweaver_state.mutate_state` 共享锁（评审 r1-A #2：与本进程其它
    state.json 写者同锁）；未知字段保留（wave21 教训：禁止整体重置）。
    """
    try:
        from app.services import vibeweaver_state as _vst

        out: Dict[str, Optional[str]] = {"msg": None}

        def _m(st: Dict[str, Any]) -> None:
            ops = st.get("ops") if isinstance(st.get("ops"), list) else []
            ops.append({"f": file_rel, "p": count_passes(root), "t": int(time.time() * 1000)})
            st["ops"] = ops[-MAX_OPS:]
            run = st["ops"][-STALL_RUN:]
            if len(run) < STALL_RUN:
                return
            same_file = all(o.get("f") == run[0].get("f") for o in run)
            no_new_pass = run[0].get("p") == run[-1].get("p")
            if not (same_file and no_new_pass):
                return
            fired_key = f"{run[0].get('f')}@{run[0].get('p')}"
            stall_state = st.get("stall") if isinstance(st.get("stall"), dict) else {}
            if stall_state.get("firedKey") == fired_key:
                return  # 同签名只报一次（新 PASS → 签名变化 → 自动重武装）
            st["stall"] = {"firedKey": fired_key, "ts": int(time.time() * 1000)}
            out["msg"] = (
                f'STALL observed (machine-counted): "{run[0].get("f")}" modified {STALL_RUN}x '
                "with no new \"iter N PASS\" entry in tests/verification_log.md in between — "
                "COV-7 stall=3x is likely reached. Do not retry the same direction: parameterize "
                "(finite candidate set + cheapest refuting test) or shift the abstraction/strategy "
                "— TESTING_PROTOCOLS.md §A4.10."
            )

        _vst.mutate_state(root, _m)
        return out["msg"]
    except Exception:
        return None


# ---------- 对外入口（hook 回调用） ----------

def _block_message(root: Path, result: Dict[str, Any]) -> str:
    lines = [
        "GATE-BLOCKED (vibeweaver physical gate): WRITE SUCCEEDED — this is a completion gate, "
        "NOT an execution stop. The task cannot be DECLARED complete yet — verification evidence "
        "is missing or falsified:",
    ]
    lines.extend(f"- {m}" for m in result.get("blocking", []))
    if result.get("warnings"):
        lines.append("Non-blocking structure warnings (fix before the final [Verification Gate] line):")
        lines.extend(f"- {m}" for m in result["warnings"])
    else:
        lines.append("No structure warnings.")
    if result.get("inline"):
        lines.append(
            "tests/assert_artifacts.py is missing — either copy it from the vibeweaver skill's "
            "scripts/assert_artifacts.py, or satisfy the inline evidence floor: >=1 `- iter N PASS/FAIL:` "
            "entry in tests/verification_log.md, tests/acceptance.md first line `> cap=5  stall=3x`, "
            "and every cited screenshot/media file present and non-empty."
        )
    elif result.get("attempts"):
        lines.append("assert_artifacts.py flag attempts: " + " ".join(result["attempts"]))
    lines.append(
        "This gate is re-checkable, not a dead stop: fix the artifacts, then your next write/edit "
        "re-runs it automatically. If the failure is legitimately out of scope, set VIBEWEAVER_GATE=off "
        "or escalate to the user."
    )
    return "\n".join(lines)


def is_evidence_path(root: Path, file_path: Optional[str]) -> bool:
    """写入路径首段为 tests/ / test/ / memory/ → 证据修复路径，永不禁门。"""
    if not file_path:
        return False
    try:
        # 复审 A-I3：tool schema 允许工作区相对路径——相对路径必须按项目根归一，
        # 否则按服务进程 CWD 解析 → relative_to 必失败 → 证据修复写被 RED 误封。
        p = Path(file_path).expanduser()
        if not p.is_absolute():
            p = Path(root) / p
        rel = p.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    if not rel.parts:
        return False
    return rel.parts[0].lower() in ("test", "tests", "memory")


async def gate_check_write_async(root: Path, file_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """一次落盘后的门禁评估。返回 {block} | {notes} | None。"""
    try:
        if not get_config().vibeweaver_gate_enabled:
            return None
    except Exception:
        return None
    if is_evidence_path(root, file_path):
        return None
    result = await check_gate_async(root)
    if result and result.get("blocking"):
        return {"block": _block_message(root, result)}
    notes: List[str] = []
    if result and result.get("warnings"):
        notes.append(
            "[GATE-WARNING (vibeweaver)] non-blocking: " + "; ".join(result["warnings"])
            + " — fix before the final [Verification Gate] line."
        )
    rel = ""
    if file_path:
        try:
            rel = str(Path(file_path).resolve().relative_to(root.resolve()))
        except (OSError, ValueError):
            rel = file_path
    stall = stall_observation(root, rel or "(unknown file)")
    if stall:
        notes.append("[GATE-WARNING (vibeweaver-stall)] " + stall)
    return {"notes": notes} if notes else None


async def gate_idle_check_async(root: Path) -> Optional[Dict[str, Any]]:
    """turn 收尾巡检：门禁红 → {blocking}（调用方留痕/拦截收尾）。"""
    try:
        if not get_config().vibeweaver_gate_enabled:
            return None
    except Exception:
        return None
    result = await check_gate_async(root)
    if result and result.get("blocking"):
        return {"blocking": result["blocking"]}
    return None


# ---------- hook 安装 ----------

async def _tool_after_hook(ctx: hook_service.ToolHookContext) -> Optional[hook_service.HookDecision]:
    if not ctx.vibeweaver_active:
        # W17 轮次门控：非激活轮（非代码任务）不设门——证据面/项目根解析都不做
        return None
    if ctx.tool_name not in _GATED_TOOLS:
        return None
    key = ctx.conversation_id or ctx.session_id
    # 评审修复：tool 调用必须按本次写入路径归属裁决（hint 优先于注册表），
    # 否则 A 项目先被触达后，写 B 项目会被按 A 的证据/封禁误判。
    root = resolve_session_root(
        ctx.workspace_path,
        key,
        starts=[(ctx.args or {}).get("file_path"), (ctx.args or {}).get("path")],
        prefer_hints=True,
    )
    if root is None:
        return None
    file_path = (ctx.args or {}).get("file_path") or (ctx.args or {}).get("path")
    from app.services import vibeweaver_delivery_service as _delivery

    out = await gate_check_write_async(root, file_path)
    if out and out.get("block"):
        # D5：证据红时 cue 也必须到达（JS 同款：投递不依赖门禁为绿）
        try:
            cues = _delivery.cue_notes(root, file_path)
        except Exception:
            cues = []
        block = out["block"]
        if cues:
            block = block + "\n" + "\n".join(cues)
        return hook_service.HookDecision(block=block)
    notes: List[str] = list((out or {}).get("notes") or [])
    try:
        # 逃生口一致性（评审 A-m3）：gate_enabled=False 时投递/快照/cue 同步关闭
        # （JS 参考：cueNotes 在 VIBEWEAVER_GATE=off 的早退之后）。
        gate_on = True
        try:
            gate_on = bool(get_config().vibeweaver_gate_enabled)
        except Exception:
            gate_on = False
        if gate_on:
            # D5 cue triggers（编辑路径命中记忆主题）→ D4 快照刷新（working_note 写入）
            # → D4 首触 cue（每会话一次，快照先行）
            notes.extend(_delivery.cue_notes(root, file_path))
            _delivery.note_note_write(root, _delivery.rel_posix(root, file_path))
            touch = _delivery.first_touch_cue(root, key)
            if touch:
                notes.append(touch)
    except Exception:
        pass
    return hook_service.HookDecision(notes=notes) if notes else None


async def _stop_hook(ctx: hook_service.StopContext) -> Optional[hook_service.StopDecision]:
    """stop 语义：证据红 → 拦下收尾 + 纠正反馈续跑（预算由 hook_service 执行）。"""
    if not ctx.vibeweaver_active:
        return None  # W17 轮次门控：非激活轮不拦收尾
    root = resolve_session_root(ctx.workspace_path, ctx.conversation_id or ctx.session_id)
    if root is None:
        return None
    out = await gate_idle_check_async(root)
    if not out:
        return None
    feedback = (
        "[vibeweaver stop hook] 收尾被门禁拦截：验证证据缺失/不实，本轮不能声明完成。"
        "请按下列条目修复证据（写入 tests/、memory/ 不受门禁限制），"
        "并在 tests/verification_log.md 追加 `- iter N PASS/FAIL:` 条目后重新收尾：\n"
        + "\n".join(f"- {m}" for m in out.get("blocking", []))
    )
    return hook_service.StopDecision(allow=False, feedback=feedback, reason="gate red at stop")


def install_hooks() -> Dict[str, Any]:
    """注册 gate 的 tool.after 与 stop 钩子（幂等）。返回注册摘要。"""
    global _installed
    summary = {"gate_tool_hooks": 0, "gate_stop_hooks": 0}
    if _installed:
        return summary
    try:
        if not (get_config().vibeweaver_enabled and get_config().vibeweaver_gate_enabled):
            return summary
    except Exception:
        return summary
    hook_service.register_tool_hook("after", _tool_after_hook, name="vibeweaver_gate")
    hook_service.register_stop_hook(_stop_hook, name="vibeweaver_gate")
    _installed = True
    summary["gate_tool_hooks"] = 1
    summary["gate_stop_hooks"] = 1
    logger.info("vibeweaver gate hooks installed")
    return summary

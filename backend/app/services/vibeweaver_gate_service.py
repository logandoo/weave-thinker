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


# ---------- assert_artifacts.py 驱动 ----------

def _ensure_assert_script(root: Path) -> Optional[Path]:
    """正典脚本缺失时从 vibeweaver 系统 skill 复制（gate.js 修复指引的
    服务端自动化——agent 无需手抄 29KB）。"""
    target = root / "tests" / "assert_artifacts.py"
    if target.exists():
        return target
    try:
        backend_root = get_config().backend_root
    except Exception:
        return None
    src = Path(backend_root) / "skills" / "vibeweaver" / "scripts" / "assert_artifacts.py"
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


async def run_assert(root: Path) -> Tuple[bool, List[Dict[str, Any]]]:
    """跑正典断言脚本（4 flag 组合），返回 (ok, attempts)。"""
    attempts: List[Dict[str, Any]] = []
    script = root / "tests" / "assert_artifacts.py"
    for flags in FLAG_COMBOS:
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", str(script), *flags,
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
    if (root / "tests" / "assert_artifacts.py").exists():
        ok, attempts = await run_assert(root)
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
    try:
        p = root / STATE_DIR / STATE_FILE
        st: Dict[str, Any] = {"ops": []}
        if p.exists():
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                st = {"ops": []}
        if not isinstance(st.get("ops"), list):
            st["ops"] = []
        st["ops"].append({"f": file_rel, "p": count_passes(root), "t": int(time.time() * 1000)})
        st["ops"] = st["ops"][-MAX_OPS:]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        run = st["ops"][-STALL_RUN:]
        if len(run) < STALL_RUN:
            return None
        same_file = all(o.get("f") == run[0].get("f") for o in run)
        no_new_pass = run[0].get("p") == run[-1].get("p")
        if same_file and no_new_pass:
            return (
                f'STALL observed (machine-counted): "{run[0].get("f")}" modified {STALL_RUN}x '
                "with no new \"iter N PASS\" entry in tests/verification_log.md in between — "
                "COV-7 stall=3x is likely reached. Do not retry the same direction: parameterize "
                "(finite candidate set + cheapest refuting test) or shift the abstraction/strategy "
                "— TESTING_PROTOCOLS.md §A4.10."
            )
        return None
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
        rel = Path(file_path).resolve().relative_to(root.resolve())
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
    if ctx.tool_name not in _GATED_TOOLS:
        return None
    root = find_project_root(
        [ctx.workspace_path, (ctx.args or {}).get("file_path"), (ctx.args or {}).get("path")],
        bound=ctx.workspace_path,
    )
    if root is None:
        return None
    file_path = (ctx.args or {}).get("file_path") or (ctx.args or {}).get("path")
    out = await gate_check_write_async(root, file_path)
    if not out:
        return None
    if out.get("block"):
        return hook_service.HookDecision(block=out["block"])
    return hook_service.HookDecision(notes=list(out.get("notes") or []))


async def _stop_hook(ctx: hook_service.StopContext) -> Optional[hook_service.StopDecision]:
    """stop 语义：证据红 → 拦下收尾 + 纠正反馈续跑（预算由 hook_service 执行）。"""
    root = find_project_root([ctx.workspace_path], bound=ctx.workspace_path)
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

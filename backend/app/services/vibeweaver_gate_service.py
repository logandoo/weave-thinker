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
import hashlib
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

# ---------- source gate pre-tool parity ----------
# vibeweaver-gate.js checks these before a tool runs. They are deliberately
# separate from the post-write artifact gate: a user-approved risk action can
# proceed, but an unapproved one must not land in the first place.
RISK_PATH_RE = re.compile(
    r"(^|/)(auth|security|payments?|billing|crypto|migrations?|permissions?|acl)(/|\.|_|$)",
    re.IGNORECASE,
)
_CODE_EXT_RE = re.compile(
    r"\.(?:py|js|mjs|ts|tsx|jsx|go|rs|java|c|cc|cpp|h|hpp|rb|php|cs|sh|bash|ps1|"
    r"vue|svelte|sql|kt|swift)$",
    re.IGNORECASE,
)
_PROTECTED_EVIDENCE = {
    "tests/acceptance.md",
    "tests/project_profile.json",
    "tests/debt_baseline.json",
    "tests/.evidence.lock",
}
_DESTRUCTIVE_ALLOW_RE = re.compile(
    r"(^|/)(node_modules|\.venv|venv|dist|build|target|coverage|\.next|\.pytest_cache|__pycache__)(/|$)"
    r"|^(\$[A-Za-z0-9_]*(/|$)|\/tmp(\/|$))"
)
_EVIDENCE_ENTRY_RE = re.compile(r"^- (?:iter \d+ (?:PASS|FAIL)|final-run:|assert-run:)", re.M)

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

def _canonical_assert_script() -> Optional[Path]:
    try:
        src = get_config().resolve_resource(
            "skills", "vibeweaver", "scripts", "assert_artifacts.py", bundled_first=True
        )
        return src if src.is_file() else None
    except Exception:
        return None


def _checker_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _canonical_checker_pin(src: Path) -> str:
    try:
        return (src.parent / "assert_artifacts.sha256").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return ""


def _legacy_resource_resolver_missing() -> bool:
    """仅旧测试/嵌入宿主持有简化 Config 时允许 legacy，生产资源缺失不放行。"""
    try:
        get_config().resolve_resource
    except AttributeError:
        return True
    except Exception:
        return False
    return False


def _ensure_assert_script(root: Path) -> Optional[Path]:
    """返回 pin 校验通过的 checker；已有自定义文件不被覆盖。

    新工作区物化 bundled canonical。已有 checker 只有 digest 与 bundled pin
    完全一致才执行；旧版/被修改的 checker 保留在原位，但执行 bundled
    fallback，避免旧规则或篡改内容成为完成裁判。
    """
    src = _canonical_assert_script()
    target = root / "tests" / "assert_artifacts.py"
    if src is None:
        if target.exists() and _legacy_resource_resolver_missing():
            logger.warning("vibeweaver gate: simplified host has no resource resolver; using legacy fixture checker")
            return target
        return None
    pin = _canonical_checker_pin(src)
    if not pin:
        # 只有简化宿主（无资源解析器）可走 legacy fixture；生产缺 pin 一律阻断。
        if target.exists() and _legacy_resource_resolver_missing():
            logger.warning("vibeweaver gate: simplified host has no resource resolver; using legacy fixture checker")
            return target
        if target.exists():
            return None
        logger.warning("vibeweaver gate: canonical checker pin is absent; materialization is for inspection only, execution stays blocked")
    else:
        if _checker_digest(src) != pin:
            logger.warning("vibeweaver gate: bundled assert_artifacts.py fails its pin")
            return None
        if target.exists() and _checker_digest(target) != pin:
            logger.warning(
                "vibeweaver gate: workspace checker differs from canonical pin; "
                "executing bundled canonical checker without overwriting it"
            )
        # Execute the immutable bundled checker whenever the pin is valid.
        # Returning the writable workspace copy here made the gate a TOCTOU
        # target: a concurrent writer could replace tests/assert_artifacts.py
        # after the digest check and before execution.
        if not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.read_bytes())
                logger.info("vibeweaver gate: materialized tests/assert_artifacts.py into %s", root)
            except OSError as exc:
                logger.warning("vibeweaver gate: cannot materialize assert_artifacts.py: %s", exc)
        return src
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
        logger.info("vibeweaver gate: materialized tests/assert_artifacts.py into %s", root)
        return target
    except OSError as exc:
        logger.warning("vibeweaver gate: cannot materialize assert_artifacts.py: %s", exc)
        return None


def _assert_json(output: str) -> Optional[Dict[str, Any]]:
    """读取 checker 的 JSON 终行（class 前缀行是允许的）。"""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None


def _assert_warnings(payload: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("fails"), list):
        return []
    out: List[str] = []
    for item in payload["fails"]:
        if not isinstance(item, dict):
            continue
        if str(item.get("severity") or "block").strip().lower() != "warn":
            continue
        msg = str(item.get("detail") or item.get("code") or "").strip()
        if msg:
            out.append(msg)
    return out


async def run_assert(
    root: Path,
    runner: str,
    script: Optional[Path] = None,
    allow_legacy_unpinned: bool = False,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """跑 pin 校验过的 checker（4 flag 组合），返回 (ok, attempts)。

    退出码只是辅助信号：有效 JSON `ok=true` 才可变绿；block/warn 直接采用
    checker severity，缺失或畸形 JSON 永远 blocking，绝不落到宽松提示。
    """
    attempts: List[Dict[str, Any]] = []
    script = script or (root / "tests" / "assert_artifacts.py")
    for flags in FLAG_COMBOS:
        try:
            proc = await asyncio.create_subprocess_exec(
                runner, str(script), "--root", str(root), *flags, "--json",
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            stdout = out.decode("utf-8", "replace").strip()
            stderr = err.decode("utf-8", "replace").strip()
            output = (stdout + ("\n" + stderr if stderr else "")).strip()
            payload = _assert_json(stdout)
            if proc.returncode == 0 and payload and payload.get("ok") is True:
                return True, [{
                    "flags": flags,
                    "output": output,
                    "failures": [],
                    "warnings": _assert_warnings(payload),
                }]
            if proc.returncode == 0 and payload is None and allow_legacy_unpinned:
                return True, [{
                    "flags": flags,
                    "output": "legacy unpinned checker: exit 0 (no JSON)",
                    "failures": [],
                    "legacy": True,
                }]
            failures = payload.get("fails") if payload and isinstance(payload.get("fails"), list) else None
            if failures is None:
                failures = [{"severity": "block", "detail": output or f"exit {proc.returncode}"}]
            attempts.append({"flags": flags, "output": output, "failures": failures})
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            attempts.append({"flags": flags, "output": "assert_artifacts.py timeout (15s)", "failures": [
                {"severity": "block", "detail": "assert_artifacts.py timeout (15s)"}
            ]})
        except OSError as exc:
            attempts.append({"flags": flags, "output": f"assert run failed: {exc}", "failures": [
                {"severity": "block", "detail": f"assert run failed: {exc}"}
            ]})
    return False, attempts


def _failure_messages(attempts: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    seen = set()
    messages: List[Tuple[str, str]] = []
    for a in attempts:
        failures = a.get("failures")
        if isinstance(failures, list):
            for item in failures:
                if not isinstance(item, dict):
                    continue
                msg = str(item.get("detail") or item.get("code") or "assert failed").strip()
                severity = str(item.get("severity") or "block").strip().lower()
                key = (msg, severity)
                if msg and key not in seen:
                    seen.add(key)
                    messages.append(key)
        else:
            for line in (a.get("output") or "").splitlines():
                m = line.strip()
                if not m.startswith("- "):
                    continue
                key = (m[2:], "")
                if key not in seen:
                    seen.add(key)
                    messages.append(key)
    if not messages and attempts:
        messages.append(((attempts[-1].get("output") or "")[:400], "block"))
    return messages


def _classify(messages: List[Any]) -> Tuple[List[str], List[str]]:
    blocking, warnings = [], []
    for item in messages:
        msg, severity = item if isinstance(item, tuple) else (str(item), "")
        severity = str(severity or "").strip().lower()
        if severity == "warn":
            warnings.append(msg)
        elif severity:
            # Upstream treats every non-warn severity as blocking. Unknown /
            # future severity values must never silently downgrade a failure.
            blocking.append(msg)
        elif any(h in msg for h in BLOCKING_HINTS):
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
    script = _ensure_assert_script(root)
    runner = shutil.which("python3")
    if script is not None and runner:
        canonical = _canonical_assert_script()
        if canonical is not None and not _canonical_checker_pin(canonical) and not _legacy_resource_resolver_missing():
            return {"blocking": ["canonical checker pin is missing — completion verification is blocked (fail-closed)"], "warnings": []}
        ok, attempts = await run_assert(
            root, runner, script, allow_legacy_unpinned=_legacy_resource_resolver_missing()
        )
        if ok:
            warnings = [
                warning
                for attempt in attempts
                for warning in (attempt.get("warnings") or [])
            ]
            return {"blocking": [], "warnings": warnings} if warnings else None
        blocking, warnings = _classify(_failure_messages(attempts))
        return {
            "blocking": blocking,
            "warnings": warnings,
            "attempts": ["[" + " ".join(a["flags"]) + "]" for a in attempts],
        }
    if _legacy_resource_resolver_missing():
        failures = inline_check(root)
        if failures:
            return {"blocking": failures, "warnings": [], "inline": True}
        return None
    return {
        "blocking": ["canonical assert_artifacts.py or a valid checker pin is unavailable — completion verification is blocked (fail-closed)"],
        "warnings": [],
    }


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


def _relative_under_root(root: Path, file_path: Optional[str]) -> str:
    if not file_path:
        return ""
    try:
        p = Path(file_path).expanduser()
        if not p.is_absolute():
            p = Path(root) / p
        rel = p.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return ""
    return rel.as_posix()


def _decisions_text(root: Path) -> str:
    try:
        return (root / "tests" / "decisions.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _risk_approved(root: Path, target: str) -> bool:
    if not target:
        return False
    rel = target.replace("\\", "/").rstrip("/")
    for line in _decisions_text(root).splitlines():
        m = re.search(r"(?:risk-approved|destructive-approved):\s*(.+)", line, re.IGNORECASE)
        if not m:
            continue
        approved = re.split(r"\s+[—–-]{1,2}\s+", m.group(1).strip())[0]
        approved = approved.strip("`\"' ")
        if not approved:
            continue
        # Token-exact path/command matching. Substring or basename matching
        # let `backend/authz` approve `backend/auth`, or a single file approve
        # its whole parent directory.
        if approved == rel:
            return True
    return False


def risk_path_block(root: Path, file_path: Optional[str]) -> Optional[str]:
    """Source risk-path gate: risk-tier writes need a decision line first."""
    if os.environ.get("VIBEWEAVER_RISK_GATE") == "off":
        return None
    rel = _relative_under_root(root, file_path)
    if not rel or not RISK_PATH_RE.search(rel):
        return None
    if _risk_approved(root, rel):
        return None
    return (
        f"GATE-BLOCKED (vibeweaver risk-path gate): {rel} matches a risk-tier path "
        "(auth/security/payment/billing/crypto/migration/permission/acl). Add a user-approved "
        f"line to tests/decisions.md: `- risk-approved: {rel} — <why>` (Class-E), or set "
        "VIBEWEAVER_RISK_GATE=off with user consent."
    )


def risk_command_block(root: Path, command: Optional[str]) -> Optional[str]:
    if os.environ.get("VIBEWEAVER_RISK_GATE") == "off" or not command:
        return None
    m = re.search(r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|git\s+rm)\s+(?:-[^\s]+\s+)*([^;&|]+)", command)
    if not m:
        return None
    for target in m.group(1).split():
        if target.startswith("-"):
            continue
        rel = target.strip("'\"").replace("\\", "/")
        if RISK_PATH_RE.search(rel) and not _risk_approved(root, rel):
            return (
                f"GATE-BLOCKED (vibeweaver risk-path gate): deleting risk-tier path {rel} "
                f"requires a user-approved `- risk-approved: {rel} — <why>` line in tests/decisions.md."
            )
    return None


def _safe_delete_target(root: Path, target: str) -> bool:
    """Allow only clearly disposable build/vendor paths; reject traversal/$VAR."""
    if not target or target.startswith("$") or ".." in Path(target).parts:
        return False
    raw = target.replace("\\", "/")
    if not _DESTRUCTIVE_ALLOW_RE.search(raw):
        return False
    try:
        p = Path(target)
        if not p.is_absolute():
            p = Path(root) / p
        p.resolve()
    except (OSError, ValueError):
        return False
    return True


def _readonly_command(command: str) -> bool:
    """Commands that only inspect text/data are not destructive."""
    c = command.strip()
    first = c.split(None, 1)[0] if c else ""
    if not re.match(r"^(?:rg|grep|egrep|fgrep|cat|head|tail|less|more|sed|awk|echo|printf|git)$", first):
        return False
    if first == "git":
        rest = c.split(None, 2)
        if len(rest) < 2 or rest[1] not in {"log", "diff", "show", "grep", "status"}:
            return False
    # A separator can introduce a second, destructive command.
    return not re.search(r"(?:;|\|\||&&|\n)\s*(?:rm|git\s+(?:add|reset|clean|checkout|restore|stash)|truncate)\b", c)


def _strip_heredoc(command: str) -> str:
    """Remove heredoc bodies before destructive-token matching."""
    return re.sub(
        r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?[\s\S]*?\n\1\b",
        "",
        command,
    )


def destructive_block(root: Path, command: Optional[str]) -> Optional[str]:
    """Source destructive-command guard, before the terminal executes."""
    if os.environ.get("VIBEWEAVER_DESTRUCTIVE_GATE") == "off" or not command:
        return None
    command = _strip_heredoc(command)
    if _readonly_command(command):
        return None
    if _risk_approved(root, command):
        return None
    c = command.strip()
    if re.search(r"git\s+add\s+(?:-A|--all)\b", c):
        return (
            "GATE-BLOCKED (destructive guard): `git add -A` stages ignored/vendor trees "
            "(45k-path incident). Use `git add -u` + explicit paths, or add "
            "`- destructive-approved: git add -A — <why>`."
        )
    if re.search(r"git\s+reset\s+--hard\b", c):
        return "GATE-BLOCKED (destructive guard): `git reset --hard` discards uncommitted work. Add a Class-E decision line or use a stash."
    if re.search(r"git\s+clean\b.*(?:-f|--force)", c, re.IGNORECASE):
        return "GATE-BLOCKED (destructive guard): `git clean -f/--force` deletes untracked files. Add a Class-E decision line first."
    if re.search(r"git\s+(?:checkout|restore)\s+--\s+\.", c, re.IGNORECASE):
        return "GATE-BLOCKED (destructive guard): `git checkout/restore -- .` discards the whole worktree. Add a Class-E decision line first."
    if re.search(r"git\s+stash\b.*(?:-u|--include-untracked)", c, re.IGNORECASE):
        return "GATE-BLOCKED (destructive guard): `git stash -u/--include-untracked` removes untracked work. Add a Class-E decision line first."
    if re.search(r"\btruncate\s+", c, re.IGNORECASE):
        return "GATE-BLOCKED (destructive guard): `truncate` destroys file content. Add a Class-E decision line first."
    m = re.search(
        r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:git\s+)?rm\s+([^\s;&|]+(?:\s+[^\s;&|]+)*)",
        c,
        re.IGNORECASE,
    )
    if m:
        parts = m.group(1).split()
        flags = [x for x in parts if x.startswith("-")]
        recursive = any(
            x in ("--recursive", "-r", "-R")
            or (not x.startswith("--") and "r" in x.lower())
            for x in flags
        )
        targets = [x.strip("'\"") for x in parts if not x.startswith("-")]
        bad = [target for target in targets if recursive and not _safe_delete_target(root, target)]
        if bad:
            return (
                "GATE-BLOCKED (destructive guard): recursive delete of non-allowlisted path(s): "
                + ", ".join(bad)
                + ". Add `- destructive-approved: <command> — <why>` for a Class-E action."
            )
    return None


def doc_class_block(root: Path, file_path: Optional[str]) -> Optional[str]:
    """Source class guard: a DOC task may not write code files."""
    rel = _relative_under_root(root, file_path)
    if not rel or not _CODE_EXT_RE.search(rel):
        return None
    try:
        log = (root / "tests" / "verification_log.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parts = [p for p in re.split(r"^(?=## )", log, flags=re.MULTILINE) if p.strip()]
    class_marker = re.compile(r"(?m)^- class:")
    marker = re.compile(
        r"^- (?:iter \d+ (?:PASS|FAIL)|final-run:|assert-run:)|\[Verification Gate\]"
    )
    block = ""
    # Prefer the last block that actually declares a class; gate tokens in a
    # trailing notes section must not mask the DOC task.
    for part in reversed(parts):
        if class_marker.search(part):
            block = part
            break
    if not block:
        for part in reversed(parts):
            if marker.search(part):
                block = part
                break
    if not block:
        block = parts[-1] if parts else log
    if not re.search(r"^- class:\s*DOC\b", block, re.MULTILINE):
        return None
    return (
        "GATE-BLOCKED (vibeweaver class guard): the current task block declares `- class: DOC` "
        f"but this write targets a code file ({Path(rel).name}). DOC is prose/office only; "
        "reclassify the task (CODE/CONFIG) before writing code."
    )


def _evidence_lock_script(root: Path) -> Optional[Path]:
    try:
        src = get_config().resolve_resource(
            "skills", "vibeweaver", "scripts", "evidence_lock.py", bundled_first=True
        )
        if src.is_file():
            return src
    except Exception:
        pass
    for candidate in (root / "scripts" / "evidence_lock.py", root / "tests" / "evidence_lock.py"):
        if candidate.is_file():
            return candidate
    return None


async def _run_evidence_lock(script: Path, root: Path, args: List[str]) -> Dict[str, Any]:
    runner = shutil.which("python3")
    if not runner:
        return {"code": 1, "out": "python3 unavailable"}
    try:
        proc = await asyncio.create_subprocess_exec(
            runner,
            str(script),
            "--root",
            str(root),
            *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        return {
            "code": int(proc.returncode),
            "out": (out.decode("utf-8", "replace") + err.decode("utf-8", "replace")).strip(),
        }
    except (OSError, asyncio.TimeoutError) as exc:
        return {"code": 1, "out": f"evidence_lock.py failed: {exc}"}


def _current_task_evidence_block(log_text: str) -> str:
    """Last task block carrying iter/final/assert evidence (not trailing notes)."""
    if not log_text:
        return ""
    parts = [p for p in re.split(r"^(?=## )", log_text, flags=re.MULTILINE) if p.strip()]
    marker = re.compile(r"(?m)^- (?:iter \d+ (?:PASS|FAIL)|final-run:|assert-run:)")
    for part in reversed(parts):
        if marker.search(part):
            return part
    return ""


async def check_protected_evidence_async(root: Path, file_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Source protected-evidence lock check (runs after write, like gate.js)."""
    rel = _relative_under_root(root, file_path)
    if rel not in _PROTECTED_EVIDENCE:
        return None
    if rel == "tests/.evidence.lock":
        return {
            "block": "GATE-BLOCKED (vibeweaver evidence lock): tests/.evidence.lock is written ONLY by "
            "scripts/evidence_lock.py — direct edits are a forgery path. Use `--init` / "
            "`--update-acceptance --reason <why> --approved` / `--seal-log`."
        }
    script = _evidence_lock_script(root)
    if script is None:
        return {
            "notes": [
                "[GATE-WARNING (vibeweaver)] evidence lock script not installed — "
                "protected evidence unchecked (legacy layout); install the canonical runtime pack"
            ]
        }
    lock_path = root / "tests" / ".evidence.lock"
    try:
        log = (root / "tests" / "verification_log.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    current_evidence = _current_task_evidence_block(log)
    if not lock_path.exists():
        if _EVIDENCE_ENTRY_RE.search(current_evidence):
            return {
                "block": "GATE-BLOCKED (vibeweaver evidence lock): tests/.evidence.lock is missing "
                "AFTER task evidence exists — this is a tamper signal, not a bootstrap. Restore it from git, "
                "or run `python3 scripts/evidence_lock.py --root . --init --force --approved` (Class-E) "
                "to re-baseline."
            }
        init = await _run_evidence_lock(script, root, ["--init"])
        if init["code"] != 0:
            return {"block": f"GATE-BLOCKED (vibeweaver evidence lock): could not initialize {lock_path}\n{init['out']}"}
        return None
    checked = await _run_evidence_lock(script, root, ["--check", "--rebase-new-task"])
    if checked["code"] != 0:
        # Before the current task has any iter/final evidence, acceptance is
        # still a draft: allow pre-evidence refinement without a Class-E
        # approval. Once evidence exists, the contract is frozen and any
        # change must go through evidence_lock.py --update-acceptance.
        if not _EVIDENCE_ENTRY_RE.search(current_evidence):
            reinit = await _run_evidence_lock(script, root, ["--init", "--force", "--approved"])
            if reinit["code"] == 0:
                return None
        return {
            "block": "GATE-BLOCKED (vibeweaver evidence lock): protected evidence changed without a licensed update.\n"
            + checked["out"]
            + f"\nFix: python3 {script} --root . --update-acceptance --reason \"<why>\" --approved (Class-E) "
            "for acceptance; restore the file for profile/debt; --seal-log after appending the log."
        }
    return None


async def gate_check_write_async(root: Path, file_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """一次落盘后的门禁评估。返回 {block} | {notes} | None。"""
    try:
        if not get_config().vibeweaver_gate_enabled:
            return None
    except Exception:
        return None
    protected = await check_protected_evidence_async(root, file_path)
    if protected:
        return protected
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

async def _tool_before_hook(ctx: hook_service.ToolHookContext) -> Optional[hook_service.HookDecision]:
    """Pre-tool risk/destructive/class guard (upstream gate beforeToolCheck)."""
    if not ctx.vibeweaver_active:
        return None
    try:
        if not get_config().vibeweaver_gate_enabled:
            return None
    except Exception:
        return None
    key = ctx.conversation_id or ctx.session_id
    if ctx.tool_name in _GATED_TOOLS:
        file_path = (ctx.args or {}).get("file_path") or (ctx.args or {}).get("path")
        root = resolve_session_root(
            ctx.workspace_path,
            key,
            starts=[file_path],
            prefer_hints=True,
        )
        if root is None:
            return None
        rel = _relative_under_root(root, file_path)
        if rel == "tests/.evidence.lock":
            return hook_service.HookDecision(
                block="GATE-BLOCKED (vibeweaver evidence lock): tests/.evidence.lock is written ONLY "
                "by scripts/evidence_lock.py — direct edits are a forgery path. Use `--init` / "
                "`--update-acceptance --reason <why> --approved` / `--seal-log`."
            )
        block = doc_class_block(root, file_path) or risk_path_block(root, file_path)
        return hook_service.HookDecision(block=block) if block else None
    if ctx.tool_name in ("terminal", "process"):
        command = str((ctx.args or {}).get("command") or "")
        root = resolve_session_root(ctx.workspace_path, key)
        if root is None or not command:
            return None
        block = risk_command_block(root, command) or destructive_block(root, command)
        return hook_service.HookDecision(block=block) if block else None
    return None


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
    summary = {"gate_tool_before_hooks": 0, "gate_tool_hooks": 0, "gate_stop_hooks": 0}
    if _installed:
        return summary
    try:
        if not (get_config().vibeweaver_enabled and get_config().vibeweaver_gate_enabled):
            return summary
    except Exception:
        return summary
    hook_service.register_tool_hook("before", _tool_before_hook, name="vibeweaver_gate_guard")
    hook_service.register_tool_hook("after", _tool_after_hook, name="vibeweaver_gate")
    hook_service.register_stop_hook(_stop_hook, name="vibeweaver_gate")
    _installed = True
    summary["gate_tool_before_hooks"] = 1
    summary["gate_tool_hooks"] = 1
    summary["gate_stop_hooks"] = 1
    logger.info("vibeweaver gate hooks installed")
    return summary

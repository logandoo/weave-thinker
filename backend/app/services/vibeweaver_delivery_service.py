# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""vibeweaver 确定性投递服务（D4/D5）— working note 快照 / 压缩恢复包 / 首触 cue / cue triggers。

方案定位（wave21/24 迁移）：依赖模型自愿回读工作笔记本的通道实测趋近于零；
**harness 拥有上下文窗口，投递就必须由 harness 执行**。本模块把三件事做成机制：

1. **compactContext**：每次上下文压缩完成时——① 把任务状态包（≤4KB）并入压缩摘要
   prompt；② 压缩后的消息流注入一条 synthetic 恢复包（第一动作=重读指引）；
   ③ 写入 pendingResume 标记，供下一轮 system 装配一次性注入（compactResume）。
2. **快照**（wave24 删源模式）：`.vibeweaver/memory-snapshot.json` ≤4KB，源文件
   `tests/working_note.md` 消失后快照即权威（投递必须活过源删除）。
3. **首触 cue**：本会话第一个受门 write 的 tool.after 追加一行 resume 指针（每会话
   一次）；并刷新快照（在任何删除窗之前捕获）。

D5 **cue triggers**（§A7.16）：扫描 `memory/*.md` frontmatter `triggers:` glob，
编辑路径命中时投递 ≤3 条一行 cue（safeGlob 上限防 ReDoS——wave16 教训：记忆主题是
被编译成正则的代理文本）；`memory/` 自身写入不触发；命中计数入 `cue-stats.json`。

全部 fail-open：投递失败绝不阻断写入/压缩。计数入 `.vibeweaver/state.json`
（memoryDeliveries 三分支）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services import vibeweaver_state

logger = logging.getLogger(__name__)

MEMORY_BUNDLE_MAX = 4096
SNAPSHOT_FILE = "memory-snapshot.json"
DELIVERY_STATE_FILE = "delivery-state.json"
CUE_MAX = 3
CUE_STATS_CAP = 100
_PENDING_CAP = 50
_SESSION_CUE_CAP = 500

_SAFE_GLOB_MAX_LEN = 200
_SAFE_GLOB_MAX_DOUBLE_STAR = 2

# 首触 cue 的进程内去重（root:conv → 已投递）；有界
_SESSION_MEM: Dict[str, bool] = {}


def reset_state() -> None:
    """测试隔离用。"""
    _SESSION_MEM.clear()


# ---------------- 基础 ----------------

def _safe_read(p: Path) -> str:
    try:
        if p.exists() and p.stat().st_size > 0:
            return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _rel_posix(root: Path, file_path: Optional[str]) -> str:
    if not file_path:
        return ""
    try:
        p = Path(file_path).expanduser()
        if not p.is_absolute():
            p = root / p
        rel = p.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return ""
    return rel.as_posix()


def rel_posix(root: Path, file_path: Optional[str]) -> str:
    """项目相对路径（posix）；越界/不可解析 → ""。"""
    return _rel_posix(root, file_path)


def working_note_of(root: Path) -> str:
    return _safe_read(root / "tests" / "working_note.md").replace("\r\n", "\n")


def is_vibeweaver_active(root: Path) -> bool:
    """vibeweaver-active 判定（与 gate/audit 同款：tests/verification_log.md 存在）。"""
    try:
        return (root / "tests" / "verification_log.md").exists()
    except OSError:
        return False


# ---------------- 快照（wave24） ----------------

def snapshot_working_note(root: Path) -> None:
    """把 working note 复制进 harness 快照（源删除后投递仍活）。"""
    try:
        text = working_note_of(root)
        if not text.strip():
            return
        p = root / vibeweaver_state.STATE_DIR / SNAPSHOT_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"rel": "tests/working_note.md", "text": text[:MEMORY_BUNDLE_MAX], "t": int(time.time() * 1000)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError as exc:
        logger.debug("working-note snapshot failed-open: %s", exc)


def snapshot_text_of(root: Path) -> str:
    try:
        d = json.loads((root / vibeweaver_state.STATE_DIR / SNAPSHOT_FILE).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            return d["text"]
    except (OSError, ValueError):
        pass
    return ""


# ---------------- 状态包（JS compactStateBundle 三分支） ----------------

def compact_state_bundle(root: Path) -> str:
    """任务状态包（≤4KB）：note 原文 → 快照 → 创建指引（三分支）。"""
    note = working_note_of(root).strip()
    from_snapshot = False
    if note:
        snapshot_working_note(root)
    else:
        note = snapshot_text_of(root).strip()
        from_snapshot = bool(note)
    if note and from_snapshot:
        return "\n".join([
            "VIBEWEAVER WORKING-STATE (harness snapshot — the source tests/working_note.md "
            "is no longer present; this copy is authoritative):",
            note[: MEMORY_BUNDLE_MAX - 400],
            "Resume rule: re-read tests/acceptance.md and the last 20 lines of "
            "tests/verification_log.md BEFORE the next edit; the working note lives in this "
            "harness snapshot now.",
        ])
    if not note:
        acc = _safe_read(root / "tests" / "acceptance.md")
        cap_line = ""
        for line in acc.split("\n"):
            if "cap=5" in line:
                cap_line = line.strip()
                break
        parts = [
            "VIBEWEAVER COMPACT-RESUME (machine-delivered — no tests/working_note.md exists):",
            "After this compaction, BEFORE the next edit: re-read tests/acceptance.md and the "
            "last 20 lines of tests/verification_log.md, and CREATE tests/working_note.md "
            "(§A7.15 hypothesis ledger: confirmed/doubted/excluded/open + key locations + "
            "dead ends + next action, <=50 lines). The harness re-delivers this note at every "
            "compaction boundary.",
        ]
        if cap_line:
            parts.append(f"Acceptance first line: {cap_line}")
        return "\n".join(parts)[:MEMORY_BUNDLE_MAX]
    return "\n".join([
        "VIBEWEAVER WORKING-STATE (machine-delivered; this ledger is task state and takes "
        "precedence over the compacted summary — preserve its essentials):",
        note[: MEMORY_BUNDLE_MAX - 400],
        "Resume rule: after compaction re-read tests/working_note.md, tests/acceptance.md, "
        "and the last 20 lines of tests/verification_log.md BEFORE the next edit.",
    ])


# ---------------- pendingResume（跨轮消费） ----------------

def _delivery_state_path(root: Path) -> Path:
    return root / vibeweaver_state.STATE_DIR / DELIVERY_STATE_FILE


def mark_pending_resume(root: Path, conversation_id: str) -> None:
    """标记「下一轮 system 装配注入一次」。会话键缺失 → 不标记（避免根级兜底键
    被无关会话消费掉「一次性」语义，评审 B-m3）。"""
    if not root or not conversation_id:
        return
    try:
        p = _delivery_state_path(root)
        try:
            st: Dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                st = {}
        except (OSError, ValueError):
            st = {}
        pending = st.get("pendingResume")
        if not isinstance(pending, dict):
            pending = {}
        key = str(conversation_id)
        pending.pop(key, None)  # 重标记刷新 recency（cap 逐出按最旧，评审 B-m4）
        pending[key] = int(time.time() * 1000)
        if len(pending) > _PENDING_CAP:
            pending = dict(list(pending.items())[-_PENDING_CAP:])
        st["pendingResume"] = pending
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        logger.debug("pending resume mark failed-open: %s", exc)


def consume_pending_resume(root: Path, conversation_id: str) -> Optional[str]:
    """消费 pending（一次性，仅精确会话键）：返回恢复包文本；无 pending → None。"""
    if not root or not conversation_id:
        return None
    key = str(conversation_id)
    try:
        p = _delivery_state_path(root)
        try:
            st: Dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                return None
        except (OSError, ValueError):
            return None
        pending = st.get("pendingResume")
        if not isinstance(pending, dict):
            return None
        if key not in pending:
            return None
        pending.pop(key, None)
        st["pendingResume"] = pending
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        vibeweaver_state.record_memory_delivery(root, "compactResume")
        return compact_state_bundle(root)
    except OSError as exc:
        logger.debug("pending resume consume failed-open: %s", exc)
        return None


def complete_compact_delivery(root: Path, conversation_id: str = "") -> str:
    """压缩成功后：投递计数 + pendingResume 标记；返回本次投递的状态包。"""
    bundle = compact_state_bundle(root)
    vibeweaver_state.record_memory_delivery(root, "compactContext")
    mark_pending_resume(root, conversation_id)
    return bundle


def prepare_compact_delivery(root_str: Optional[str], conversation_id: str = "") -> str:
    """压缩完成点统一入口（compressor 调用；root 非 vibeweaver-active → ""）。"""
    if not root_str:
        return ""
    try:
        root = Path(root_str)
        if not is_vibeweaver_active(root):
            return ""
        return complete_compact_delivery(root, conversation_id)
    except Exception:
        return ""


# ---------------- 首触 cue ----------------

def resume_cue_note(root: Path) -> Optional[str]:
    if working_note_of(root).strip():
        return (
            "[vibeweaver] tests/working_note.md exists (task state ledger) — read it first "
            "(§3.3 re-entry); the harness re-delivers it at every compaction boundary."
        )
    if (root / "tests" / "acceptance.md").exists():
        return (
            "[vibeweaver] active task detected without tests/working_note.md — create it "
            "(§A7.15); the harness delivers it deterministically at every compaction boundary."
        )
    return None


def first_touch_cue(root: Path, session_key: str) -> Optional[str]:
    """会话首个受门 write 的一次性 resume 指针（快照先行）。"""
    try:
        key = f"{root}:{session_key or '(session)'}"
        if _SESSION_MEM.get(key):
            return None
        note = resume_cue_note(root)
        if not note:
            return None
        snapshot_working_note(root)  # 任何删除窗之前捕获
        if len(_SESSION_MEM) >= _SESSION_CUE_CAP:
            for k in list(_SESSION_MEM.keys())[:100]:
                _SESSION_MEM.pop(k, None)
        _SESSION_MEM[key] = True
        vibeweaver_state.record_memory_delivery(root, "sessionCue")
        return note
    except Exception:
        return None


def note_note_write(root: Path, rel_path: str) -> None:
    """working_note 写入后的 tool.after 刷新点。"""
    if rel_path.replace("\\", "/") == "tests/working_note.md":
        snapshot_working_note(root)


# ---------------- D5：cue triggers（§A7.16 + safeGlob） ----------------

def safe_glob(t: Any) -> bool:
    """投递安全上限：记忆主题文本会被编译成正则同步执行——恶意/疏忽的 glob
    （重复 `**/` → 组合式回溯）会楔死事件循环。`**` 组 ≤2、长度 ≤200、拒控制字符
    （它们兼作内部哨兵字节）。"""
    if not isinstance(t, str) or not t:
        return False
    if len(t) > _SAFE_GLOB_MAX_LEN:
        return False
    if re.search(r"[\x00-\x1f]", t):
        return False
    return t.count("**") <= _SAFE_GLOB_MAX_DOUBLE_STAR


def glob_to_regex(glob: str) -> re.Pattern:
    """glob → 正则：`**/` 跨目录、`*` 不跨 `/`、`?` 匹配单字符（锚定项目相对路径）。"""
    g = re.escape(str(glob))
    g = g.replace(r"\*\*/", "\x01")
    g = g.replace(r"\*\*", "\x02")
    g = g.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    g = g.replace("\x01", "(?:.*/)?").replace("\x02", ".*")
    return re.compile("^" + g + "$")


def parse_triggers(frontmatter: str) -> List[str]:
    """解析主题 frontmatter 的 `triggers:`（inline / 列表 / 标量）；缺省 → []。"""
    out: List[str] = []
    lines = frontmatter.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^triggers:\s*(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith("["):
            inner = rest[1:]
            if inner.endswith("]"):
                inner = inner[:-1]
            for part in inner.split(","):
                v = part.strip().strip("\"'")
                if v:
                    out.append(v)
        elif not rest:
            for j in range(i + 1, len(lines)):
                li = re.match(r"^\s*-\s+[\"']?([^\"']+?)[\"']?\s*(?:#.*)?$", lines[j])
                if not li:
                    break
                out.append(li.group(1))
        else:
            scalar = re.sub(r"[\"']", "", rest)
            scalar = re.sub(r"\s+#.*$", "", scalar).strip()
            if scalar:
                out.append(scalar)
        break
    return out


def cue_notes(root: Path, file_path: Optional[str]) -> List[str]:
    """编辑路径命中的记忆主题 cue（一行指针，≤3 条；memory/ 自身写入不触发）。"""
    try:
        rel = _rel_posix(root, file_path)
        if not rel:
            return []
        if rel.split("/")[0].lower() == "memory":
            return []
        mem_dir = root / "memory"
        if not mem_dir.is_dir():
            return []
        notes: List[str] = []
        fired: List[str] = []
        for f in sorted(mem_dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".md") or f.name == "MEMORY.md":
                continue
            text = _safe_read(f).replace("\r\n", "\n").lstrip("\ufeff")
            m = re.match(r"^---\n([\s\S]*?)\n---", text)
            if not m:
                continue
            fm = m.group(1)
            triggers = [t for t in parse_triggers(fm) if t and safe_glob(t)]
            if not triggers:
                continue
            hit = False
            for t in triggers:
                try:
                    if glob_to_regex(t).match(rel):
                        hit = True
                        break
                except re.error:
                    continue
            if not hit:
                continue
            trust_m = re.search(r"^(?:trust|status):\s*(\S+)", fm, re.MULTILINE)
            trust = trust_m.group(1) if trust_m else "?"
            heading_m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            heading = heading_m.group(1) if heading_m else f.name
            notes.append(
                f"[GATE-WARNING (vibeweaver-cue)] memory cue: memory/{f.name} ({trust}) "
                f"matches this path — read it before editing: {heading}"
            )
            fired.append(f.name)
            if len(notes) >= CUE_MAX:
                break
        if fired:
            vibeweaver_state.record_cue_fires(root, fired)
        return notes
    except Exception:
        return []

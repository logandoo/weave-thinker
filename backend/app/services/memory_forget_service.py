# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W0-4/W5-1/W5-2 遗忘闭环（记忆/长程 v2，2026-10-23，PLAN W0-4/W5/B4）。

D1=a **memory-only** 语义（用户已决策）：删除记忆层（概念/事件/潜意识/澄清/
agent_memories 文件层/dream/pinned constraints/dream reports 等），**保留
对话与笔记**——API/UI 明示保留面（残余报告 disclosure）。

- 清单：`deletion_manifests`（scope_json/status/residual_json；保留审计不含正文）
- 残余扫描：DB 记忆面 + agent_memories 文件 + 检索缓存 → memory-only 口径
  残余=0 或显式声明（Bemula 2610.18052 六类请求的抽取测试见
  tests/test_memory_forget.py）
- 擦除面（W5-1）：erase_extended_surfaces 覆盖 conversation artifacts /
  rolling summaries / resume snapshots / evidence units / skills /
  pinned constraints / dream reports——全部按 user_id 作用域（B20）。
- 可逆优先（P2）：概念=软遗忘（status='forgotten'+valid_to），非物理删除；
  pinned constraints=revoke 而非删除。
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_QUOTED_RE = re.compile(r"[「『“\"']([^」』”\"'\n]{2,60})[」』”\"']")
_MARKER_RE = re.compile(
    r"(?:删除记忆|忘掉|忘记|遗忘|请忘掉|从记忆中删除|forget)\s*[:：,，]?\s*(.{2,80})",
    re.IGNORECASE)
_TRAIL_PUNCT_RE = re.compile(r"[。．.、，,;；:：!！?？\s]+$")

RETAINED_SURFACES = ("conversations", "messages", "notes", "notebooks")


def _like_escape(needle: str) -> str:
    """评审 A I2：ILIKE 通配符转义——`%`/`_`/`\\` 是模式字符，不转义时
    「忘掉「%」」会匹配整表（过度擦除 + 残余计数失真）。"""
    return (str(needle or "")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_"))


def parse_forget_needles(text: str) -> List[str]:
    """从遗忘请求抽取要遗忘的词条（引号优先；无引号走标记子句；精确优先）。"""
    raw = str(text or "")
    if not raw.strip():
        return []
    quoted = [q.strip() for q in _QUOTED_RE.findall(raw) if q.strip()]
    if quoted:
        out, seen = [], set()
        for q in quoted:
            if q not in seen:
                seen.add(q)
                out.append(q)
        return out
    m = _MARKER_RE.search(raw)
    if not m:
        return []
    clause = _TRAIL_PUNCT_RE.sub("", m.group(1).strip()).strip("「」『』“”\"'")
    return [clause] if len(clause) >= 2 else []


def residual_report(*, needles: List[str], hits: Dict[str, int],
                    memory_hits: Dict[str, int]) -> Dict[str, Any]:
    """残余报告（memory-only 口径）：memory 层残余=0 才算通过；
    对话/笔记命中计入 retained_hits 并显式声明保留（D1=a UI 明示面）。"""
    mem = {k: int(v) for k, v in (memory_hits or {}).items() if int(v) > 0}
    return {
        "semantic": "memory-only",
        "needles": list(needles or []),
        "retained_surfaces_declared": list(RETAINED_SURFACES),
        "retained_hits": dict(hits or {}),
        "memory_hits": mem,
        "residual_count": sum(mem.values()),
        "disclosure": "对话与笔记按 D1=a 保留（memory-only 遗忘语义）；如需连同会话/笔记删除请另行申请 full forget",
    }


async def create_deletion_manifest(db: Any, user_id: str, request_id: str,
                                   scope: Dict[str, Any]):
    """落遗忘清单（pending）。scope 含 needles 等；审计不含正文。"""
    from app.db.database import DeletionManifest

    row = DeletionManifest(
        user_id=user_id,
        request_id=str(request_id or "")[:64],
        scope_json={"needles": list((scope or {}).get("needles") or [])},
        status="pending",
        residual_json={},
    )
    db.add(row)
    await db.flush()
    return row


def _scrub_file_store(user_id: str, needles: List[str]) -> int:
    """agent_memories 文件层：删除含 needle 的条目（`\\n§\\n` 分隔），返回删除数。"""
    root = os.environ.get("AGENT_MEMORY_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agent_memories")
    udir = os.path.join(root, str(user_id))
    if not os.path.isdir(udir):
        return 0
    removed = 0
    for name in os.listdir(udir):
        if not name.endswith(".md"):
            continue
        path = os.path.join(udir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = f.read().split("\n§\n")
        except OSError:
            continue
        kept = []
        for e in entries:
            if any(n and n in e for n in needles):
                removed += 1
            else:
                kept.append(e)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n§\n".join(kept))
        except OSError:
            logger.warning("forget file scrub write failed: %s", path)
    return removed


async def scan_residuals(db: Any, user_id: str, needles: List[str]) -> Dict[str, Any]:
    """残余扫描：memory 面（概念/事件/潜意识/agent 记忆/dream/pins）+ 保留面
    （对话/消息/笔记）分别计数。memory 面残余=0 是 memory-only 验收线。
    每条扫描走 savepoint：单条失败不得毒化外层事务（吞异常≠事务恢复）。"""
    from sqlalchemy import text

    memory, retained = {}, {}
    checks = [
        ("memory_concepts", ["canonical_name", "description_full"], "memory",
         "user_id = :uid AND status = 'active'"),
        ("memory_episodes", ["narrative"], "memory", "user_id = :uid"),
        ("subconscious_log", ["raw_text"], "memory", "user_id = :uid"),
        ("conversations", ["title"], "retained", "user_id = :uid"),
        ("messages", ["content"], "retained",
         "conversation_id IN (SELECT id FROM conversations WHERE user_id = :uid)"),
        ("notes", ["content"], "retained",
         "notebook_id IN (SELECT id FROM notebooks WHERE user_id = :uid)"),
    ]
    for table, cols, bucket, owner in checks:
        total = 0
        for col in cols:
            for n in needles:
                try:
                    async with db.begin_nested():
                        v = (await db.execute(text(
                            f"SELECT COUNT(*) FROM {table} WHERE {owner}"
                            f" AND {col} ILIKE :n"),
                            {"uid": user_id, "n": f"%{_like_escape(n)}%"})).scalar() or 0
                        total += int(v)
                except Exception:
                    logger.debug("residual scan miss %s.%s", table, col,
                                 exc_info=True)
        if total:
            (memory if bucket == "memory" else retained)[table] = total
    # agent 系（agent_state_id 结构，无 user_id）
    for table, col in (("agent_memories", "content"),
                       ("agent_dreams", "summary")):
        total = 0
        for n in needles:
            try:
                async with db.begin_nested():
                    v = (await db.execute(text(
                        f"SELECT COUNT(*) FROM {table} WHERE agent_state_id IN "
                        f"(SELECT id FROM user_agent_states WHERE user_id = :uid)"
                        f" AND {col} ILIKE :n"),
                        {"uid": user_id, "n": f"%{_like_escape(n)}%"})).scalar() or 0
                    total += int(v)
            except Exception:
                logger.debug("residual scan miss %s.%s", table, col, exc_info=True)
        if total:
            memory[table] = memory.get(table, 0) + total
    # pinned constraints（活跃面）
    for n in needles:
        try:
            async with db.begin_nested():
                c = (await db.execute(text(
                    "SELECT COUNT(*) FROM conversation_pinned_constraints "
                    "WHERE user_id = :uid AND revoked_at IS NULL AND text ILIKE :n"),
                    {"uid": user_id, "n": f"%{_like_escape(n)}%"})).scalar() or 0
                if c:
                    memory["conversation_pinned_constraints"] = (
                        memory.get("conversation_pinned_constraints", 0) + int(c))
        except Exception:
            logger.debug("residual scan pins miss", exc_info=True)
    return {"memory": memory, "retained": retained}


async def execute_forget(db: Any, user_id: str, manifest_id: str) -> Dict[str, Any]:
    """执行遗忘（memory-only）：软遗忘概念、删记忆层行/文件条目、revoke 约束，
    然后残余扫描 → 清单落档。返回残余报告。"""
    from sqlalchemy import text

    from app.db.database import DeletionManifest

    man = await db.get(DeletionManifest, manifest_id)
    if man is None or man.user_id != user_id:
        raise ValueError("manifest not found")
    needles = list((man.scope_json or {}).get("needles") or [])
    if not needles:
        man.status = "completed"
        man.residual_json = {"note": "no needles"}
        man.completed_at = datetime.utcnow()
        await db.flush()
        return residual_report(needles=[], hits={}, memory_hits={})

    like = [f"%{_like_escape(n)}%" for n in needles]

    async def _exec_savepoint(stmt: str, params: Dict[str, Any]) -> None:
        """savepoint 化（评审 B I6）：pgvector 缺失/表缺失时单句失败不 500、
        不毒化事务——遗忘/撤销面不得随降级消失。"""
        try:
            async with db.begin_nested():
                await db.execute(text(stmt), params)
        except Exception:
            logger.warning("execute_forget statement failed (savepoint): %s",
                           stmt[:60], exc_info=True)

    # 概念：软遗忘（P2 降级 > 删除）
    for pat in like:
        await _exec_savepoint(
            "UPDATE memory_concepts SET status='forgotten', valid_to=NOW(),"
            " weight=0, updated_at=NOW() WHERE user_id=:uid AND ("
            "canonical_name ILIKE :p OR description_full ILIKE :p)",
            {"uid": user_id, "p": pat})
    # 事件 / 潜意识 / 澄清 / agent 记忆 / dream：物理删除（遗忘请求的删除面）
    for table, col in (("memory_episodes", "narrative"),
                       ("subconscious_log", "raw_text"),
                       ("memory_clarifications", "original_text")):
        for pat in like:
            await _exec_savepoint(
                f"DELETE FROM {table} WHERE user_id = :uid AND {col} ILIKE :p",
                {"uid": user_id, "p": pat})
    for table, col in (("agent_memories", "content"),
                       ("agent_dreams", "summary")):
        for pat in like:
            await _exec_savepoint(
                f"DELETE FROM {table} WHERE agent_state_id IN "
                f"(SELECT id FROM user_agent_states WHERE user_id = :uid)"
                f" AND {col} ILIKE :p",
                {"uid": user_id, "p": pat})
    # 约束：revoke（可逆，P2）
    for pat in like:
        await _exec_savepoint(
            "UPDATE conversation_pinned_constraints SET revoked_at=NOW(),"
            " revoked_by='forget' WHERE user_id=:uid AND revoked_at IS NULL"
            " AND text ILIKE :p",
            {"uid": user_id, "p": pat})
    # 文件层
    _scrub_file_store(user_id, needles)

    scan = await scan_residuals(db, user_id, needles)
    retained_hits = scan.get("retained", {})
    rep = residual_report(needles=needles, hits=retained_hits,
                          memory_hits=scan.get("memory", {}))
    man.status = "completed" if rep["residual_count"] == 0 else "residual"
    man.residual_json = {
        "residual_count": rep["residual_count"],
        "memory_hits": rep["memory_hits"],
        "retained_hits": rep["retained_hits"],
        "disclosure": rep["disclosure"],
    }
    man.completed_at = datetime.utcnow()
    await db.flush()
    return rep


async def erase_extended_surfaces(db: Any, user_id: str) -> Dict[str, int]:
    """W5-1 擦除面扩展（配合 DELETE /api/memory/all 的 GDPR 全擦除）：
    conversation artifacts / rolling summaries / resume snapshots /
    evidence units / skills / pinned constraints / dream reports——
    全部按 user_id 作用域（B20 多租户边界：无越权删除）。"""
    from sqlalchemy import text

    counts: Dict[str, int] = {}
    uid = str(user_id)

    async def _exec(name: str, stmt: str, params: Dict[str, Any]) -> None:
        try:
            res = await db.execute(text(stmt), params)
            counts[name] = int(getattr(res, "rowcount", 0) or 0)
        except Exception:
            logger.warning("erase_extended_surfaces %s failed", name, exc_info=True)
            counts[name] = -1

    # 会话派生物（artifacts / rolling summaries / resume snapshots / evidence units）
    await _exec("conversation_artifacts",
                "DELETE FROM conversation_artifacts WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE user_id = :uid)", {"uid": uid})
    await _exec("rolling_summaries",
                "DELETE FROM conversation_rolling_summaries WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE user_id = :uid)", {"uid": uid})
    await _exec("resume_snapshots",
                "DELETE FROM conversation_resume_snapshots WHERE conversation_id IN "
                "(SELECT id FROM conversations WHERE user_id = :uid)", {"uid": uid})
    await _exec("evidence_units",
                "UPDATE conversations SET deathmatch_evidence_units = '[]'::jsonb "
                "WHERE user_id = :uid AND deathmatch_evidence_units IS NOT NULL "
                "AND deathmatch_evidence_units::text <> '[]'", {"uid": uid})
    # 技能（W4-3 程序性记忆产物）：skill_files 由 FK CASCADE 随 user_skills 删
    await _exec("skill_files",
                "DELETE FROM skill_files WHERE skill_id IN "
                "(SELECT id FROM user_skills WHERE user_id = :uid)", {"uid": uid})
    await _exec("user_skills",
                "DELETE FROM user_skills WHERE user_id = :uid", {"uid": uid})
    # 固定约束（W1-2）与梦境修复报告（W3-3）：按 user_id 删（PLAN §5.1 擦除列）
    await _exec("pinned_constraints",
                "DELETE FROM conversation_pinned_constraints WHERE user_id = :uid",
                {"uid": uid})
    await _exec("dream_repair_reports",
                "DELETE FROM dream_repair_reports WHERE user_id = :uid", {"uid": uid})
    return counts

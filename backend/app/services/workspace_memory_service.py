# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""任务 / 项目分层记忆与 curator 晋升（2026-11-10 波 · T10）。

设计契约 §8：**分存储、分读取、分写入、分生命周期**

```
run working note / evidence
        │ run 结束或挂起：distill
        ▼
run-local candidate memory（⏳，带 run/evidence 来源）
        │ task curator：去重、核对证据、保留任务局部性
        ▼
Task memory
        │ 任务完成/集成或用户发布：只提炼项目通用经验
        ▼
Project memory        （永不自动进入系统记忆）
```

* 引用命名空间独立：`vwt:<task_id>:<topic>` / `vwp:<project_id>:<topic>`；
* `⛔/❌/⏳/✅` 是工程经验信任层级，**不**与系统记忆的 `verification_state` /
  `invalid_reason` 互相映射；
* curator 只做单调 taint 合并（更受限者胜），永不因「已发布」提升信任；
* 幂等键 `<task>:<run>:<topic>:<content_hash>`，并发 agent 不直接改共享索引；
* 本层文件**不进入** `MEMORY_SYNC_ENTITIES` / v2 概念表 / Dream Verify / retention。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

STATUS_CANDIDATE = "⏳"
STATUS_VERIFIED = "✅"
STATUS_FAILED = "❌"
STATUS_FORBIDDEN = "⛔"
_STATUSES = (STATUS_CANDIDATE, STATUS_VERIFIED, STATUS_FAILED, STATUS_FORBIDDEN)
_TAINT_RANK = {"trusted": 0, "agent": 1, "untrusted": 2}
_INDEX_CAP_CHARS = 25_000
_INDEX_CAP_LINES = 200
_INJECTION_CAP_CHARS = 300
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


@dataclass(frozen=True)
class MemoryTopic:
    name: str
    status: str
    path: str
    origin: str = "agent"
    taint: str = "agent"
    source_run: str = ""
    source_attempt: str = ""
    idempotency_key: str = ""
    evidence: tuple[str, ...] = ()
    updated_at: str = ""
    body: str = ""

    def as_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        out = {
            "name": self.name, "status": self.status, "path": self.path,
            "origin": self.origin, "taint": self.taint, "source_run": self.source_run,
            "source_attempt": self.source_attempt, "idempotency_key": self.idempotency_key,
            "evidence": list(self.evidence), "updated_at": self.updated_at,
        }
        if include_body:
            out["body"] = self.body
        return out


def task_ref(task_id: str, topic: str) -> str:
    return f"vwt:{task_id}:{topic}"


def project_ref(project_id: str, topic: str) -> str:
    return f"vwp:{project_id}:{topic}"


def _slug(topic: str) -> str:
    clean = _UNSAFE_NAME.sub("_", str(topic or "").strip()).strip("_") or "topic"
    return clean[:60]


def _content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:16]


def idempotency_key(*, task_id: str, run_id: str, topic: str, content: str) -> str:
    return f"{task_id}:{run_id}:{_slug(topic)}:{_content_hash(content)}"


# --- 文件读写 -----------------------------------------------------------------

def _render(topic: str, content: str, *, status: str, origin: str, taint: str,
            run_id: str, attempt_id: str, evidence: Sequence[str], key: str) -> str:
    meta = {
        "status": status, "origin": origin, "taint": taint,
        "source_run": run_id, "source_attempt": attempt_id,
        "idempotency_key": key, "evidence": list(evidence),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "authority": "workspace-task-memory",   # 明确不是系统记忆
    }
    return (f"---\n{json.dumps(meta, ensure_ascii=False, sort_keys=True)}\n---\n\n"
            f"# {topic}\n\n{str(content or '').strip()}\n")


def _parse(path: Path) -> MemoryTopic | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = json.loads(parts[1])
            except ValueError:
                meta = {}
            body = parts[2].strip()
    title = path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return MemoryTopic(
        name=title, status=str(meta.get("status") or STATUS_CANDIDATE),
        path=str(path), origin=str(meta.get("origin") or "agent"),
        taint=str(meta.get("taint") or "agent"),
        source_run=str(meta.get("source_run") or ""),
        source_attempt=str(meta.get("source_attempt") or ""),
        idempotency_key=str(meta.get("idempotency_key") or ""),
        evidence=tuple(meta.get("evidence") or ()),
        updated_at=str(meta.get("updated_at") or ""),
        body=body,
    )


def _write_topic_file(memory_dir: Path, topic: str, content: str, *, status: str, origin: str,
                      taint: str, run_id: str, attempt_id: str, evidence: Sequence[str],
                      key: str) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / f"{_slug(topic)}.md"
    path.write_text(_render(topic, content, status=status, origin=origin, taint=taint,
                            run_id=run_id, attempt_id=attempt_id, evidence=evidence, key=key),
                    encoding="utf-8")
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        index.write_text("# 记忆索引\n\n> 有界索引（≤200 行 / 25KB）。topic 文件按需读取。\n",
                         encoding="utf-8")
    _rebuild_index(memory_dir)
    return path


def _rebuild_index(memory_dir: Path) -> None:
    lines = ["# 记忆索引", "", "> 有界索引（≤200 行 / 25KB）。topic 文件按需读取，不整体预注入。", ""]
    topics = list_topics(memory_dir)
    for t in topics[:_INDEX_CAP_LINES - len(lines)]:
        evidence = f" · 证据 {len(t.evidence)}" if t.evidence else ""
        lines.append(f"- {t.status} [{t.name}]({_slug(t.name)}.md) · {t.taint}"
                     f" · run {t.source_run[:8] or '—'}{evidence}")
    try:
        (memory_dir / "MEMORY.md").write_text("\n".join(lines)[:_INDEX_CAP_CHARS] + "\n",
                                              encoding="utf-8")
    except OSError as exc:
        logger.warning("workspace memory index write failed: %s", exc)


def list_topics(memory_dir: Path | str) -> list[MemoryTopic]:
    base = Path(memory_dir)
    if not base.is_dir():
        return []
    out: list[MemoryTopic] = []
    for path in sorted(base.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        parsed = _parse(path)
        if parsed is not None:
            out.append(parsed)
    out.sort(key=lambda t: t.updated_at, reverse=True)
    return out


def read_index(memory_dir: Path | str) -> str:
    index = Path(memory_dir) / "MEMORY.md"
    try:
        return index.read_text(encoding="utf-8")[:_INDEX_CAP_CHARS]
    except OSError:
        return ""


def index_section_for_injection(memory_dir: Path | str) -> str:
    """注入系统提示的**有界**索引（§8.2：只给索引，topic 按需读取）。"""
    topics = list_topics(memory_dir)
    if not topics:
        return ""
    lines = []
    used = 0
    for t in topics:
        line = f"- {t.status} {t.name}（{t.taint}）"
        if used + len(line) + 1 > _INJECTION_CAP_CHARS:
            lines.append(f"- …另有 {len(topics) - len(lines)} 个 topic 未列出")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


# --- 写入与晋升 ---------------------------------------------------------------

def write_candidate(*, task_id: str, run_id: str, attempt_id: str, topic: str, content: str,
                    evidence: Sequence[str] = (), attempt_memory_dir: Path,
                    origin: str = "agent", taint: str = "agent") -> dict[str, Any]:
    """run 结束/挂起时写 attempt 级候选（幂等：同 key 不重复追加）。"""
    key = idempotency_key(task_id=task_id, run_id=run_id, topic=topic, content=content)
    path = _write_topic_file(
        Path(attempt_memory_dir), topic, content, status=STATUS_CANDIDATE, origin=origin,
        taint=taint, run_id=run_id, attempt_id=attempt_id, evidence=evidence, key=key)
    return {"ok": True, "path": str(path), "idempotency_key": key, "status": STATUS_CANDIDATE,
            "level": "run_candidate"}


def _merge_taint(*values: str) -> str:
    """单调合并：取更受限者（trusted > agent > untrusted）。永不因发布而提升。"""
    worst = "trusted"
    for value in values:
        if _TAINT_RANK.get(str(value or "agent"), 2) > _TAINT_RANK.get(worst, 0):
            worst = str(value or "agent")
    return worst


def curate_task(*, task_memory_dir: Path, candidate_dirs: Iterable[Path],
                workspace_root_for_evidence: Path | None = None) -> dict[str, Any]:
    """task curator：去重、核对证据存在性、保留任务局部性；不提升信任。"""
    task_dir = Path(task_memory_dir)
    existing = {t.idempotency_key: t for t in list_topics(task_dir)}
    seen_hashes = {_content_hash(t.body) for t in existing.values()}
    promoted: list[str] = []
    deduped: list[str] = []
    quarantined: list[str] = []

    for cand_dir in candidate_dirs:
        for topic in list_topics(Path(cand_dir)):
            if topic.idempotency_key and topic.idempotency_key in existing:
                deduped.append(topic.name)
                continue
            if _content_hash(topic.body) in seen_hashes:
                deduped.append(topic.name)
                continue
            if topic.status in (STATUS_FAILED, STATUS_FORBIDDEN):
                # 失败/禁用的教训保持 quarantine，不成为「已验证」事实
                quarantined.append(topic.name)
                target = task_dir / "quarantine"
                _write_topic_file(target, topic.name, topic.body, status=topic.status,
                                  origin=topic.origin, taint=topic.taint,
                                  run_id=topic.source_run, attempt_id=topic.source_attempt,
                                  evidence=topic.evidence, key=topic.idempotency_key)
                continue
            evidence_ok = _evidence_present(topic, workspace_root_for_evidence)
            status = STATUS_VERIFIED if evidence_ok else STATUS_CANDIDATE
            taint = _merge_taint(topic.taint, *(() if not evidence_ok else ()))
            _write_topic_file(task_dir, topic.name, topic.body, status=status,
                              origin=topic.origin, taint=taint, run_id=topic.source_run,
                              attempt_id=topic.source_attempt, evidence=topic.evidence,
                              key=topic.idempotency_key)
            promoted.append(topic.name)
            seen_hashes.add(_content_hash(topic.body))
    return {"ok": True, "promoted": promoted, "deduped": deduped, "quarantined": quarantined,
            "task_memory": str(task_dir)}


def _evidence_present(topic: MemoryTopic, root: Path | None) -> bool:
    if not topic.evidence:
        return False
    if root is None:
        return False
    base = Path(root).resolve()
    for item in topic.evidence:
        candidate = Path(str(item))
        target = candidate if candidate.is_absolute() else (base / candidate)
        try:
            if target.resolve().is_file() and ws.is_inside(base, target):
                return True
        except (OSError, RuntimeError):
            continue
    return False


def promote_to_project(*, task_memory_dir: Path, project_memory_dir: Path, topics: Sequence[str],
                       actor: str = "user") -> dict[str, Any]:
    """只提炼项目通用经验；来源保留在 source_run/source_task 字段里。"""
    wanted = {_slug(t): str(t) for t in topics}
    seen_slugs: set[str] = set()
    moved: list[str] = []
    missing: list[str] = []
    for topic in list_topics(Path(task_memory_dir)):
        if _slug(topic.name) not in wanted:
            continue
        seen_slugs.add(_slug(topic.name))
        if topic.status != STATUS_VERIFIED:
            missing.append(topic.name)   # 未验证不得晋升为项目事实
            continue
        _write_topic_file(Path(project_memory_dir), topic.name, topic.body,
                          status=topic.status, origin=topic.origin, taint=topic.taint,
                          run_id=topic.source_run, attempt_id=topic.source_attempt,
                          evidence=topic.evidence, key=topic.idempotency_key)
        moved.append(topic.name)
    # 请求了但任务层根本没有（或只在 quarantine 里）→ 同样拒绝晋升
    for slug, raw in wanted.items():
        if slug not in seen_slugs:
            missing.append(raw)
    return {"ok": True, "moved": moved, "refused_unverified": missing, "promoted_by": actor}


def retract(*, memory_dir: Path, topic: str) -> dict[str, Any]:
    """撤回发布（不删系统记忆，也不删别的层）。"""
    path = Path(memory_dir) / f"{_slug(topic)}.md"
    if not path.is_file():
        return {"ok": False, "error": "topic not found"}
    tombstone = Path(memory_dir) / "quarantine" / f"{_slug(topic)}.retracted.md"
    tombstone.parent.mkdir(parents=True, exist_ok=True)
    try:
        tombstone.write_text(path.read_text(encoding="utf-8") + "\n<!-- retracted -->\n",
                             encoding="utf-8")
        path.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    _rebuild_index(Path(memory_dir))
    return {"ok": True, "retracted": str(path), "tombstone": str(tombstone)}


def search(*, memory_dir: Path | str, query: str, limit: int = 6,
           include_history: bool = False) -> list[dict[str, Any]]:
    """确定性词法检索（不做 embedding，不与 v2 检索共用索引）。"""
    q = str(query or "").strip().lower()
    base = Path(memory_dir)
    topics = list_topics(base)
    if include_history and (base / "quarantine").is_dir():
        topics += list_topics(base / "quarantine")
    scored: list[tuple[float, MemoryTopic]] = []
    for topic in topics:
        hay = f"{topic.name} {topic.body}".lower()
        if not q:
            scored.append((0.0, topic))
            continue
        score = 0.0
        if q in topic.name.lower():
            score += 2.0
        if q in hay:
            score += 1.0
        for token in [t for t in q.split() if len(t) >= 2]:
            if token in hay:
                score += 0.4
        if score > 0:
            scored.append((score, topic))
    scored.sort(key=lambda pair: (-pair[0], pair[1].updated_at), reverse=False)
    out = []
    for score, topic in scored[:max(1, int(limit))]:
        excerpt = re.sub(r"\s+", " ", topic.body)[:400]
        item = topic.as_dict()
        item["score"] = round(score, 3)
        item["excerpt"] = excerpt
        out.append(item)
    return out


__all__ = ["MemoryTopic", "STATUS_CANDIDATE", "STATUS_FORBIDDEN", "STATUS_FAILED",
           "STATUS_VERIFIED", "curate_task", "idempotency_key", "index_section_for_injection",
           "list_topics", "project_ref", "promote_to_project", "read_index", "retract",
           "search", "task_ref", "write_candidate"]

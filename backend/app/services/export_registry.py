# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""PDF 导出登记/去重/并发（2026-10-06 三波，用户点名；D-16）。

契约：
- **登记**：每次 PDF 导出（对话/笔记）落 ``export_tasks`` 台账（含
  ``content_hash``）——任何一次导出可查（2026-10-06 18:00 冻结事件 0 台账行
  实证旧对话导出完全无登记）。
- **去重**：``content_hash(title+content+action+format)`` 命中 completed 行且
  产物文件在盘 → 直接回文件**零渲染**；并发同内容**单飞**（只渲染一次，等待方
  复用首个产物）。
- **并发**：全局信号量钳制渲染并行度（默认 2）——质量不变，仅限制无界并行的
  WeasyPrint/Chromium 渲染风暴（18:00 冻结画像）。
"""
import asyncio
import contextlib
import hashlib
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

PDF_RENDER_CONCURRENCY = 2

# 渲染管线版本盐（A4.9 I6）：模板/管线变更时 bump——旧缓存指纹失配自动作废
EXPORT_RENDER_VERSION = "v1-20261007"

_render_sem: asyncio.Semaphore | None = None
_render_sem_loop = None
_inflight: dict[str, asyncio.Event] = {}
# 在写产物集（评审 B-1，fix-round 1）：render_with_dedup 写盘→登记完成前的窗口——
# 此间的 content-addressed 文件不得被清理/删除路径 unlink（无行引用≠可删）。
_writing_paths: set[str] = set()
# 文件锁表（UPSTREAM_TODO_20261007 项4 / 06 档项15，W6 A-m6）：per-key
# refcount——`[Lock, n]`，n=当前持有者+等待者数；归零即 pop（此前只增不减=
# 长驻多租户进程缓慢泄漏）。increment 与锁获取之间无 await（单事件循环内
# 原子）；decrement 在 finally（cancelled waiter 亦归还）。count≥2 时绝不
# 回收——不存在「新请求拿到与旧等待者不同的锁」的破互斥窗口。
_file_locks: dict[str, list] = {}


@contextlib.asynccontextmanager
async def _file_lock(key: str):
    entry = _file_locks.get(key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        _file_locks[key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0:
            _file_locks.pop(key, None)


def content_hash(*parts: str) -> str:
    """内容指纹：长度前缀 + 载荷喂 sha256（长度前缀消除分隔符歧义——A4.9 M1，
    内容内嵌 NUL 不可构造碰撞）。"""
    h = hashlib.sha256()
    for p in parts:
        b = (p or "").encode("utf-8")
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
    return h.hexdigest()


def fingerprint(user_id: str, *parts: str) -> str:
    """导出指纹（A4.9 I4：user 入指纹——跨用户同内容不同指纹，产物文件不互踩）。"""
    return content_hash(EXPORT_RENDER_VERSION, user_id or "", *parts)


def workspace_fingerprint(workspace_root: str | None) -> str:
    """工作区指纹（A4.9 I6）：引用图片的路径/大小/mtime 入键——图表改动后
    同文导出不得命中旧缓存。有界扫描（≤200 文件）。"""
    if not workspace_root or not os.path.isdir(workspace_root):
        return "no-ws"
    h = hashlib.sha256()
    n = 0
    for root, _dirs, files in os.walk(workspace_root):
        for name in sorted(files):
            fp = os.path.join(root, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, workspace_root)
            h.update(f"{rel}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))
            n += 1
            if n >= 200:
                return h.hexdigest()[:16] + "+trunc"
    return h.hexdigest()[:16] or "empty-ws"


def _semaphore() -> asyncio.Semaphore:
    global _render_sem, _render_sem_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _render_sem is None or _render_sem_loop is not loop:
        _render_sem = asyncio.Semaphore(PDF_RENDER_CONCURRENCY)
        _render_sem_loop = loop
    return _render_sem


async def run_render(fn, *args, **kwargs):
    """渲染受护栏：to_thread（不阻塞事件循环）+ 全局信号量（限并行度）。"""
    async with _semaphore():
        return await asyncio.to_thread(fn, *args, **kwargs)


async def find_cached_export(db, user_id: str, chash: str, fmt: str = "pdf"):
    """查同内容已完成导出（产物复用候选）。"""
    from sqlalchemy import select
    from app.db.database import ExportTask

    result = await db.execute(
        select(ExportTask).where(
            ExportTask.user_id == user_id,
            ExportTask.content_hash == chash,
            ExportTask.format == fmt,
            ExportTask.status == "completed",
            ExportTask.file_path.isnot(None),
        ).order_by(ExportTask.completed_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def register_export(db, *, user_id: str, task_type: str, fmt: str,
                          chash: str, file_path: str, filename: str,
                          note_id: str | None = None,
                          note_ids: str | None = None,
                          endnote_enabled: bool = True):
    """导出登记：completed 台账行（同步导出即产即记；异步任务由 worker 回写）。"""
    from app.db.database import ExportTask

    row = ExportTask(
        id=str(uuid.uuid4()),
        user_id=user_id,
        task_type=task_type,
        format=fmt,
        note_id=note_id,
        note_ids=note_ids,
        status="completed",
        progress=1.0,
        file_path=file_path,
        filename=filename,
        content_hash=chash,
        endnote_enabled=endnote_enabled,
        completed_at=__import__("datetime").datetime.utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


async def delete_file_if_unreferenced(db, file_path: str, exclude_ids: set[str]) -> bool:
    """仅删独占导出产物（UPSTREAM_TODO_20261006 项 8，评审 B m3）。

    W6 起产物按 content_hash 共享命名——多任务/单飞复用可指向同一文件，按行
    直删会连带删掉它任务在盘产物（下载 404 且无重渲染）。删除前查
    ``export_tasks`` 引用：仍被非 ``exclude_ids`` 行引用 → 保留，由最后引用行
    的清理路径收割。返回是否真正删除。
    """
    if not file_path:
        return False
    if file_path in _writing_paths:
        return False  # 评审 B-1：在写产物（写盘→登记窗口）永不收割
    from sqlalchemy import select
    from app.db.database import ExportTask

    stmt = select(ExportTask.id).where(ExportTask.file_path == file_path)
    if exclude_ids:
        stmt = stmt.where(ExportTask.id.notin_(exclude_ids))
    ref_id = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if ref_id is not None:
        return False
    try:
        if await asyncio.to_thread(os.path.isfile, file_path):
            await asyncio.to_thread(os.remove, file_path)
            return True
    except OSError:
        logger.exception("Failed to remove export file %s", file_path)
    return False


async def delete_row_and_reap_file(db, row) -> bool:
    """删台账行 + 独占产物收割（同一文件锁内原子序；评审 B-2，fix-round 1）。

    并发双删 TOCTOU（两行互为最后引用、各自 guard 都看见对方）= 共享文件永久
    泄漏——「删行→提交→按剩余引用收割」在 `_file_lock(file_path)` 内串行后，
    后到者必见前者行已消，独占产物由后到者收割（last-ref 收割语义成立）。
    锁在最后持有/等待者释放后回收（项4，refcount 归零 pop）。
    """
    fp = getattr(row, "file_path", None) or ""
    row_id = str(getattr(row, "id", ""))
    async with _file_lock(fp or row_id):
        await db.delete(row)
        await db.commit()
        return await delete_file_if_unreferenced(db, fp, {row_id})


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def render_with_dedup(db, user_id: str, *, task_type: str, fmt: str,
                            key_parts: tuple, render_fn, output_dir: str,
                            filename_base: str, ext: str | None = None,
                            endnote_enabled: bool = True):
    """去重主入口：返回 (file_path, filename, cached: bool)。

    命中缓存（同 hash + completed + 产物在盘）→ 直接复用文件零渲染；
    否则单飞渲染（并发同 hash 只渲染一次）→ 写产物 → 登记台账。
    """
    chash = fingerprint(user_id, *key_parts)

    async def _lookup():
        row = await find_cached_export(db, user_id, chash, fmt)
        if row is not None and await asyncio.to_thread(os.path.isfile, row.file_path):
            return row
        return None

    cached = await _lookup()
    if cached is not None:
        return cached.file_path, cached.filename, True

    # 单飞（A4.9 I3 修）：循环接手——首飞失败后等待方按序接手（各自成 leader），
    # pop 只认自己的 holder（防摘他人的 event）；等待方醒来先复查缓存。
    holder = None
    while holder is None:
        cached = await _lookup()
        if cached is not None:
            return cached.file_path, cached.filename, True
        ev = _inflight.get(chash)
        if ev is None:
            holder = asyncio.Event()
            _inflight[chash] = holder
        else:
            await ev.wait()  # 首飞结束（成败皆然）→ 回到循环复查/接手
    try:
        data = await run_render(render_fn)
        _ext = ext or ("zip" if fmt == "zip" else "pdf")
        name = f"{user_id[:8]}_{filename_base or 'export'}_{chash[:12]}.{_ext}"
        path = os.path.join(output_dir, name)
        _writing_paths.add(path)  # 评审 B-1：写盘→登记窗口内产物不许被收割
        try:
            await asyncio.to_thread(_write_file, path, data)
            await register_export(db, user_id=user_id, task_type=task_type, fmt=fmt,
                                  chash=chash, file_path=path, filename=name,
                                  endnote_enabled=endnote_enabled)
        finally:
            _writing_paths.discard(path)
        return path, name, False
    finally:
        if _inflight.get(chash) is holder:
            _inflight.pop(chash, None)
        holder.set()

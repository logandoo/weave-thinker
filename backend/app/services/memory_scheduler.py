# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
import time
import uuid
import zlib
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import AsyncSessionLocal

config = get_config()
logger = logging.getLogger(__name__)

_WORKER_ID = os.environ.get("WORKER_INSTANCE_ID", str(uuid.uuid4()))
_LEADER_LOCK_ID = 0x4D454D4F5259


def _user_lock_id(user_id: str) -> int:
    """跨进程稳定的 per-user advisory lock id（Python hash() 按进程随机化，不可用）。"""
    return zlib.crc32(f"mem_user_{user_id}".encode("utf-8")) % (2**31)


def _get_memory_tz() -> ZoneInfo:
    return ZoneInfo(config.memory_timezone)


def _local_today() -> datetime:
    return datetime.now(_get_memory_tz()).replace(hour=0, minute=0, second=0, microsecond=0)


def _next_consolidation_action(now_local: datetime, done_date: str | None) -> str:
    """consolidation 循环调度决策（纯函数，可测）。

    - "sleep": 非午夜窗口，直接休眠
    - "run": 午夜窗口且当日尚未执行 -> 尝试执行（失败重试）
    - "done-sleep": 当日已成功执行 -> 本夜不再重复
    """
    if now_local.hour != 0:
        return "sleep"
    today = now_local.strftime("%Y-%m-%d")
    if done_date == today:
        return "done-sleep"
    return "run"


def _task_done_callback(scheduler, name: str):
    """后台任务异常兜底：asyncio fire-and-forget 任务死亡不再静默（2026-08-10 事故根因之一）。
    正常停机（stop() 先置 _running=False 再 cancel）时循环自然退出不算异常。"""

    def _cb(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("MemoryScheduler %s task died: %s", name, exc, exc_info=exc)
        elif scheduler._running:
            logger.warning("MemoryScheduler %s task exited unexpectedly while running", name)

    return _cb


class MemoryScheduler:
    def __init__(self):
        self._subconscious_task: asyncio.Task | None = None
        self._consolidation_task: asyncio.Task | None = None
        self._running = False

    @property
    def enabled(self) -> bool:
        from app.services.memory_runtime_state import memory_runtime_enabled
        return memory_runtime_enabled(config) and bool(config.memory.get("subconscious_enabled", True))

    async def start(self) -> None:
        if not self.enabled:
            logger.info("MemoryScheduler: disabled via config (memory.enabled=false or subconscious_enabled=false)")
            return
        self._running = True
        self._subconscious_task = asyncio.create_task(self._run_subconscious_scan_loop())
        self._consolidation_task = asyncio.create_task(self._run_consolidation_loop())
        self._subconscious_task.add_done_callback(_task_done_callback(self, "subconscious"))
        self._consolidation_task.add_done_callback(_task_done_callback(self, "consolidation"))
        logger.info("MemoryScheduler: started (worker=%s)", _WORKER_ID)

    async def stop(self) -> None:
        self._running = False
        for task in (self._subconscious_task, self._consolidation_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        logger.info("MemoryScheduler: stopped")

    async def _try_acquire_leader(self, db: AsyncSession) -> bool:
        try:
            # 2026-08-10 锁泄漏根治：连接池复用可能带回上一次会话残留的 session 级
            # advisory 锁（per-user 锁/leader 锁），先全量清理再获取，避免陈旧锁导致
            # 本连接上的旧锁永久悬挂（unlock_all 只释放当前会话持有的锁，安全）。
            await db.execute(text("SELECT pg_advisory_unlock_all()"))
            result = await db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _LEADER_LOCK_ID},
            )
            acquired = result.scalar()
            if acquired:
                await db.execute(
                    text("""
                        INSERT INTO worker_instances (id, pid, started_at, last_heartbeat, status, metadata)
                        VALUES (:id, :pid, :started_at, :heartbeat, 'active', :meta)
                        ON CONFLICT (id) DO UPDATE
                        SET last_heartbeat = :heartbeat, status = 'active'
                    """),
                    {
                        "id": _WORKER_ID,
                        "pid": os.getpid(),
                        "started_at": time.time(),
                        "heartbeat": time.time(),
                        "meta": json.dumps({"role": "memory_leader"}),
                    },
                )
                await db.commit()
                return True
            return False
        except Exception:
            logger.exception("MemoryScheduler: leader election failed")
            return False

    async def _release_leader(self, db: AsyncSession) -> None:
        # unlock_all 覆盖 leader 锁 + 任何残留 per-user 锁；失败必须大声（原实现
        # except: pass 静默吞掉导致锁泄漏 8/4-8/10 无任何告警）。
        try:
            await db.execute(text("SELECT pg_advisory_unlock_all()"))
            await db.commit()
        except Exception:
            logger.exception("MemoryScheduler: leader lock release failed (will self-heal on next acquire)")

    async def _run_subconscious_scan_loop(self) -> None:
        interval = max(int(config.memory.get("extraction_interval_minutes", 15)) * 60, 60)
        # 启动后等待一个 interval 再首次执行，避免启动阶段争抢 DB 连接
        await asyncio.sleep(min(interval, 300))
        while self._running:
            try:
                await self._run_subconscious_scan()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MemoryScheduler: subconscious scan error")
            await asyncio.sleep(interval)

    async def _run_subconscious_scan(self) -> None:
        async with AsyncSessionLocal() as db:
            if not await self._try_acquire_leader(db):
                logger.debug("MemoryScheduler: not leader, skip scan")
                return
            skipped = 0
            try:
                active_users = await self._get_active_users(db)
                for user_id in active_users:
                    try:
                        acquired = await db.execute(
                            text("SELECT pg_try_advisory_lock(:lock_id)"),
                            {"lock_id": _user_lock_id(user_id)},
                        )
                        if not acquired.scalar():
                            skipped += 1
                            continue
                        try:
                            await asyncio.wait_for(
                                self._run_subconscious_for_user(db, user_id),
                                timeout=120,
                            )
                        finally:
                            # 先 rollback 保证事务干净，再释放 per-user 锁；任一失败必须
                            # 大声记录（池 reset 监听器 + 下次 acquire 的 unlock_all 兜底）
                            try:
                                await db.rollback()
                            except Exception:
                                logger.exception("MemoryScheduler: rollback failed for user=%s", user_id)
                            try:
                                await db.execute(
                                    text("SELECT pg_advisory_unlock(:lock_id)"),
                                    {"lock_id": _user_lock_id(user_id)},
                                )
                                await db.commit()
                            except Exception:
                                logger.exception("MemoryScheduler: failed to release user lock for %s (auto-cleaned on pool return)", user_id)
                    except asyncio.TimeoutError:
                        logger.warning("MemoryScheduler: user %s scan timeout", user_id)
                    except Exception:
                        logger.exception("MemoryScheduler: user %s scan failed", user_id)
            finally:
                await self._release_leader(db)
            logger.info("MemoryScheduler: scan cycle done users=%d skipped_locked=%d", len(active_users), skipped)

    async def _run_subconscious_for_user(self, db: AsyncSession, user_id: str) -> None:
        from app.services.memory_subconscious_service import (
            archive_soft_deprecated,
            ingest_pending_raw_units,
            scan_recurrence,
        )
        await ingest_pending_raw_units(db, user_id)
        await db.commit()
        # §9.10 降级链最重档 subconscious_off：新 raw 仅入存档，暂停 recurrence 扫描升级
        try:
            from app.services.memory_cost_governance_service import is_step_enabled
            sub_enabled = await is_step_enabled(user_id, "subconscious_off", db)
        except Exception:
            sub_enabled = True
        if sub_enabled and config.memory.get("recurrence_trigger_enabled", True):
            await scan_recurrence(db, user_id)
            await db.commit()
        try:
            await archive_soft_deprecated(db, user_id)
            await db.commit()
        except Exception:
            logger.debug("subconscious archive failed for user=%s", user_id, exc_info=True)
        # §2026-08-09 画像事实写路径：每日一次 LLM 提炼 profile 概念入库
        # （幂等；失败不影响本循环其它步骤）
        try:
            if config.memory.get("profile_sync_enabled", True):
                from app.services.memory_profile_service import sync_profile_concepts
                await sync_profile_concepts(db, user_id)
                await db.commit()
        except Exception:
            logger.debug("profile concept sync failed for user=%s", user_id, exc_info=True)

    async def _run_consolidation_loop(self) -> None:
        """午夜 consolidation，带重试窗口（2026-08-10 修复：原实现午夜只试一次，
        与扫描锁竞争失败即静默跳过，导致 8/4 起全库 consolidation 停摆）。"""
        done_date: str | None = None
        while self._running:
            await asyncio.sleep(60)
            now_local = datetime.now(_get_memory_tz())
            action = _next_consolidation_action(now_local, done_date)
            if action == "sleep":
                continue
            if action == "done-sleep":
                await asyncio.sleep(600)
                continue
            try:
                ran, processed = await self._run_consolidation_check()
                if ran and processed == 0:
                    logger.warning("MemoryScheduler: consolidation ran but 0 users processed, retrying in 5 min")
                    await asyncio.sleep(300)
                    continue
                if ran:
                    done_date = now_local.strftime("%Y-%m-%d")
                    logger.info("MemoryScheduler: consolidation completed for %s (users=%d)", done_date, processed)
                else:
                    logger.warning("MemoryScheduler: consolidation check skipped (leader busy), retrying in 5 min")
                    await asyncio.sleep(300)
                    continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MemoryScheduler: consolidation error, retrying in 5 min")
                await asyncio.sleep(300)
                continue
            await asyncio.sleep(3600)

    async def _run_consolidation_check(self) -> tuple[bool, int]:
        """执行午夜 consolidation。

        返回 (ran, processed_users)：ran=False 表示未获得 leader 锁被跳过（调用方重试）；
        processed_users 为实际完成 consolidation 的用户数（0 表示本轮无用户被处理，
        调用方不置 done 并重试，避免用户级全失败被当作"已成功"吞掉）。

        注意计数语义（R2 审查备注）：processed 包含正常完成、no_state、_should_consolidate
        跳过（skipped）与内部步骤失败但正常返回（had_failure）的用户——这些路径都推进了
        last_consolidation_at 水位；processed==0 仅在"无 active 用户 / 全锁竞争跳过 /
        全抛异常超时"时出现。
        """
        async with AsyncSessionLocal() as db:
            if not await self._try_acquire_leader(db):
                logger.debug("MemoryScheduler: consolidation not leader, skip")
                return False, 0
            skipped = 0
            processed = 0
            try:
                active_users = await self._get_active_users(db)
                # §9.10：计费自动降级每日检查（先于 consolidation，降级用户跳过贵操作）
                for user_id in active_users:
                    try:
                        from app.services.memory_cost_governance_service import check_user_threshold_and_degrade
                        await asyncio.wait_for(
                            check_user_threshold_and_degrade(db, user_id), timeout=30)
                        await db.commit()
                    except asyncio.TimeoutError:
                        logger.warning("MemoryScheduler: cost governance check timeout for %s", user_id)
                        await db.rollback()
                    except Exception:
                        logger.debug("cost governance check failed for %s", user_id, exc_info=True)
                        await db.rollback()
                for user_id in active_users:
                    try:
                        acquired = await db.execute(
                            text("SELECT pg_try_advisory_lock(:lock_id)"),
                            {"lock_id": _user_lock_id(user_id)},
                        )
                        if not acquired.scalar():
                            skipped += 1
                            continue
                        try:
                            await asyncio.wait_for(
                                self._run_consolidation_for_user(db, user_id),
                                timeout=300,
                            )
                            processed += 1
                        finally:
                            try:
                                await db.rollback()
                            except Exception:
                                logger.exception("MemoryScheduler: consolidation rollback failed for %s", user_id)
                            try:
                                await db.execute(
                                    text("SELECT pg_advisory_unlock(:lock_id)"),
                                    {"lock_id": _user_lock_id(user_id)},
                                )
                                await db.commit()
                            except Exception:
                                logger.exception("MemoryScheduler: failed to release consolidation lock for %s (auto-cleaned on pool return)", user_id)
                    except asyncio.TimeoutError:
                        logger.warning("MemoryScheduler: user %s consolidation timeout", user_id)
                    except Exception:
                        logger.exception("MemoryScheduler: user %s consolidation failed", user_id)
            finally:
                await self._release_leader(db)
            logger.info("MemoryScheduler: consolidation cycle done users=%d processed=%d skipped_locked=%d", len(active_users), processed, skipped)
            return True, processed

    async def _run_consolidation_for_user(self, db: AsyncSession, user_id: str) -> None:
        from app.services.memory_consolidation_service import run_consolidation
        await run_consolidation(db, user_id)

    async def _get_active_users(self, db: AsyncSession) -> list[str]:
        result = await db.execute(
            text("SELECT id FROM users WHERE is_active = TRUE ORDER BY last_login_at DESC NULLS LAST LIMIT 200")
        )
        return [row[0] for row in result.fetchall()]


memory_scheduler = MemoryScheduler()

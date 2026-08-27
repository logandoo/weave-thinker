# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
import signal
import sys
import asyncio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)

from app.api import chat, conversation, auth, assistant, asr, admin, sessions, notes, scheduled_tasks, files, agent_tasks, export_tasks, file_upload, image_upload, config as config_api, skills, voice, system, memory as memory_api, skins as skins_api
from app.db.database import init_db, AsyncSessionLocal
from app.core.config import get_config, clear_config_cache
from app.services.agent_scheduler import agent_scheduler
from app.services.agent_worker import agent_worker
from app.services.export_worker import export_worker
from app.services.http_client import close_shared_async_client
from app.services.shared_state import shared_state, configure_db_backend

logger = logging.getLogger(__name__)

# 后台任务强引用集（防 GC 中断在途任务）
_background_tasks: set = set()


async def _worker_heartbeat_loop() -> None:
    """Refresh this worker's last_heartbeat so snapshot-owner liveness
    checks (shared_state.is_worker_active) can tell live workers from
    hard-killed ones. Cadence 30s; the freshness window must exceed it."""
    import time as _t
    while True:
        await asyncio.sleep(30)
        if not shared_state.is_db_enabled:
            continue
        try:
            from sqlalchemy import text
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("UPDATE worker_instances SET last_heartbeat = :hb WHERE id = :id"),
                    {"hb": _t.time(), "id": shared_state.worker_id},
                )
                await session.commit()
        except Exception:
            pass

config = get_config()

app = FastAPI(title="Weave Thinker API", version="0.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.security_cors_allow_origins,
    allow_credentials=config.security_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(conversation.router)
app.include_router(assistant.router)
app.include_router(asr.router)
app.include_router(admin.router)
app.include_router(sessions.router)
app.include_router(notes.router)
app.include_router(scheduled_tasks.router)
app.include_router(files.router)
app.include_router(agent_tasks.router)
app.include_router(export_tasks.router)
app.include_router(file_upload.router)
app.include_router(image_upload.router)
app.include_router(config_api.router)
app.include_router(skills.router)
app.include_router(voice.router)
app.include_router(system.router)
app.include_router(memory_api.router)
app.include_router(memory_api.admin_router)
app.include_router(skins_api.router)


async def _memory_reprobe_after_reload() -> None:
    """SIGHUP 配置重载后重新探测 memory 运行环境。

    pgvector 可用性由启动期 migrations 确定（运行期不变）；embedding 探测
    在新 Config 实例上重跑。先测后启：探测期间 kill-switch 保持禁用
    （请求走旧方案兜底），确认可用才解除——无半开窗口（A4.9 round5 Minor #2）。
    """
    from app.db import migrations as _m
    if not _m.PGVECTOR_AVAILABLE:
        return
    from app.services.memory_runtime_state import (
        memory_disabled_reason as _reason,
        enable_memory as _enable_memory,
    )
    from app.services.memory_embedding_service import probe_memory_embedding_on_startup
    was_disabled = _reason() is not None
    ok = await probe_memory_embedding_on_startup()
    if ok and was_disabled:
        # 探测通过且此前被禁用 → 环境已修复，解除禁用
        _enable_memory()


@app.on_event("startup")
async def startup_event():
    if not config.security_jwt_secret_key:
        raise RuntimeError("JWT secret key is not configured")

    def _on_sighup_reload_config(signum, frame):
        clear_config_cache()
        logger.info("Config cache cleared via SIGHUP")
        # 重载后重探测 memory 环境（kill-switch 跨 Config 实例一致：
        # 环境已修复则解除禁用，未修复则探测再次禁用——不会半新半旧）
        try:
            loop = asyncio.get_running_loop()
            # 信号处理器上下文用 call_soon_threadsafe（create_task 非 signal-safe）
            loop.call_soon_threadsafe(lambda: loop.create_task(_memory_reprobe_after_reload()))
        except RuntimeError:
            pass  # 事件循环未就绪（启动早期），首次启动探测会覆盖

    if sys.platform != "win32":
        signal.signal(signal.SIGHUP, _on_sighup_reload_config)

    audio_files_dir = os.path.join(os.path.dirname(__file__), "audio_files")
    os.makedirs(audio_files_dir, exist_ok=True)
    output_files_dir = os.path.join(os.path.dirname(__file__), "output_files")
    os.makedirs(output_files_dir, exist_ok=True)
    fonts_dir = os.path.join(os.path.dirname(__file__), "Fonts")
    os.makedirs(fonts_dir, exist_ok=True)
    await init_db()

    # Phase 5.5: Wire DB backend for multi-instance shared state
    from app.services.shared_state import configure_db_backend
    configure_db_backend(AsyncSessionLocal)
    try:
        await shared_state.enable_db_backend()
        # Register this worker instance in the database
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text
            from app.services.shared_state import WORKER_INSTANCE_ID, WORKER_STARTED_AT
            import os as _os
            await session.execute(
                text("""INSERT INTO worker_instances (id, pid, started_at, last_heartbeat, status)
                        VALUES (:id, :pid, :started_at, :heartbeat, :status)
                        ON CONFLICT (id) DO UPDATE
                        SET last_heartbeat = :heartbeat, pid = :pid, status = :status"""),
                {
                    "id": WORKER_INSTANCE_ID,
                    "pid": _os.getpid(),
                    "started_at": WORKER_STARTED_AT,
                    "heartbeat": WORKER_STARTED_AT,
                    "status": "active",
                },
            )
            await session.commit()
        logger.info("Worker instance registered: %s (pid=%d)", WORKER_INSTANCE_ID, _os.getpid())
        # Heartbeat loop: keep last_heartbeat fresh so is_worker_active()
        # (shared_state) can distinguish a live worker from a hard-killed
        # one whose status row still says 'active'. Without freshness,
        # stale buffer/agent snapshots from dead workers are never
        # distrusted and resurrect as immortal "running" zombies that pin
        # the frontend's streaming state forever (conv 149ce886, 2026-08-01).
        _heartbeat_task = asyncio.create_task(_worker_heartbeat_loop())
        _background_tasks.add(_heartbeat_task)
    except Exception:
        logger.warning("Failed to enable DB shared state backend — running single-instance mode", exc_info=True)

    await agent_scheduler.start()
    await agent_worker.start()
    await export_worker.start()
    # §9.5：pgvector 缺失时 memory v2 表不存在——kill-switch 禁用 memory 子系统
    # （跨 Config 实例一致，服务以旧方案继续运行）
    from app.db import migrations as _db_migrations
    from app.services.memory_runtime_state import disable_memory as _disable_memory
    if not _db_migrations.PGVECTOR_AVAILABLE:
        if config.memory.get("enabled") or config.memory.get("migration_enabled"):
            logger.error(
                "pgvector 不可用，禁用 memory 子系统；"
                "安装 pgvector 并重启后可重新开启（§9.5）")
        _disable_memory("pgvector 不可用（§9.5）")
    # Memory & Dreaming v2 scheduler（§9.11：先探测 embedding provider，不可用则禁用）
    from app.services.memory_embedding_service import probe_memory_embedding_on_startup
    await probe_memory_embedding_on_startup()
    from app.services.memory_scheduler import memory_scheduler
    await memory_scheduler.start()
    # §8.3 迁移启动自动排队（含 status='running' 崩溃恢复断点续传）
    from app.services.memory_runtime_state import memory_runtime_enabled as _mem_rt_enabled
    if _mem_rt_enabled(config) and config.memory.get("migration_enabled", False):
        from app.services.memory_migration_service import enqueue_pending_migrations
        _mig_task = asyncio.create_task(enqueue_pending_migrations())
        _background_tasks.add(_mig_task)

        def _log_migration_result(t: asyncio.Task) -> None:
            _background_tasks.discard(t)
            if t.cancelled():
                return
            if t.exception():
                logger.error("migration queue failed", exc_info=t.exception())
            else:
                logger.info("migration queue done: %s", t.result())

        _mig_task.add_done_callback(_log_migration_result)
    # Start stream buffer cleanup loop
    from app.services.stream_buffer import stream_buffer_manager
    await stream_buffer_manager.start_cleanup()
    # Start active agent registry cleanup loop + orphan recovery
    from app.services.active_agent_registry import ActiveAgentRegistry
    registry = ActiveAgentRegistry.get_instance()
    await registry.start_cleanup()
    await registry.recover_orphaned_tasks()


@app.on_event("shutdown")
async def shutdown_event():
    # De-register worker instance
    if shared_state.is_db_enabled:
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import text
                await session.execute(
                    text("UPDATE worker_instances SET status='stopped' WHERE id = :id"),
                    {"id": shared_state.worker_id},
                )
                await session.commit()
            logger.info("Worker instance de-registered: %s", shared_state.worker_id)
        except Exception:
            pass
    await agent_scheduler.stop()
    await agent_worker.stop()
    await export_worker.stop()
    from app.services.memory_scheduler import memory_scheduler
    await memory_scheduler.stop()
    try:
        from app.services.interactive_browser_service import InteractiveBrowserService
        await InteractiveBrowserService.get_instance().shutdown()
    except Exception:
        pass
    await close_shared_async_client()


static_dir = os.path.join(os.path.dirname(__file__), "static")
index_path = os.path.join(static_dir, "index.html")


@app.post("/api/admin/reload-config")
async def reload_config(request: Request):
    if request.headers.get("x-admin-secret") != config.security_jwt_secret_key:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    clear_config_cache()
    return {"status": "ok"}


# SPA catch-all: serve index.html for client-side routes
@app.get("/app/frontend/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path:
        # Resolve the real path and ensure it stays within static_dir
        file_path = os.path.realpath(os.path.join(static_dir, full_path))
        if file_path.startswith(os.path.realpath(static_dir) + os.sep) and os.path.isfile(file_path):
            return FileResponse(file_path)
    # Serve index.html for client-side routing
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html")
    return {"detail": "Not Found"}


@app.get("/")
def root():
    frontend_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(frontend_path):
        return RedirectResponse(url="/app/frontend/")
    return {"message": "Weave Thinker API is running", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.server_host,
        port=config.server_port,
        ssl_keyfile="key.pem",
        ssl_certfile="cert.pem",
        reload=False
    )
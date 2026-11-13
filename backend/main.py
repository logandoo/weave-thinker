# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
import re
import secrets
import signal
import sys
import asyncio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)

from app.api import chat, conversation, auth, assistant, asr, admin, sessions, notes, scheduled_tasks, files, agent_tasks, export_tasks, file_upload, image_upload, models as models_api, skills, voice, system, memory as memory_api, skins as skins_api, user_settings, sync as sync_api, citations as citations_api, server_admin
from app.db.database import init_db, AsyncSessionLocal
from app.core.config import get_config, clear_config_cache, production_security_errors, security_config_warnings
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
    # UPSTREAM_TODO_20261007 项2：drain 门 503 的 Retry-After 不在 CORS 安全
    # 清单——跨源（app://weave → http://127.0.0.1:<端口>）必须显式 expose，
    # 否则客户端读不到服务端建议退避、恒回退内置 5000ms。
    expose_headers=["Retry-After"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(conversation.router)
app.include_router(server_admin.router)
from app.api import message_payloads as message_payloads_api
from app.api import workspace as workspace_api
from app.api import mcp as mcp_api
app.include_router(message_payloads_api.router)
app.include_router(mcp_api.router)
app.include_router(workspace_api.router)
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
app.include_router(models_api.router)
app.include_router(skills.router)
app.include_router(voice.router)
app.include_router(system.router)
app.include_router(memory_api.router)
app.include_router(memory_api.admin_router)
app.include_router(skins_api.router)
app.include_router(user_settings.router)
app.include_router(sync_api.router)
app.include_router(citations_api.router)
from app.api import client_devices as client_devices_api
from app.api import device_ws as device_ws_api
app.include_router(client_devices_api.router)
app.include_router(device_ws_api.router)


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


_durable_job_worker = None  # durable job worker 句柄（lifespan 启停）

@app.on_event("startup")
async def startup_event():
    if not config.security_jwt_secret_key:
        raise RuntimeError("JWT secret key is not configured")
    # F2（2026-10-26）：启动清扫上一代遗留的工具子进程组（SIGKILL/崩溃后兜底；
    # 签名不匹配 fail-safe 不杀，见 spawn_registry）
    try:
        from app.services.spawn_registry import sweep_orphans as _spawn_sweep
        await _spawn_sweep()
    except Exception:
        logging.getLogger(__name__).warning("spawn sweep_orphans failed", exc_info=True)
    # P0 TMA-NM: a tenant id without the experimental opt-in is a refusal —
    # the filesystem segment is not an isolation guarantee (no DB/RLS).
    from app.services.tenant_service import (
        multi_tenant_experimental as tenant_mode_enabled,
        tenant_config_errors,
    )
    _tenant_errors = tenant_config_errors()
    if _tenant_errors:
        raise RuntimeError("Unsafe tenant configuration: " + "; ".join(_tenant_errors))
    if (
        (config.memory_provenance_strict or tenant_mode_enabled())
        and not config.memory_authority_key_ready
    ):
        raise RuntimeError(
            "memory authority flags require a non-dev >=32-char "
            "[security].memory_hmac_key (or a strong JWT secret): "
            "the tracked dev key can forge origin=user"
        )
    if not str(config.security.get("memory_hmac_key", "") or "").strip():
        logger.warning(
            "SECURITY: memory provenance HMAC derives from jwt_secret_key; "
            "set [security].memory_hmac_key for independent rotation"
        )
    if config.tma_mode != "off":
        try:
            from app.services.verdict_log_service import verdict_key_status
            if verdict_key_status() == "dev":
                logger.warning(
                    "SECURITY: TMA verdict chain uses a dev/public HMAC key "
                    "(key_id=v1-dev); the audit chain is structurally valid but forgeable"
                )
        except Exception:
            logger.debug("verdict key status check failed", exc_info=True)
    # P0 S2: production-only fail-closed guard.  Local dev (WT_ENV unset)
    # keeps the tracked single-user bypass choice; a public deployment opts in
    # with WT_ENV=production and must not boot from the unsafe template.
    _prod_errors = production_security_errors(config)
    if _prod_errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(_prod_errors))
    # 2026-11-11 补救清单 #3（AUDIT_20261111 §3 M-2）：安全门开关被严格解析纠正或
    # 拒绝时，启动日志必须点名——否则 `registration_open = "false"` 这类笔误会让
    # 站点静默保持开放注册，运维却以为已经关掉。
    for _sec_warn in security_config_warnings(config):
        logger.warning("SECURITY CONFIG: %s", _sec_warn)
    if config.super_admin_bypass and not os.environ.get("WT_ENV"):
        logger.warning(
            "SECURITY: super_admin_bypass=true is active with WT_ENV unset. "
            "This is a single-user/local-dev choice; public deployments must set "
            "WT_ENV=production (the startup guard then rejects host-level bypass)."
        )

    # W18 G8（PLAN §11）：多 worker 自检——设备执行注册表/配对码为进程内态，
    # workers>1 即禁用 client_exec 并 ERROR 告警。
    from app.services.device_session_service import guard_multiworker_client_exec
    guard_multiworker_client_exec()

    # conv-open 性能治理 P1-1（DESIGN_conv_open_performance）：所有 Message 写点
    # 的超限 tool_results/reasoning_content/tool_calls 自动外置到 message_payloads。
    from app.services.message_payload_service import install_message_payload_hook
    install_message_payload_hook()

    # 2026-11-01: [mcp.servers.*] hot reload (config.toml watch; no restart).
    try:
        from app.tools.mcp_client import start_mcp_hot_reload
        start_mcp_hot_reload()
    except Exception:
        logger.warning("MCP hot reload startup failed (tools still load once)", exc_info=True)

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

    # 部署排空令牌（2026-10-06，A4.9 r1 I5）：每启动生成随机令牌落盘
    # backend/.drain_token（0600）——stop.sh 读取后经 X-Drain-Token 头调用
    # drain 端点；令牌闸使本机恶意网页/反代拓扑无法打进 drain。
    try:
        from app.api import server_admin as _server_admin
        _drain_tok = _server_admin.new_drain_token()
        _server_admin.set_drain_token(_drain_tok)
        _tok_path = os.path.join(os.path.dirname(__file__), ".drain_token")
        # scoped 复审 Minor：写即 0600（os.open mode 原子生效），消除
        # write-then-chmod 的权限窗口。
        _fd = os.open(_tok_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(_drain_tok)
    except Exception:
        logger.exception("drain token bootstrap failed (fail-open; drain 端点将拒绝无令牌调用)")

    # sync 波（2026-09-26 自 weave-thinker-client 上游）：注册 ORM 变更捕获；
    # [sync].enabled 在发射期逐事件读取（端点侧同配置请求期 404），SIGHUP 热改不脱钩。
    # W4 项5（2026-10-06）：注册必须先于 boot sweep——sweep 经 ORM 翻转/删除，
    # mapper 事件在 sweep 提交的 flush 里触发，晚注册=事件时零监听器=零 SyncEvent
    # （残稿依旧跨端不可见，A4.9 双评审 Critical 实证）。
    from app.services.sync_capture import register_sync_capture
    register_sync_capture()

    # 渐进落库 sweep（2026-10-06，conv ae9aa092）：上次进程死亡（部署强杀/
    # 崩溃/OOM）遗留的 streaming 在途行全部翻 interrupted——用户刷新后看到
    # 部分正文+「已中断」徽标，而不是整段蒸发。fail-open 不阻塞启动。
    try:
        from app.api.chat import mark_interrupted_streams
        from app.db.database import AsyncSessionLocal as _SweepSession
        async with _SweepSession() as _sweep_db:
            _swept = await mark_interrupted_streams(_sweep_db)
            await _sweep_db.commit()
        if _swept:
            logger.warning("boot sweep: %d streaming message(s) marked interrupted", _swept)
    except Exception:
        logger.exception("boot sweep mark_interrupted_streams failed (fail-open)")

    # 死磕 DAG 波次 W2d：进程重启后残留的 active 目标循环不可能存活（SSE
    # 驱动随进程消失）——统一停泊为 paused + PAUSED 包，避免幽灵 active 状态。
    try:
        from app.services.deathmatch_service import recover_stale_active_deathmatch
        async with AsyncSessionLocal() as _dm_session:
            _dm_recovered = await recover_stale_active_deathmatch(_dm_session)
            if _dm_recovered:
                await _dm_session.commit()
                logger.info(
                    "deathmatch: normalized %d stale active conversation(s) to paused",
                    _dm_recovered,
                )
    except Exception as exc:
        logger.warning("deathmatch stale-active recovery failed: %s", exc)

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
    # durable job runner——启动对账（exit_code 落盘 finalize/
    # pid 活接管/否则 unknown）后进入轮询；5h+ 重作业靠 detached 子进程跨重启存活。
    from app.services.job_runner_service import JobRunnerWorker
    global _durable_job_worker
    _durable_job_worker = JobRunnerWorker()
    await _durable_job_worker.start()
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
    # vibeweaver 执法层 hook 安装（gate: tool.after + stop；幂等、fail-open）
    try:
        from app.services.vibeweaver_gate_service import install_hooks as _vw_install
        _vw_summary = _vw_install()
        logger.info("vibeweaver gate hooks: %s", _vw_summary)
        from app.services.vibeweaver_audit_service import install_hooks as _vw_audit_install
        _vw_audit_summary = _vw_audit_install()
        logger.info("vibeweaver audit hooks: %s", _vw_audit_summary)
        # D3：stop-guard 第三只 stop 钩子（gate/audit 之后 = when-would-allow）
        from app.services.vibeweaver_stop_guard import install_hooks as _vw_sg_install
        _vw_sg_summary = _vw_sg_install()
        logger.info("vibeweaver stop-guard hooks: %s", _vw_sg_summary)
    except Exception:
        logger.exception("vibeweaver hook installation failed (fail-open)")
    # 循环 Governor v2（设计 docs/DESIGN_20261025_loop_guard_v2.md §A5）：
    # Tier A 运行时防线**无条件挂载**——关掉 vibeweaver 不再连产品防线一起关掉。
    try:
        from app.services.loop_governor_service import install as _lgov_install
        logger.info("loop governor: %s", _lgov_install())
    except Exception:
        logger.exception("loop governor installation failed (fail-open)")


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
    # durable job worker 停轮询（detached 子进程不连带杀死——作业跨重启存活）
    try:
        if _durable_job_worker is not None:
            await _durable_job_worker.stop()
    except Exception:
        pass
    from app.services.memory_scheduler import memory_scheduler
    await memory_scheduler.stop()
    try:
        from app.services.interactive_browser_service import InteractiveBrowserService
        await InteractiveBrowserService.get_instance().shutdown()
    except Exception:
        pass
    # A4.9 I-2: 进程会话的 OS 子进程在 start_new_session 下独立于服务进程，
    # 服务退出/重启时必须显式清理，否则成为无法再被 kill 的孤儿进程。
    try:
        from app.services.process_session_service import process_manager
        await process_manager.shutdown_all()
    except Exception:
        pass
    # F2（2026-10-26）：回收登记在案的工具子进程组（terminal/code_exec 后台子孙）
    try:
        from app.services.spawn_registry import shutdown_all as _spawn_shutdown_all
        await _spawn_shutdown_all()
    except Exception:
        logging.getLogger(__name__).warning("spawn shutdown_all failed", exc_info=True)
    # fix-round I3：死亡模式写-through drain 接停机——在飞目标列 UPDATE
    # 不得因停机丢失（B-8/D-15 落库面）；5s 超时兜底，drain 挂死不拖停机。
    try:
        from app.services.deathmatch_write_through import drain_write_through
        await asyncio.wait_for(drain_write_through(), timeout=5)
    except Exception:
        pass
    try:
        from app.tools.mcp_client import stop_mcp_hot_reload
        stop_mcp_hot_reload()
    except Exception:
        pass
    await close_shared_async_client()


static_dir = os.path.join(os.path.dirname(__file__), "static")
index_path = os.path.join(static_dir, "index.html")


@app.post("/api/admin/reload-config")
async def reload_config(request: Request):
    """重载配置（2026-11-11 补救 #5a）：独立密钥 + 恒定时间比较 + 真正重载模型注册表。

    此前用 `x-admin-secret != [security].jwt_secret_key` 做校验——一密两用（JWT 签名
    密钥兼作管理口令）且非恒定时间比较；且只清配置缓存、**不**重载模型注册表
    （2026-09-04 审计「三条热重载链路全断」的遗留项）。

    未配置 `[security] admin_reload_secret` 时端点 fail-closed（403），不复用 JWT 密钥。
    """
    from fastapi import HTTPException, status

    expected = str(get_config().security.get("admin_reload_secret", "") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reload-config 未启用：请在 config.toml [security] 配置独立的 admin_reload_secret",
        )
    supplied = request.headers.get("x-admin-secret", "") or ""
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # 先验证新配置能解析，再清缓存：否则一个畸形的 config.toml 会让全站 get_config()
    # 抛错（评审 C Important）。校验失败时旧缓存原样保留，服务继续可用。
    from app.core.config import Config

    try:
        Config()
    except Exception as exc:  # noqa: BLE001 — 必须让运维看见具体解析错误
        logger.error("reload-config: 新配置无法解析，已保留旧配置: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"新配置无法解析，已保留旧配置：{exc}",
        )

    clear_config_cache()
    try:
        from app.model_gateway.registry import reload_model_registry

        reload_model_registry()
    except Exception as exc:  # noqa: BLE001 — 重载失败必须让运维看见，不能静默
        logger.error("reload-config: 模型注册表重载失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"配置缓存已清，但模型注册表重载失败（旧注册表已保留）：{exc}",
        )
    return {"status": "ok", "model_registry": "reloaded"}


# 2026-09-19: content-hashed bundles are safe to cache forever; index.html
# must always revalidate, otherwise a long-open tab (or a heuristically cached
# document) keeps running a stale build after a deploy — symptom seen as
# "office previews render blank" while the server already served new PDFs.
# Vite/Terser hashes are base64url and may contain "-" (even trailing);
# scope to assets/ so hand-written files can never be pinned immutable.
_HASHED_ASSET_RE = re.compile(r"^assets/[^/]+-[A-Za-z0-9_-]{4,}\.[a-z0-9]+$")


def _static_cache_headers(full_path: str) -> dict:
    if _HASHED_ASSET_RE.search(full_path):
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "no-cache, must-revalidate"}


# SPA catch-all: serve index.html for client-side routes
@app.get("/app/frontend/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path:
        # Resolve the real path and ensure it stays within static_dir
        file_path = os.path.realpath(os.path.join(static_dir, full_path))
        if file_path.startswith(os.path.realpath(static_dir) + os.sep) and os.path.isfile(file_path):
            return FileResponse(file_path, headers=_static_cache_headers(full_path))
    # Serve index.html for client-side routing
    if os.path.isfile(index_path):
        return FileResponse(
            index_path,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
    return {"detail": "Not Found"}


@app.get("/")
def root(request: Request):
    frontend_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(frontend_path):
        # 保留深链 query（?conv= 等）：8159 vite 直接服务 `/` 不丢参，
        # 生产 8158 若丢参则所有走根路径的深链（任务通知/分享链接）失效。
        query = request.url.query
        target = f"/app/frontend/?{query}" if query else "/app/frontend/"
        return RedirectResponse(url=target)
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
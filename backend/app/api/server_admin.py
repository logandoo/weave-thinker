# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""部署排空端点（2026-10-06，conv ae9aa092 修复波 P2）。

stop.sh/deploy 重启前排空在途 run：先 POST /api/server/drain 置 draining
（chat 新 run 入口 503 拒收，避免「边排边进」），再轮询 GET active-runs
至 0 或超时（超时照杀——有渐进落库兜底，被杀不再丢数据）。

安全（A4.9 r1 I5 加固）：双闸——① 共享令牌：`X-Drain-Token` 头必须匹配
启动期生成的随机令牌（落盘 backend/.drain_token 0600，stop.sh 读取；
每次启动轮换）；② loopback-only（直连形态下纵深）。令牌缺失/不符即 403
——本机恶意网页无法再用 CORS `*` + loopback 打进 drain（持续 503 DoS）。

部署约束：loopback 闸假定**不经同机反代暴露** 8158（反代会把所有请求的
peer 变成 127.0.0.1 使闸失效——此时令牌闸仍是唯一防线，令牌不泄漏即安全）。
"""
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.active_agent_registry import ActiveAgentRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/server", tags=["server"])

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# 进程级 drain 令牌（启动期由 main.py set_drain_token 注入；测试可直接设置）。
_drain_token: str = ""


def set_drain_token(token: str) -> None:
    global _drain_token
    _drain_token = token or ""


def new_drain_token() -> str:
    return secrets.token_hex(32)


def _loopback_only(request: Request) -> None:
    host = ""
    try:
        host = (request.client.host if request.client else "") or ""
    except Exception:
        host = ""
    if host not in _LOOPBACK_HOSTS:
        logger.warning("server_admin: non-loopback access rejected from %r", host)
        raise HTTPException(status_code=403, detail="loopback only")


def _check_auth(request: Request) -> None:
    """令牌闸（主）+ loopback 闸（纵深）。令牌未配置 = 启动 bootstrap 失败
    ——scoped 复审 Minor：此时若退化回仅 loopback，会重新打开「本机恶意网页
    CORS * + loopback → drain DoS」向量；直接 503 拒绝服务（fail-closed）。"""
    _loopback_only(request)
    if not _drain_token:
        raise HTTPException(status_code=503, detail="drain token not configured")
    if request.headers.get("X-Drain-Token", "") != _drain_token:
        logger.warning("server_admin: bad drain token from %r",
                       request.client.host if request.client else None)
        raise HTTPException(status_code=403, detail="bad drain token")


class DrainRequest(BaseModel):
    on: bool = True


@router.post("/drain")
async def set_drain(req: DrainRequest, request: Request):
    _check_auth(request)
    reg = ActiveAgentRegistry.get_instance()
    await reg.set_draining(req.on)
    logger.info("server drain %s (active=%d)", "ON" if req.on else "OFF",
                await reg.active_run_count())
    return {"draining": reg.is_draining}


async def _active_run_total() -> int:
    """排空计数三源求和（D-12，2026-10-06）：交互 chat（registry 含预约槽）+
    后台 running 任务 + 语音在途会话——stop.sh 排空等待覆盖这三面。"""
    from app.services import voice_service
    from app.services.agent_worker import agent_worker
    reg = ActiveAgentRegistry.get_instance()
    return (await reg.active_run_count()
            + agent_worker.running_count()
            + voice_service.active_session_count())


@router.get("/active-runs")
async def active_runs(request: Request):
    _check_auth(request)
    reg = ActiveAgentRegistry.get_instance()
    return {"draining": reg.is_draining, "active": await _active_run_total()}

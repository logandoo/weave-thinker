# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W16 设备执行模式 — 设备 WS 会话管理（DeviceSessionManager）。

每设备一条出站长连接（/api/device/ws）；call_id→Future 的 RPC 往返、
心跳时效判活、断线 fail-fast（at-most-once，不重放写操作）。
单进程约束：内存注册表与 HTTP 面同进程（PLAN §6.3/§9）。

A4.9 C-1 修复：register/unregister/handle_message 全部按会话对象做身份校验
——旧连接迟到的 finally 不得拆掉新连接的会话；register 时关旧 socket。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# 传输上限（A4.9 I-11）：单 transfer 分块数/累计字节，防设备侧撑爆内存
MAX_TRANSFER_CHUNKS = 4096
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_MESSAGE_CHARS = 24 * 1024 * 1024
# D2（遗留清零）：分块汇编的空闲窗附加量——每收到新块按「max(timeout,1)+本值」
# 前移截止（慢链路大量分块不被固定 deadline 掐死）；测试可 monkeypatch。
TRANSFER_IDLE_EXTRA_SECONDS = 5.0


class DeviceOfflineError(Exception):
    pass


class DeviceCallTimeoutError(Exception):
    pass


class DeviceCallError(Exception):
    """设备调用错误。code=wire 结构化错误码（error_code 字段）——消费面
    classify_wire_error(code=...) 优先用它，防错误文本伪装前缀（评审 r3/r4 I-b）。"""

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        self.code = code


class DeviceOversizeError(DeviceCallError):
    """字节超限（HTTP 映射 413，区别于一般设备错误 502）。"""
    pass


@dataclass
class DeviceSession:
    device_id: str
    user_id: str
    ws: Any
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)
    last_seen: datetime = field(default_factory=datetime.utcnow)
    closed: bool = False
    transfers: Dict[str, list] = field(default_factory=dict)
    transfer_bytes: Dict[str, int] = field(default_factory=dict)
    claimed_transfers: set = field(default_factory=set)
    poisoned_transfers: set = field(default_factory=set)
    # ② progress 续期（2026-10-11）：call_id → {window, started, deadline}。
    # 设备长打包/转换（fs/zip·fs/office-*）期间发 progress 即按原窗前移截止；
    # 无 progress 时超时语义与旧实现一致；D1：deadline ≤ started+cap（硬上限）。
    pending_waits: Dict[str, Dict[str, float]] = field(default_factory=dict)


class DeviceSessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, DeviceSession] = {}

    def device_user_id(self, device_id: str) -> Optional[str]:
        """在线会话的属主 user_id（无会话=None）。C1 属主栅栏用：派发按 device_id
        盲路由，硬删释放的 id 被他人抢占时靠此拦截劫持。"""
        sess = self._sessions.get(device_id)
        return getattr(sess, "user_id", None) if sess is not None else None

    def register(self, device_id: str, user_id: str, ws: Any) -> DeviceSession:
        old = self._sessions.pop(device_id, None)
        if old is not None and not old.closed:
            old.closed = True
            self._fail_pending(old, DeviceOfflineError("设备连接被新连接取代"))
            # A4.9 C-1：关旧 socket，避免僵尸 receive loop 继续往会话写
            self._close_ws(old.ws)
        sess = DeviceSession(device_id=device_id, user_id=user_id, ws=ws)
        self._sessions[device_id] = sess
        logger.info("device executor connected: %s (user=%s)", device_id, user_id)
        return sess

    @staticmethod
    def _close_ws(ws: Any) -> None:
        try:
            close = getattr(ws, "close", None)
            if close is not None:
                res = close(code=1012)
                if asyncio.iscoroutine(res):
                    try:
                        asyncio.get_running_loop().create_task(res)
                    except RuntimeError:
                        res.close()
        except Exception:
            pass

    def unregister(self, device_id: str, session: Optional[DeviceSession] = None) -> None:
        """A4.9 C-1：传入 session 时只拆该会话（身份校验），迟到的旧连接 finally
        不得误拆新连接。r4 NEW-M2：拆会话同时关 socket（僵尸 receive loop 不滞留）。"""
        current = self._sessions.get(device_id)
        if session is not None and current is not session:
            if session is not None and not session.closed:
                session.closed = True
                self._fail_pending(session, DeviceOfflineError("设备连接已被新连接取代"))
                self._close_ws(session.ws)
            return
        if current is not None:
            current.closed = True
            self._sessions.pop(device_id, None)
            self._fail_pending(current, DeviceOfflineError("设备已断开"))
            self._close_ws(current.ws)
            logger.info("device executor disconnected: %s", device_id)

    def is_online(self, device_id: str) -> bool:
        sess = self._sessions.get(device_id)
        return sess is not None and not sess.closed and self._fresh(sess)

    def _fresh(self, sess: DeviceSession) -> bool:
        """心跳时效（A4.9 I-12）：超过 heartbeat*2+2s 未见到消息=半开连接判离线。"""
        try:
            from app.core.config import get_config
            ttl = get_config().client_exec_heartbeat_seconds * 2 + 2
        except Exception:
            ttl = 62
        return (datetime.utcnow() - sess.last_seen).total_seconds() <= ttl

    def get(self, device_id: str) -> Optional[DeviceSession]:
        return self._sessions.get(device_id)

    def online_device_ids(self) -> set:
        return {
            d for d, s in self._sessions.items()
            if not s.closed and self._fresh(s)
        }

    async def rpc_call(
        self,
        device_id: str,
        method: str,
        params: Dict[str, Any],
        timeout: Optional[float] = None,
        expected_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        sess = self._sessions.get(device_id)
        if sess is None or sess.closed:
            raise DeviceOfflineError("设备已离线")
        if not self._fresh(sess):
            raise DeviceOfflineError("设备心跳超时（连接疑似半开）")
        # NEW-I2（r4）：send 时原子复核会话属主——栅栏读到 send 之间的
        # check-then-act 窗（skill_run_script 解析可达 10-50ms）在此闭合。
        if expected_user_id is not None and getattr(sess, "user_id", None) != expected_user_id:
            raise DeviceCallError("设备身份不匹配（安全拦截）", code="device_owner_mismatch")
        call_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        sess.pending[call_id] = fut
        window: Optional[float] = None if timeout is None else float(timeout)
        if window is not None:
            started = time.monotonic()
            cap = self._call_max_extension_seconds()
            deadline = started + window
            if cap > 0:
                deadline = min(deadline, started + cap)
            sess.pending_waits[call_id] = {
                "window": window, "started": started, "deadline": deadline,
            }
        try:
            await sess.ws.send_json(
                {"jsonrpc": "2.0", "id": call_id, "method": method, "params": params}
            )
        except Exception as exc:  # send 失败=连接已死
            self._drop_call_waits(sess, call_id)
            sess.pending.pop(call_id, None)
            self.unregister(device_id, sess)
            raise DeviceOfflineError("设备连接失败") from exc
        try:
            if window is None:
                # 旧语义：无显式 timeout = 不设截止（调用方自担；fetch_file_bytes
                # 等生产路径均已显式传窗，A4.9 I-1）。
                return await fut
            while True:
                wait = sess.pending_waits.get(call_id)
                if wait is None:
                    # 防御（A4.9 r1 I-3）：续期表被并发清时不得降级为无界等待
                    # （I-1）——已完成的 future 透传真实异常，否则按超时。
                    if fut.done():
                        return fut.result()
                    raise asyncio.TimeoutError()
                deadline = wait["deadline"]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                try:
                    # shield：wait_for 超时只取消外层，不取消 fut（续期后继续等）
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=remaining)
                except asyncio.TimeoutError:
                    # 期间收到 progress 续期（截止前移）→ 按新窗继续等
                    new_wait = sess.pending_waits.get(call_id)
                    if new_wait is not None and new_wait["deadline"] > deadline:
                        continue
                    raise
        except asyncio.TimeoutError as exc:
            # A4.9 I-7：超时尽力通知设备取消（杀在途进程），不误报已执行
            try:
                await sess.ws.send_json({
                    "jsonrpc": "2.0", "id": uuid.uuid4().hex,
                    "method": "tools/cancel", "params": {"call_id": call_id},
                })
            except Exception:
                pass
            raise DeviceCallTimeoutError("设备执行超时") from exc
        finally:
            self._drop_call_waits(sess, call_id)
            sess.pending.pop(call_id, None)

    @staticmethod
    def _drop_call_waits(sess: DeviceSession, call_id: str) -> None:
        """续期状态随调用结束清理（成功/超时/取消/断线出口统一走 finally）。"""
        sess.pending_waits.pop(call_id, None)

    @staticmethod
    def _call_max_extension_seconds() -> float:
        """D1：续期绝对上限（0=off）——配置读取失败按默认 3600 兜底。"""
        try:
            from app.core.config import get_config
            return float(get_config().client_exec_call_max_extension_seconds)
        except Exception:
            return 3600.0

    def _renew_progress_deadline(self, sess: DeviceSession, params: Dict[str, Any]) -> None:
        """② progress {call_id, note} → 续期（2026-10-11）。

        设备在长打包/转换（fs/zip·fs/office-*）期间发 progress 即按**原窗**把
        该调用的等待截止前移 `now + window`；未注册窗口（无超时调用/未知 id）
        一律忽略。无 progress 时截止不动 → 仍按原窗超时（不松有界性）。
        """
        call_id = str(params.get("call_id") or "")
        wait = sess.pending_waits.get(call_id) if call_id else None
        if wait is None:
            return
        new_deadline = time.monotonic() + wait["window"]
        cap = self._call_max_extension_seconds()
        if cap > 0:
            new_deadline = min(new_deadline, wait["started"] + cap)
        if new_deadline > wait["deadline"]:
            wait["deadline"] = new_deadline
            import re as _re
            note = _re.sub(r"[\x00-\x1f\x7f]", " ", str(params.get("note") or ""))[:60]
            logger.info(
                "device progress renewal: %s call=%s deadline+%.0fs note=%s",
                sess.device_id, call_id[:8],
                wait["deadline"] - time.monotonic(), note,
            )

    def handle_message(
        self, session: Union[str, DeviceSession], msg: Any
    ) -> None:
        sess = session if isinstance(session, DeviceSession) else self._sessions.get(session)
        if sess is None or sess.closed or not isinstance(msg, dict):
            return
        sess.last_seen = datetime.utcnow()
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = sess.pending.pop(str(msg["id"]), None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg.get("error") or {}
                    fut.set_exception(
                        DeviceCallError(str(err.get("message", err))[:500])
                    )
                else:
                    fut.set_result(msg.get("result") if msg.get("result") is not None else {})
            return
        method = msg.get("method")
        if method == "heartbeat":
            return
        if method == "progress":
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                return
            self._renew_progress_deadline(sess, params)
            return
        if method == "chunk":
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                # A4.9 r1 #1：畸形帧不得抛 AttributeError 掀翻会话（杀在途调用）
                return
            self._on_chunk(sess, params)
            return
        logger.debug("device %s sent unknown message method=%s", sess.device_id, method)

    def _on_chunk(self, sess: DeviceSession, params: Dict[str, Any]) -> None:
        transfer_id = str(params.get("transfer_id") or "")
        if not transfer_id:
            return
        # A4.9 I-5/M1：分块数/累计字节上限——超限毒化该 transfer（汇编端必拒），
        # 不再静默丢块/清缓冲（截断文件曾可静默送达）
        chunks = sess.transfers.setdefault(transfer_id, [])
        if len(chunks) >= MAX_TRANSFER_CHUNKS:
            sess.poisoned_transfers.add(transfer_id)
            sess.transfers.pop(transfer_id, None)
            sess.transfer_bytes.pop(transfer_id, None)
            return
        data_len = len(str(params.get("data_b64") or ""))
        total = sess.transfer_bytes.get(transfer_id, 0) + data_len
        if total > MAX_TRANSFER_BYTES:
            sess.poisoned_transfers.add(transfer_id)
            sess.transfers.pop(transfer_id, None)
            sess.transfer_bytes.pop(transfer_id, None)
            return
        sess.transfer_bytes[transfer_id] = total
        chunks.append(params)

    def collect_transfer(self, device_id: str, transfer_id: str) -> list:
        sess = self._sessions.get(device_id)
        if sess is None:
            return []
        sess.transfer_bytes.pop(transfer_id, None)
        return sess.transfers.pop(transfer_id, [])

    def claim_transfer(self, device_id: str, transfer_id: str) -> bool:
        """A4.9 I-11：同一 transfer 只允许一个汇编者（防并发下载互偷缓冲）。"""
        sess = self._sessions.get(device_id)
        if sess is None:
            return False
        if transfer_id in sess.claimed_transfers:
            return False
        # r2 #3：有界拒新（不再 clear 活跃 claim——单汇编者不变量优先）
        if len(sess.claimed_transfers) > 4096:
            return False
        sess.claimed_transfers.add(transfer_id)
        return True

    def release_transfer(self, device_id: str, transfer_id: str) -> None:
        """A4.9 M1：汇编结束（成功/失败）释放 claim——在途保护窗口仅覆盖汇编期。"""
        sess = self._sessions.get(device_id)
        if sess is not None:
            sess.claimed_transfers.discard(transfer_id)
            sess.poisoned_transfers.discard(transfer_id)  # r2：毒化集同步清理，防无界增长

    def _fail_pending(self, sess: DeviceSession, exc: Exception) -> None:
        for fut in list(sess.pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        sess.pending.clear()
        sess.pending_waits.clear()

    async def fetch_file_bytes(
        self,
        device_id: str,
        method: str,
        params: Dict[str, Any],
        max_bytes: Optional[int] = None,
        timeout: Optional[float] = None,
        expected_user_id: Optional[str] = None,
    ) -> bytes:
        """W19 通用设备字节通道：fs/download|fs/zip|fs/office-* 共用。

        result 形状（PLAN §5）：`{ok, data_b64, sha256?, size?}` 单包，或
        `{ok, transfer_id, sha256?, size?}` + chunk 通知（逐块 sha256、末块 last）。
        大小闸前置（base64 估算再解码）+ 逐块校验 + seq 连续性 + 单汇编者。
        A4.9 I-1：RPC 等待有界（config call_timeout，不再 timeout=None 永等）。"""
        import base64
        import hashlib

        try:
            from app.core.config import get_config
            cfg = get_config()
            if max_bytes is None:
                max_bytes = cfg.client_exec_max_payload_mb * 1024 * 1024
            if timeout is None:
                timeout = float(cfg.client_exec_call_timeout_seconds)
        except Exception:
            max_bytes = max_bytes or 10 * 1024 * 1024
            timeout = timeout or 120.0
        result = await self.rpc_call(
            device_id, method, params, timeout=timeout, expected_user_id=expected_user_id
        )
        if not isinstance(result, dict) or result.get("ok") is False:
            raise DeviceCallError(
                str((result or {}).get("error") or "设备读取失败"),
                code=(result or {}).get("error_code"),
            )
        data_b64 = result.get("data_b64")
        if isinstance(data_b64, str):
            if len(data_b64) > (max_bytes * 4 // 3 + 64):
                raise DeviceOversizeError("文件超过设备代理上限")
            try:
                data = base64.b64decode(data_b64)
            except Exception as exc:
                raise DeviceCallError("设备文件解码失败") from exc
        else:
            data = await self._assemble_transfer(
                device_id, str(result.get("transfer_id") or ""), max_bytes, timeout
            )
        if len(data) > max_bytes:
            raise DeviceOversizeError("文件超过设备代理上限")
        expected = result.get("sha256")
        if isinstance(expected, str) and expected:
            if hashlib.sha256(data).hexdigest() != expected:
                raise DeviceCallError("设备文件校验失败")
        return data

    async def _assemble_transfer(
        self, device_id: str, transfer_id: str, max_bytes: int, timeout: float
    ) -> bytes:
        import base64
        import hashlib

        if not transfer_id:
            raise DeviceCallError("设备返回了无法解析的下载结果")
        if self._sessions.get(device_id) is None:
            raise DeviceOfflineError("设备已离线")
        if not self.claim_transfer(device_id, transfer_id):
            raise DeviceCallError("该文件传输已在进行中")
        started = asyncio.get_event_loop().time()
        idle_window = max(timeout, 1.0) + TRANSFER_IDLE_EXTRA_SECONDS
        cap = self._call_max_extension_seconds()
        hard_deadline = (started + cap) if cap > 0 else None
        deadline = started + idle_window
        if hard_deadline is not None:
            deadline = min(deadline, hard_deadline)
        assembled: list = []
        try:
            while asyncio.get_event_loop().time() < deadline:
                sess = self._sessions.get(device_id)
                if sess is not None and transfer_id in getattr(sess, "poisoned_transfers", set()):
                    raise DeviceOversizeError("设备文件传输被中止（分块超限）")
                got = self.collect_transfer(device_id, transfer_id)
                if got:
                    # D2：块=活性信号——按 idle 窗前移截止（慢传不被固定窗掐死）
                    assembled.extend(got)
                    new_deadline = asyncio.get_event_loop().time() + idle_window
                    if hard_deadline is not None:
                        new_deadline = min(new_deadline, hard_deadline)
                    if new_deadline > deadline:
                        deadline = new_deadline
                if assembled and any(c.get("last") for c in assembled):
                    ordered = sorted(assembled, key=lambda c: int(c.get("seq") or 0))
                    # A4.9 I-5：seq 连续性——缺块/重块不得静默拼接
                    for i, c in enumerate(ordered):
                        if int(c.get("seq") or 0) != i:
                            raise DeviceCallError("设备文件分块序号不连续")
                    est = sum(len(str(c.get("data_b64") or "")) * 3 // 4 for c in ordered)
                    if est > max_bytes:
                        raise DeviceOversizeError("文件超过设备代理上限")
                    try:
                        data = b"".join(
                            base64.b64decode(c.get("data_b64") or "") for c in ordered
                        )
                    except Exception as exc:
                        raise DeviceCallError("设备文件解码失败") from exc
                    for c in ordered:
                        chunk_sha = c.get("sha256")
                        chunk_raw = base64.b64decode(c.get("data_b64") or "")
                        if isinstance(chunk_sha, str) and chunk_sha:
                            if hashlib.sha256(chunk_raw).hexdigest() != chunk_sha:
                                raise DeviceCallError("设备文件分块校验失败")
                    if len(data) > max_bytes:
                        raise DeviceOversizeError("文件超过设备代理上限")
                    return data
                await asyncio.sleep(0.05)
            raise DeviceCallTimeoutError("设备文件传输超时")
        finally:
            self.release_transfer(device_id, transfer_id)


device_session_manager = DeviceSessionManager()


def guard_multiworker_client_exec(env: Optional[dict] = None) -> bool:
    """W18 G8（PLAN §11 启动自检）：多 worker 部署下内存注册表/配对码进程内失效
    ——检测 WEB_CONCURRENCY/WT_WORKERS>1 即禁用 client_exec 并 ERROR 告警。
    返回 True=单 worker（保持启用）。"""
    import os

    env = env if env is not None else os.environ
    raw = env.get("WEB_CONCURRENCY") or env.get("WT_WORKERS") or env.get("UVICORN_WORKERS") or "1"
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        workers = 1
        logger.warning("multi-worker guard: unparseable workers value %r — assuming 1", raw)
    if workers <= 1:
        return True
    try:
        from app.core.config import get_config

        cfg = get_config()
        section = cfg._config.setdefault("client_exec", {})
        section["enabled"] = False
    except Exception:
        logger.exception("failed to disable client_exec for multi-worker deployment")
    logger.error(
        "client_exec disabled: multi-worker deployment (%d workers) is not supported "
        "for device execution (process-local session registry / pairing codes) — "
        "see docs/PLAN_device_execution_mode.md §11",
        workers,
    )
    return False

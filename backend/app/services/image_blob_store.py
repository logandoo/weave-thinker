# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""图片字节 blob 存储（2026-11-09 图片信封零损失内存改造 · spec §4.1/§4.2/§4.3）。

一句话：把消息里那份 base64 图片**副本**换成一个 `blob://<uid8>/sha256/<64hex>`
指针，字节落到服务端自有目录；上线前在唯一必经点（`expand_tool_images`）物化回
**与今天逐字节相同**的 data URL —— 模型看到的东西不变（HC-1），变的只是驻留。

安全不变量（照 can1357 `blob-artifact-architecture.md` 的 `parseBlobRef()` 采纳，
spec §2/§4.6）：**任何路径拼接之前**先用正则白名单校验 ref，不匹配即拒绝。
路径完全由 `uid8 + sha` 派生，外部永远无法注入路径片段。

授权面（spec §4.1，v2 关键修订）：`MessagePayload` 的"sha 不作寻址"禁令有两条
理由，第 ② 条（伪造 ref 越权读他人内容）对文件系统 blob 同样成立 ⇒ ref 里的
`uid8` 必须等于本轮消息归属用户的命名空间，否则拒绝，不读、不降级。

失败语义（HC-3）：拿不到**完整**字节就抛 `ImageBlobMaterializeError` 终止本轮，
绝不以文本/占位/裸 ref 继续请求——静默降级是最隐蔽的降质（P0.5 前车：
`force=True` + raw 截断产出 2,529 chars 不可解析 JSON 并丢图）。
"""
from __future__ import annotations

import asyncio
import base64
import errno
import fcntl
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

BLOB_SCHEME = "blob://"
BLOB_REF_RE = re.compile(r"^blob://([0-9a-f]{8})/sha256/([0-9a-f]{64})$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UID8_RE = re.compile(r"^[0-9a-f]{8}$")

# 与 tool_image_results.MAX_INLINE_BYTES 同顶（单图 8MB）；物化后 base64 上限 = 4/3
_MAX_BLOB_BYTES = 8 * 1024 * 1024

GC_LOCK_NAME = ".gc.lock"


# ---------------------------------------------------------------------------
# 指标旁路（A4.9 二轮评审 A·C1）
#
# `materialize_frame_sync` 是 HC-3 的 fail-closed 通道：它 `raise` 之前必须先把
# 失败**记下来**，所以指标调用点不能包在 `except Exception` 里（那正是"静默丢弃
# 失败信号"）。但反过来，指标模块自己出任何问题（ImportError / 属性缺失）都绝不能
# 把数据路径炸掉——那会让 blob 永远写不进去，而唯一记录这件事的计数器恰好就是
# 坏掉的那个，运维永远看不见。
#
# 折中：**响亮地降级一次**。首次导入失败打一条 ERROR（进 chatllm.log/journald，
# grep 得到），之后走 no-op 桩；数据路径继续按设计工作（退回内联 = 今天的形态）。
# ---------------------------------------------------------------------------


class _NoopMetrics:
    """指标不可用时的替身：所有方法都是空操作，但失败本身已经报过一次。"""

    def __getattr__(self, _name: str):
        return lambda *a, **k: None


_metrics_mod: Any = None
_metrics_failed = False


def _metrics():
    global _metrics_mod, _metrics_failed
    if _metrics_mod is not None:
        return _metrics_mod
    if _metrics_failed:
        return _NOOP_METRICS
    try:
        from app.services import image_blob_metrics as _mod

        _metrics_mod = _mod
        return _mod
    except Exception as exc:  # ImportError 之外还有属性/初始化异常
        _metrics_failed = True
        logger.error(
            "image blob metrics unavailable (%s: %s) — 图片 blob 可观测层降级为 no-op；"
            "数据路径仍按设计工作（写失败退回内联），但 image_blob_* 指标不再更新",
            type(exc).__name__, exc,
        )
        return _NOOP_METRICS


_NOOP_METRICS = _NoopMetrics()


class ImageBlobMaterializeError(RuntimeError):
    """blob 物化失败 —— **必须**向上冒泡（HC-3）。

    `reason` 是稳定枚举，直接进指标 `image_blob_read_failure_reason_<reason>`：
      ref_malformed      ref 语法不合法（正则不匹配）
      uid_mismatch       ref 的 uid8 != 本轮消息归属用户（授权失败）
      owner_unresolved   拿不到归属（装配缺失，fail-closed 而不是放行）
      blob_missing       磁盘上没有这个 blob
      sha_mismatch       读到的字节 sha 与 ref 不符（内容被换/写坏）
      read_error         IO 异常
      too_large          超出单图上限
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or f"image blob materialize failed: {reason}")


# ---------------------------------------------------------------------------
# 路径与 ref
# ---------------------------------------------------------------------------


def blob_root() -> str:
    from app.core.config import get_config

    return str(get_config().agent_image_blob_root)


def uid8_for_user(user_id: object) -> str:
    from app.services.image_blob_context import uid8_for_user as _u

    return _u(user_id)


def parse_blob_ref(ref: Any) -> Optional[Tuple[str, str]]:
    """ref → (uid8, sha256)；不匹配白名单一律 None（**绝不**参与路径拼接）。"""
    if not isinstance(ref, str):
        return None
    match = BLOB_REF_RE.match(ref.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def make_blob_ref(uid8: str, sha256_hex: str) -> str:
    if not _UID8_RE.match(uid8 or "") or not _HEX64_RE.match(sha256_hex or ""):
        raise ValueError("ref 只能由 uid8 + sha256 派生")
    return f"{BLOB_SCHEME}{uid8}/sha256/{sha256_hex}"


def blob_path_in(root: str, uid8: str, sha256_hex: str) -> str:
    """在**指定根**下派生路径——GC 带 `--root` 时必须用它。

    存在理由（A4.9 二轮评审 A·C1）：`blob_path()` 走配置根，GC 却按 `--root`
    扫描。两个根不一致时，运维对着备份目录跑 GC，删掉的却是**生产在用**的 blob，
    而他以为刀口在副本上。扫描、加锁、删除必须共用同一个根，没有例外。
    """
    if not _UID8_RE.match(uid8 or "") or not _HEX64_RE.match(sha256_hex or ""):
        raise ValueError("blob 路径只能由 uid8 + sha256 派生")
    return os.path.join(root, uid8, sha256_hex[:2], f"{sha256_hex}.bin")


def blob_path(uid8: str, sha256_hex: str) -> str:
    """`<root>/<uid8>/<sha[:2]>/<sha>.bin` —— 全部由校验过的 hex 派生，无外部片段。"""
    return blob_path_in(blob_root(), uid8, sha256_hex)


def _ensure_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 跨进程互斥（spec §4.5 第 4 项的跨进程补全，见 tests/decisions.md D-IEB-5）
# ---------------------------------------------------------------------------


def _gc_lock_path(root: Optional[str] = None) -> str:
    return os.path.join(root if root is not None else blob_root(), GC_LOCK_NAME)


def blob_read_guard(root: Optional[str] = None):
    """物化读期间的**共享**锁上下文（GC 的独占锁拿不到就会跳过本轮）。

    只在 LRU miss 的真实磁盘读上加锁：flock 是微秒级 syscall，命中缓存时
    完全不碰它（HC-5）。进程崩溃时内核自动释放，无幽灵锁。
    `root` 显式传入时用它，否则用配置根——GC 与物化读必须锁同一个文件，
    否则互斥是假的（A4.9 二轮评审 A·C1）。
    """
    import contextlib

    @contextlib.contextmanager
    def _guard():
        base = root if root is not None else blob_root()
        _ensure_private_dir(base)
        fd = os.open(_gc_lock_path(base), os.O_RDONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    return _guard()


def gc_exclusive_lock(root: Optional[str] = None):
    """GC 删除期的**独占**锁；拿不到（有请求正在物化）返回 None，调用方跳过本轮。"""
    import contextlib

    @contextlib.contextmanager
    def _guard():
        base = root if root is not None else blob_root()
        _ensure_private_dir(base)
        fd = os.open(_gc_lock_path(base), os.O_RDONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    yield False
                    return
                raise
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    return _guard()


# ---------------------------------------------------------------------------
# 写（唯一写入点在 tool_image_results.build_image_envelope_from_file）
# ---------------------------------------------------------------------------


def write_blob_sync(user_id: object, data: bytes) -> Tuple[str, str]:
    """落一份不可变 blob，返回 (ref, sha256)。同 sha 同内容 → 幂等。

    原子发布用 tmp + `os.link`（不是裸 `O_CREAT|O_EXCL` 直写最终路径）：后者
    只保证创建不竞争，**不保证可见性原子**——并发读者可能看到半截字节，
    sha 校验失败就把一次正常请求 fail-closed 掉（把性能问题换成正确性问题）。
    见 tests/decisions.md D-IEB-4。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("blob 内容不能为空")
    if len(data) > _MAX_BLOB_BYTES:
        raise ValueError(f"blob 超出单图上限 {len(data)} > {_MAX_BLOB_BYTES}")
    sha256_hex = hashlib.sha256(bytes(data)).hexdigest()
    uid8 = uid8_for_user(user_id)
    if not uid8:
        raise ValueError("blob 命名空间需要 user_id")
    final = blob_path(uid8, sha256_hex)
    _ensure_private_dir(os.path.dirname(final))

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(final), prefix=f".{sha256_hex[:12]}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(bytes(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        try:
            os.link(tmp_path, final)      # 原子发布；已存在 → FileExistsError（同内容）
        except FileExistsError:
            pass                          # 内容寻址幂等
        try:
            _dir_fd = os.open(os.path.dirname(final), os.O_RDONLY)
            try:
                os.fsync(_dir_fd)         # 掉电后目录项也看得到
            finally:
                os.close(_dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return make_blob_ref(uid8, sha256_hex), sha256_hex


def b64_chars_for_bytes(n: int) -> int:
    """n 字节的标准 base64 长度（含 padding）= 4·⌈n/3⌉。

    为什么不真编码：写侧只需要这个**数字**（24MB 上线裁剪的判定输入），
    `len(b64encode(data))` 要在事件循环上多分配 4/3·n 字节再丢掉（HC-5）。
    """
    return 4 * ((max(0, int(n)) + 2) // 3)


# ---------------------------------------------------------------------------
# 物化 LRU（缓存不是存放处：逐出永不丢数据，与"逐出即丢内容"的方案 C 本质不同）
# ---------------------------------------------------------------------------

_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()
_cache_hits = 0
_cache_misses = 0


def _cache_limits() -> int:
    try:
        from app.core.config import get_config

        return max(0, int(get_config().agent_image_blob_cache_max_bytes))
    except Exception:
        return 32 * 1024 * 1024


def cache_stats() -> Dict[str, int]:
    with _cache_lock:
        return {
            "hits": _cache_hits,
            "misses": _cache_misses,
            "bytes": _cache_bytes,
            "entries": len(_cache),
        }


def clear_cache() -> None:
    global _cache_bytes, _cache_hits, _cache_misses
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0
        _cache_hits = 0
        _cache_misses = 0


def _cache_get(key: str) -> Optional[bytes]:
    global _cache_hits, _cache_misses
    max_bytes = _cache_limits()
    with _cache_lock:
        if max_bytes <= 0:
            return None
        value = _cache.get(key)
        if value is None:
            _cache_misses += 1
            return None
        _cache_hits += 1
        _cache.move_to_end(key)
        return value


def _cache_put(key: str, value: bytes) -> None:
    global _cache_bytes
    max_bytes = _cache_limits()
    if max_bytes <= 0 or len(value) > max_bytes:
        return
    with _cache_lock:
        # 覆盖同名条目时必须先把**旧值**的字节从账上扣掉，否则每覆盖一次
        # `_cache_bytes` 单向虚增，LRU 会以为早已装满而逐出热条目，
        # `cache_stats()["bytes"]` 也不再是真实驻留量。
        _stale = _cache.pop(key, None)
        if _stale is not None:
            _cache_bytes = max(0, _cache_bytes - len(_stale))
        _cache[key] = value
        _cache_bytes += len(value)
        while _cache and _cache_bytes > max_bytes:
            _key, _val = _cache.popitem(last=False)
            _cache_bytes = max(0, _cache_bytes - len(_val))


# ---------------------------------------------------------------------------
# 读 / 物化
# ---------------------------------------------------------------------------


def _read_blob_bytes(uid8: str, sha256_hex: str) -> bytes:
    path = blob_path(uid8, sha256_hex)
    if not os.path.isfile(path):
        raise ImageBlobMaterializeError("blob_missing", f"blob 不存在: {uid8}/{sha256_hex[:12]}…")
    try:
        with blob_read_guard():
            with open(path, "rb") as fh:
                data = fh.read()
    except ImageBlobMaterializeError:
        raise
    except OSError as exc:
        raise ImageBlobMaterializeError("read_error", f"blob 读取失败: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != sha256_hex:
        # 路径对但内容被换/写坏 —— 绝不"将就用"（HC-3）
        raise ImageBlobMaterializeError(
            "sha_mismatch", f"blob 内容校验失败: {uid8}/{sha256_hex[:12]}…"
        )
    return data


def materialize_frame_sync(img: Dict[str, Any], owner_uid8: Optional[str]) -> str:
    """blob_ref → base64 字符串（与今天 `base64.b64encode(f.read())` 逐字节相同）。

    指标走 `_metrics()`：导入本身出问题绝不能把这条 fail-closed 通道炸成
    非预期异常（那会让"为什么失败"彻底无从查起），但也不能静默——见
    `_metrics()` 的"响亮地降级一次"。
    """
    metrics = _metrics()

    ref = img.get("blob_ref")
    parsed = parse_blob_ref(ref)
    if parsed is None:
        metrics.note_read_failure("ref_malformed")
        raise ImageBlobMaterializeError("ref_malformed", f"非法 blob ref: {str(ref)[:80]!r}")
    uid8, sha256_hex = parsed

    ref_uid = str(img.get("ref_uid") or "").strip()
    if owner_uid8 is None:
        metrics.note_read_failure("owner_unresolved")
        raise ImageBlobMaterializeError(
            "owner_unresolved", "无法确定本轮消息归属用户，拒绝物化图片字节"
        )
    if uid8 != owner_uid8 or (ref_uid and ref_uid != owner_uid8):
        # 授权失败：不读、不降级（spec §4.1；伪造 ref 越权读他人图片字节）
        metrics.note_read_failure("uid_mismatch")
        raise ImageBlobMaterializeError(
            "uid_mismatch", f"blob 命名空间 {uid8} 不属于本轮用户 {owner_uid8}"
        )

    key = f"{uid8}/{sha256_hex}"
    cached = _cache_get(key)
    if cached is not None:
        metrics.note_cache_hit()
        return base64.b64encode(cached).decode("ascii")
    metrics.note_cache_miss()

    try:
        data = _read_blob_bytes(uid8, sha256_hex)
    except ImageBlobMaterializeError as exc:
        # 失败必须可观测（spec §4.7）：blob_missing / sha_mismatch / read_error
        metrics.note_read_failure(exc.reason)
        raise
    if len(data) > _MAX_BLOB_BYTES:
        metrics.note_read_failure("too_large")
        raise ImageBlobMaterializeError("too_large", f"blob 超出上限: {len(data)}")
    _cache_put(key, data)
    metrics.note_read_ok(len(data))
    note_disk_activity(uid8)
    return base64.b64encode(data).decode("ascii")


_DISK_REPORT_TTL_SECONDS = 60.0
# `None` = 从未上报过。**不能**初始化成 0.0：`time.monotonic()` 是开机以来的秒数，
# 刚重启的机器（正是 OOM 之后的 60 机的形态）上 `monotonic() < 60` 会让第一次记账
# 被 TTL 挡掉，配额告警读到的 `_QUOTA_USAGE` 是空的。
_disk_reported_at: Optional[float] = None
_quota_lock = threading.Lock()


def reset_disk_report_gate() -> None:
    """测试隔离：复位"是否已上报过磁盘量"的门。"""
    global _disk_reported_at
    with _quota_lock:
        _disk_reported_at = None


def note_disk_activity(uid8: str) -> None:
    """磁盘口径记账（配额告警的输入）——**读写两侧都要调**。

    只在读侧记账的话，HC-6 的磁盘告警恰好对"把盘写满"的那条路径完全失明：
    增长来自写，告警却只挂在读上（A4.9 评审 A·I4）。

    `disk_usage()` 是全树 walk，不能挂在每次读写上；进程内 60s 最多算一次——
    配额告警本来就是"一次/用户"的粗粒度信号，一分钟滞后完全够。
    """
    global _disk_reported_at
    with _quota_lock:
        due = _disk_reported_at is None or (
            time.monotonic() - _disk_reported_at) >= _DISK_REPORT_TTL_SECONDS
        if due:
            _disk_reported_at = time.monotonic()
    try:
        metrics = _metrics()
        if due:
            total, by_user = disk_usage()
            metrics.note_disk_usage(total, by_user)
        metrics.evaluate_user_quota(uid8)
    except Exception:
        logger.debug("blob disk accounting failed", exc_info=True)


async def materialize_frame(img: Dict[str, Any], owner_uid8: Optional[str]) -> str:
    return await asyncio.to_thread(materialize_frame_sync, img, owner_uid8)


def _user_bytes_on_disk(uid8: str) -> int:
    try:
        base = os.path.join(blob_root(), uid8)
        if not os.path.isdir(base):
            return 0
        total = 0
        for _root, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith(".bin"):
                    try:
                        total += os.path.getsize(os.path.join(_root, name))
                    except OSError:
                        pass
        return total
    except Exception:
        return 0


def disk_usage() -> Tuple[int, Dict[str, int]]:
    """(总字节, {uid8: 字节})——GC 报表与配额告警共用（spec §4.7 磁盘口径）。"""
    total = 0
    by_user: Dict[str, int] = {}
    base = blob_root()
    if not os.path.isdir(base):
        return 0, by_user
    for uid8 in sorted(os.listdir(base)):
        if not _UID8_RE.match(uid8):
            continue
        used = _user_bytes_on_disk(uid8)
        by_user[uid8] = used
        total += used
    return total, by_user


def envelope_has_blob_ref(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for img in (payload.get("images") or []):
        if isinstance(img, dict) and img.get("blob_ref"):
            return True
    return False


def frame_b64_chars(img: Any) -> int:
    """24MB 上线裁剪判定用的 base64 字符数（spec §4.3 唯一改动的估算点）。

    ref 信封读 `b64_chars`——它就是原 base64 长度，判定结果与今天逐位等价；
    legacy 信封仍读 `data_b64` 长度（不得用 len(content) 兜底，那会把 JSON
    开销与多帧算进去，让 24MB 边界上的帧选择翻转 = 老会话上线内容变化）。
    """
    if not isinstance(img, dict):
        return 0
    declared = img.get("b64_chars")
    if isinstance(declared, int) and declared > 0:
        return declared
    return len(str(img.get("data_b64") or ""))


def describe_ref_for_log(ref: Any) -> str:
    """日志里只出现 uid8 + sha 前缀，绝不打印完整 ref 之外的内容（防注入串入日志）。"""
    parsed = parse_blob_ref(ref)
    if parsed is None:
        return "<invalid-ref>"
    return f"{parsed[0]}/{parsed[1][:12]}…"

# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""图片信封 / blob 的进程内可观测性与软阈值告警（2026-11-09 · spec §4.7）。

**为什么在进程内计数**：60 OOM（`docs/POSTMORTEM_20261109_60_image_envelope_oom.md`）
之前完全没有驻留指标——`envelope_chars` 的语义（常驻，非上线）被普遍误读，
事故会话 4 分钟从 23.6M 涨到 75.0M 字符无人知晓。本模块把两个口径分开命名、
分开告警，并把「磁盘写失败 → 退回内联 → OOM 向量原样回归」这条复合故障链
（评审 I6-B）绑成一条升级告警，而不是两个各管一半的告警。

指标（snapshot 键名即 spec §4.7 的名字）::

    image_envelope_resident_chars   消息列表 + tool_results_accumulated + stream buffer 三处合计
    image_envelope_wire_chars       本轮实际上线 base64 字符量
    image_blob_cache_hits / _misses / _cache_hit_ratio
    image_blob_read_bytes / image_blob_read_failures_total
    image_blob_write_failures_total  落盘失败退回内联的计数（=OOM 向量回归计数）
    image_blob_bytes_total / image_blob_bytes_by_user  磁盘用量

告警全部是**日志级**（仓内无 metrics 基础设施，见 tests/decisions.md
D-IEB-1）：`alert_once` 按 key 去重（软阈值要求「一次/会话」），WARN 走
`logging`，运维在 `chatllm.log` / journald 里 grep `IMAGE-BLOB-ALERT` 即可。
只告警，不裁剪、不删除（HC-1/HC-4/HC-6）。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

ALERT_TAG = "IMAGE-BLOB-ALERT"

# spec §4.7 双档软阈值（默认值；[agent.image_blobs] 可覆盖）
DEFAULT_RESIDENT_SOFT_LIMIT_CHARS = 32 * 1024 * 1024
DEFAULT_WIRE_SOFT_LIMIT_CHARS = 20 * 1024 * 1024

_counters: "OrderedDict[str, float]" = OrderedDict()
_carriers: "OrderedDict[str, int]" = OrderedDict()
_alerted: "OrderedDict[str, bool]" = OrderedDict()
_lock = threading.Lock()
_MAX_ALERT_KEYS = 4096

RESIDENT_CARRIER_NAMES = ("messages", "tool_results_accumulated", "stream_buffer")
# 三载体的名字就是 `set_carrier` 的键；报表与测试都按 `RESIDENT_CARRIER_NAMES` 引用。


def _thresholds() -> tuple:
    try:
        from app.core.config import get_config

        blobs = (get_config().agent or {}).get("image_blobs", {}) or {}
        resident = int(blobs.get("resident_soft_limit_chars", DEFAULT_RESIDENT_SOFT_LIMIT_CHARS))
        wire = int(blobs.get("wire_soft_limit_chars", DEFAULT_WIRE_SOFT_LIMIT_CHARS))
        quota = int(blobs.get("user_quota_bytes", 0) or 0)
    except Exception:
        resident = DEFAULT_RESIDENT_SOFT_LIMIT_CHARS
        wire = DEFAULT_WIRE_SOFT_LIMIT_CHARS
        quota = 0
    return resident, wire, quota


# ---------------------------------------------------------------- counters


def incr(name: str, by: float = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + by


def set_gauge(name: str, value: float) -> None:
    with _lock:
        _counters[name] = value


def get(name: str) -> float:
    with _lock:
        return _counters.get(name, 0)


def snapshot() -> Dict[str, Any]:
    """当前指标快照（含派生值）。测试与日志共用同一口径。"""
    with _lock:
        out: Dict[str, Any] = dict(_counters)
        out["image_envelope_resident_by_carrier"] = dict(_carriers)
        out["image_envelope_resident_chars"] = sum(_carriers.values())
        hits = _counters.get("image_blob_cache_hits", 0)
        misses = _counters.get("image_blob_cache_misses", 0)
        total = hits + misses
        out["image_blob_cache_hit_ratio"] = round(hits / total, 4) if total else 0.0
        out["image_alerts_emitted_total"] = len(_alerted)
    return out


def reset() -> None:
    """测试隔离用：清空计数、载体、配额账与告警去重集。"""
    with _lock:
        _counters.clear()
        _carriers.clear()
        _alerted.clear()
        _QUOTA_USAGE.clear()


# ---------------------------------------------------------------- resident 载体


def set_carrier(carrier: str, chars: int) -> None:
    """记录某一驻留载体（消息列表 / 累积 tool_results / stream buffer）的信封字符数。

    `image_envelope_resident_chars` = 三处合计（评审 I7-A：常驻不止消息列表一份）。
    """
    with _lock:
        _carriers[carrier] = max(0, int(chars))


def resident_chars() -> int:
    with _lock:
        return sum(_carriers.values())


def note_wire_chars(chars: int) -> None:
    """本轮实际上线 base64 字符量（wire 口径，与常驻口径分开）。"""
    set_gauge("image_envelope_wire_chars", max(0, int(chars)))


# ---------------------------------------------------------------- blob 读/写事件


def note_cache_hit() -> None:
    incr("image_blob_cache_hits")


def note_cache_miss() -> None:
    incr("image_blob_cache_misses")


def note_read_ok(byte_count: int) -> None:
    incr("image_blob_read_bytes", byte_count)


def note_read_failure(reason: str) -> None:
    incr("image_blob_read_failures_total")
    incr(f"image_blob_read_failure_reason_{reason}")


def note_write_failure(reason: str) -> None:
    """落盘失败 → 退回内联。**这就是 OOM 向量回归计数**（spec §4.7 复合故障链）。

    非零时必须把驻留告警等级一并抬起来：磁盘高水位 → 退回内联 → 内存无上界的
    原事故路径原样复活，两个告警各管一半等于没告警。
    """
    incr("image_blob_write_failures_total")
    incr(f"image_blob_write_failure_reason_{reason}")
    logger.warning(
        "%s level=escalate key=blob-write-failures image_blob_write_failures_total=%d "
        "reason=%s —— 已退回内联，OOM 向量回归；驻留软阈值告警等级同步升级",
        ALERT_TAG, int(get("image_blob_write_failures_total")), reason,
    )


def note_disk_usage(bytes_total: int, by_user: Optional[Dict[str, int]] = None) -> None:
    set_gauge("image_blob_bytes_total", max(0, int(bytes_total)))
    if by_user is not None:
        set_gauge("image_blob_users_with_blobs", len(by_user))
        with _lock:
            _QUOTA_USAGE.clear()
            for uid8, used in by_user.items():
                _QUOTA_USAGE[str(uid8)] = max(0, int(used))


_QUOTA_USAGE: "OrderedDict[str, int]" = OrderedDict()


def quota_usage_bytes(uid8: str) -> int:
    with _lock:
        return _QUOTA_USAGE.get(uid8, 0)


# ---------------------------------------------------------------- 告警


def alert_once(key: str, message: str, *args: Any, level: int = logging.WARNING) -> bool:
    """按 key 去重的一条告警（软阈值要求「一次/会话」）。返回是否真的发出。"""
    with _lock:
        if key in _alerted:
            return False
        _alerted[key] = True
        while len(_alerted) > _MAX_ALERT_KEYS:
            _alerted.popitem(last=False)
    logger.warning("%s %s", ALERT_TAG, message % args if args else message)
    return True


def evaluate_soft_thresholds(
    *,
    session_key: str,
    wire_chars: int,
    has_blob_refs: bool,
    extra_alerts: Iterable[str] = (),
) -> None:
    """分档软阈值（spec §4.7 / 评审 B-M1）。

    - legacy 会话（内联 base64 驻留）：`resident_chars > 32M` 告警一次/会话，**只告警不裁剪**。
    - ref 会话：P1b 之后驻留只剩每帧 ~200B，32M 档永不可达 ⇒ 另设 `wire_chars > 20M` 档。
    - `image_blob_write_failures_total > 0`：磁盘退回内联的复合故障链 ⇒ 升级措辞。
    """
    resident_limit, wire_limit, _quota = _thresholds()
    resident = resident_chars()
    escalated = int(get("image_blob_write_failures_total")) > 0
    tier = "escalated(blob-write-failures)" if escalated else "normal"

    if not has_blob_refs and resident > resident_limit > 0:
        alert_once(
            f"resident|{session_key}",
            f"level=resident session={session_key} image_envelope_resident_chars={resident} "
            f"> {resident_limit}（tier={tier}）—— 只告警不裁剪（HC-1/HC-4），"
            f"载体分布={dict(_carriers)}",
        )
    if has_blob_refs and wire_chars > wire_limit > 0:
        alert_once(
            f"wire|{session_key}",
            f"level=wire session={session_key} image_envelope_wire_chars={wire_chars} "
            f"> {wire_limit}（tier={tier}）—— 上线体积超软阈值，检查帧选择与 24MB 预算",
        )
    for key in extra_alerts:
        alert_once(f"{key}|{session_key}", f"level={key} session={session_key}")


def evaluate_user_quota(uid8: str, used_bytes: Optional[int] = None) -> bool:
    """按用户 blob 配额**告警**（spec §4.10 (a)：不删不裁，只提示运维扩容）。

    `user_quota_bytes <= 0` 是**显式关闭**（出厂默认就是 0，即"不按用户限额"）。
    这不是静默失效：GC 的 `--report` 会把"配额告警：关闭"打在报表第一行，
    运维看得到它没开（A4.9 二轮评审 A·I10）。
    """
    if not uid8:
        return False
    _r, _w, quota = _thresholds()
    if quota <= 0:
        return False
    used = quota_usage_bytes(uid8) if used_bytes is None else int(used_bytes)
    if used <= quota:
        return False
    return alert_once(
        f"quota|{uid8}",
        f"level=quota uid8={uid8} image_blob_bytes_used={used} > user_quota_bytes={quota} "
        f"—— 按 HC-6 不删不裁，请扩容或按 §4.10 (b)(c) 处置",
    )

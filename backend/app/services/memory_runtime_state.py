# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""memory 子系统运行时禁用状态（§9.5 / §9.11 探测结果的权威存放处）。

背景：§9.5（pgvector 缺失）与 §9.11（embedding provider 不支持/维度不匹配）
探测此前通过 `config.memory["enabled"] = False` 禁用子系统——但 Config 实例
经 lru_cache 缓存，SIGHUP `clear_config_cache()` 后会重建新实例（TOML 中的
enabled=true 复活），而各服务模块级 `config = get_config()` 仍持旧实例
（False），新旧实例门控判定不一致（A4.9 round 4 复审 F6）。

本模块用进程级全局 flag 存放禁用状态：不随 Config 重建而丢失，
所有门控统一经 `memory_runtime_enabled(cfg)` 判定（config.enabled AND 未禁用）。
SIGHUP 重载后由 main._memory_reprobe_after_reload 重新探测：
环境已修复则解除禁用，未修复则探测会再次禁用。
"""
import logging

logger = logging.getLogger(__name__)

_DISABLED_REASON: str | None = None


def disable_memory(reason: str) -> None:
    global _DISABLED_REASON
    if _DISABLED_REASON != reason:
        logger.warning("memory 子系统运行时禁用: %s", reason)
    _DISABLED_REASON = reason


def enable_memory() -> None:
    global _DISABLED_REASON
    if _DISABLED_REASON is not None:
        logger.info("memory 子系统运行时禁用解除（此前: %s）", _DISABLED_REASON)
    _DISABLED_REASON = None


def memory_disabled_reason() -> str | None:
    return _DISABLED_REASON


def memory_runtime_enabled(cfg) -> bool:
    """config [memory].enabled AND 运行时未被探测禁用。cfg 为任一 Config 实例。"""
    if _DISABLED_REASON is not None:
        return False
    try:
        return bool(cfg.memory.get("enabled"))
    except Exception:
        return False

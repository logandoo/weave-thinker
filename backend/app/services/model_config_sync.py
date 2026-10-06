# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""模型池配置随同步 — 服务端载荷构建 + 客户端落盘应用（2026-09-29 波，D-21/D-22）。

背景：模型池（config_model.toml 的 [endpoints]/[routing]/[defaults]）不在同步域，
绑定账户的设备拿不到部署侧模型配置（用户报告：助手模型列表仅剩「mimo（旧配置）」
与 deepseek；远端实有全部模型）。本模块把「服务端合并配置的模型段」作为 resync
的 `model_config` 实体下行；客户端白名单净化后落盘 config_model.toml 并由调用方
热重载注册表 → 全端模型列表一致（D-16 原则：同账户所有数据一致，含模型配置）。

方向：服务端 → 客户端单向（部署侧是事实源；多租户服务器防客户端上行污染）。
密钥随载荷（D-16 威胁模型：自托管+TLS）；服务端可用 [sync] model_config_include_keys
关闭密钥随行（多租户/开放注册部署，见 D-22）。落盘 0600（含备份/临时文件）；
写前 round-trip 校验（拒绝会产出不可解析 TOML 的载荷）；既有损坏文件先隔离再重建。

已知边界（v1）：载荷缺段=保留本地段（legacy 服务端保护）；段删除需显式空段。
同一本地栈多绑定=后写胜（日志 + .sync-meta 留痕，不阻断）。
"""
import copy
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import toml

logger = logging.getLogger(__name__)

# 随同步的模型段白名单（config_model.toml 的端点位/路由/采样默认）。
# [secrets]（web_search/context7 密钥）与 [default_assistant] 不在本波范围。
MODEL_CONFIG_SECTIONS = ("endpoints", "routing", "defaults")
# include_keys=False 时递归剥离的密钥形字段名（D-22；深度覆盖 extra/headers 等嵌套位）
_KEY_FIELD_RE = re.compile(r"(?i)(api[_-]?key|authorization|secret)$")


def _strip_key_fields(obj: Any) -> Any:
    """递归剥离密钥形字段（键名匹配；含 endpoints.*.extra/headers 嵌套）。"""
    if isinstance(obj, dict):
        return {
            k: _strip_key_fields(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and _KEY_FIELD_RE.search(k))
        }
    if isinstance(obj, list):
        return [_strip_key_fields(v) for v in obj]
    return obj


def _prune_none(obj: Any) -> Any:
    """递归剔除 None（TOML 无 null；防恶意载荷 null 值导致序列化丢失→重复改写）。"""
    if isinstance(obj, dict):
        return {k: _prune_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_prune_none(v) for v in obj]
    return obj


def build_model_config_payload(conf: Dict[str, Any], include_keys: bool = True) -> Dict[str, Any]:
    """合并配置 dict → 下行载荷（仅白名单段；深拷贝防调用方篡改运行时配置）。

    ``include_keys=False``：递归剥离密钥形字段（含 nested extra/headers，D-22）。
    缺段不携带键=客户端保留本地对应段（legacy 服务端保护）。"""
    payload: Dict[str, Any] = {}
    for section in MODEL_CONFIG_SECTIONS:
        value = (conf or {}).get(section)
        if isinstance(value, dict):
            payload[section] = copy.deepcopy(value)
    if not include_keys:
        payload = _strip_key_fields(payload)
    return payload


def resolve_model_config_target() -> Path:
    """客户端落盘目标：既有 config_model.toml，或主配置同目录的兄弟路径。"""
    from app.core.config import get_config

    cfg = get_config()
    if getattr(cfg, "model_config_path", None):
        return Path(cfg.model_config_path)
    return Path(cfg.config_path).parent / "config_model.toml"


def _secure_write(path: Path, data: str) -> None:
    """0600 原子写：O_EXCL 临时文件（防符号链接跟随/碰撞）→ os.replace。"""
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _read_source_meta(target: Path) -> Optional[dict]:
    meta = target.with_name(target.name + ".sync-meta")
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_source_meta(target: Path, source: Optional[str]) -> None:
    if not source:
        return
    meta = target.with_name(target.name + ".sync-meta")
    try:
        _secure_write(meta, json.dumps(
            {"source": source, "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            ensure_ascii=False,
        ) + "\n")
    except OSError:
        logger.warning("failed to write sync-meta beside %s (continuing)", target)


def apply_model_config(
    payload: Dict[str, Any], target: Path, source: Optional[str] = None,
) -> bool:
    """载荷 → 本地 config_model.toml（白名单净化 + 合并保留非模型段 + 原子写 0600）。

    返回 True=文件有语义变更并已落盘（调用方负责 reload_model_registry）；
    False=无有效段 / 无变化 / 载荷会产出不可解析 TOML（警告留痕，绝不落盘）。
    ``source``：应用来源（绑定服务器 URL）——跨绑定覆盖时告警留痕（不阻断）。
    """
    if not isinstance(payload, dict):
        logger.warning("model_config payload is not an object — ignored")
        return False
    incoming: Dict[str, Any] = {}
    for section in MODEL_CONFIG_SECTIONS:
        if section not in payload:
            continue
        value = payload[section]
        if not isinstance(value, dict):
            logger.warning("model_config section [%s] is not an object — ignored", section)
            continue
        incoming[section] = _prune_none(value)
    if not incoming:
        logger.warning("model_config payload carries no valid model sections — ignored")
        return False

    target = Path(target)
    existing: Dict[str, Any] = {}
    quarantined: Optional[Path] = None
    if target.exists():
        try:
            loaded = toml.load(target)
        except Exception:  # noqa: BLE001 — 损坏文件隔离保留（可自愈，勿静默丢弃）
            quarantined = target.with_name(f"{target.name}.corrupt-{int(time.time())}")
            try:
                os.replace(target, quarantined)
                os.chmod(quarantined, 0o600)
                logger.warning("existing %s is not valid TOML — quarantined to %s",
                               target, quarantined.name)
            except OSError:
                logger.warning("existing %s is not valid TOML — refusing to overwrite", target)
                return False
        else:
            if isinstance(loaded, dict):
                existing = loaded
    merged = dict(existing)
    merged.update(incoming)
    if merged == existing:
        return False  # 语义无变化（含手工注释/格式差异）——不重写

    try:
        new_text = toml.dumps(merged)
        toml.loads(new_text)  # round-trip 守卫：绝不落盘不可解析 TOML（D-22）
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_config payload would produce invalid TOML (%s) — rejected", exc)
        if quarantined is not None and not target.exists():
            try:
                os.replace(quarantined, target)  # 恢复隔离件（本轮未改变现状）
            except OSError:
                logger.warning("failed to restore quarantined file %s", quarantined)
        return False

    prev = _read_source_meta(target)
    if prev and source and prev.get("source") and prev["source"] != source:
        logger.warning(
            "model_config overwrite across bindings: %s → %s (last writer wins)",
            prev.get("source"), source,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            _secure_write(target.with_name(target.name + ".bak-sync"), target.read_text(encoding="utf-8"))
        except OSError:
            logger.warning("failed to write backup beside %s (continuing)", target)
    _secure_write(target, new_text)
    _write_source_meta(target, source)
    aliases = sorted((merged.get("endpoints") or {}).keys())
    logger.info(
        "model_config applied → %s (endpoints=%d: %s)",
        target, len(aliases), ",".join(aliases[:12]),
    )
    return True

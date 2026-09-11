# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""ModelRegistry — 模型端点注册表（模型控制的唯一入口）。

配置来源（优先级，2026-08-31 两文件合并）：
1. `backend/config_model.toml`（gitignored）的 [endpoints.*] / [routing] 段 ——
   端点与 purpose 路由的唯一显式来源。
2. 缺省合成：显式段未覆盖的端点，从合并配置的 legacy 键（[api]/[providers]/
   [asr]/[voice]/[memory]/[deathmatch.*]）合成 —— 行为与基线完全一致的回滚网。
   （model_registry.toml 已废弃并被忽略；一次性并入：
   scripts/consolidate_model_config.py --apply）

purpose → 端点：resolve(purpose) 先查 routing 映射得到别名（可带
model_name 覆盖），未映射的 purpose 一律回落 "main"。
"""
import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.model_gateway.schemas import (
    KIND_ASR,
    KIND_EMBEDDING,
    KIND_LLM,
    KIND_RERANK,
    KIND_TTS,
    ModelEndpoint,
)

logger = logging.getLogger(__name__)

DEFAULT_LLM_ALIAS = "main"
# provider_type → capabilities 的既有 UI 语义（ChatInput 思考菜单）。
# reasoning_efforts 数组序 = 菜单显示序（legacy：DeepSeek 降序 Max/High/Low，
# Qwen3.8 xhigh/medium/low）；effort_meta 为模型层显式配置项——每档位的
# label/desc/default（默认高亮）/params（该档位选中时并入 thinking extra_body
# 的 wire 参数）。toml [endpoints.*.capabilities] 同构覆盖。
_CAPABILITIES_BY_PROVIDER = {
    "deepseek": {
        "supports_reasoning": True,
        "reasoning_efforts": ["max", "high", "low"],
        "effort_meta": {
            "max": {"label": "Max", "desc": "最大深度思考"},
            "high": {"label": "High", "desc": "标准深度思考", "default": True},
            "low": {"label": "Low", "desc": "轻量快速推理"},
        },
    },
    "qwen3.8_vllm": {
        "supports_reasoning": True,
        "reasoning_efforts": ["xhigh", "medium", "low"],
        "effort_meta": {
            "xhigh": {"label": "xhigh", "desc": "最强推理深度"},
            "medium": {"label": "medium", "desc": "均衡速度与深度"},
            "low": {"label": "low", "desc": "轻量快速推理"},
        },
    },
    # Qwen3.8-Flash-Next（modelscope 模型卡）：档位/默认与 27B 相同
    # （xhigh 默认），模板无 thinking_budget——差异只在 profile wire 层。
    "qwen3.8_next": {
        "supports_reasoning": True,
        "reasoning_efforts": ["xhigh", "medium", "low"],
        "effort_meta": {
            "xhigh": {"label": "xhigh", "desc": "最强推理深度"},
            "medium": {"label": "medium", "desc": "均衡速度与深度"},
            "low": {"label": "low", "desc": "轻量快速推理"},
        },
    },
}
# [providers.*] 键（legacy provider 段键名，非 endpoint alias——alias 已于
# 2026-09-01 去点改名为 "qwen3.8"）→ 助手侧 provider_type 的双向映射。
# 仅 legacy [providers] 合成路径与 _provider_cfg 回落使用；现役配置无此段。
_PROVIDER_KEY_TO_TYPE = {"qwen3.8_27b": "qwen3.8_vllm"}
_TYPE_TO_PROVIDER_KEY = {"qwen3.8_vllm": "qwen3.8_27b"}
# Purpose-only endpoints hidden from user-visible alias lists (public_aliases):
# they are infrastructure (vision model etc.), not chat-model choices.
_INTERNAL_PURPOSE_ALIASES = frozenset({"vlm"})
# memory 子系统 purpose → [memory] 中的模型配置键（对齐 memory_llm_factory）。
_MEMORY_PURPOSE_KEYS = {
    "memory.concept_extraction": "concept_extraction_model",
    "memory.dream": "dream_model",
    "memory.clarification": "clarification_model",
    "memory.query_expansion": "query_expansion_model",
    "memory.migration": "concept_extraction_model",
    "memory.consolidation": "dream_model",
}
# 辅助模型 purpose → [agent.auxiliary] 配置键（主端点上的 model_name 覆盖）。
_AUX_PURPOSE_KEYS = {
    "coordinator": "coordinator_model",
    "aux.compression": "compression_model",
    "aux.search_decision": "search_decision_model",
    "aux.title": "title_model",
    "aux.classifier": "classifier_model",
}


def _capabilities_for(provider_type: str) -> Dict[str, Any]:
    return copy.deepcopy(_CAPABILITIES_BY_PROVIDER.get(provider_type, {}))


def synthesize(conf: dict) -> Tuple[Dict[str, ModelEndpoint], Dict[str, Any]]:
    """从合并后的配置字典合成默认端点表与 routing 表（纯函数，便于测试）。"""
    conf = conf or {}
    api = conf.get("api", {}) or {}
    defaults = conf.get("defaults", {}) or {}
    providers = conf.get("providers", {}) or {}
    asr = conf.get("asr", {}) or {}
    voice = conf.get("voice", {}) or {}
    memory = conf.get("memory", {}) or {}
    agent = conf.get("agent", {}) or {}
    deathmatch = conf.get("deathmatch", {}) or {}

    endpoints: Dict[str, ModelEndpoint] = {}
    routing: Dict[str, Any] = {}

    api_url = str(api.get("base_url", "") or "")
    api_key = str(api.get("api_key", "") or "")
    api_model = str(api.get("model_name", "") or "")

    # ---- 主 LLM 端点（裸 LLMService 语义：应用 [defaults] 全局采样） ----
    main_params = {k: v for k, v in defaults.items() if v is not None and not isinstance(v, dict)}
    endpoints[DEFAULT_LLM_ALIAS] = ModelEndpoint(
        alias=DEFAULT_LLM_ALIAS,
        kind=KIND_LLM,
        base_url=api_url,
        api_key=api_key,
        model_name=api_model,
        display_name="默认模型",
        is_custom=False,
        params=main_params,
    )

    # ---- [providers.*] → 同名别名（legacy create_llm_service 语义 is_custom=True） ----
    for name, pdata in providers.items():
        if not isinstance(pdata, dict):
            continue
        ptype = _PROVIDER_KEY_TO_TYPE.get(str(name), str(name))
        ep = ModelEndpoint(
            alias=str(name),
            kind=KIND_LLM,
            base_url=str(pdata.get("base_url", "") or ""),
            api_key=str(pdata.get("api_key", "") or ""),
            model_name=str(pdata.get("model_name", "") or ""),
            provider_type=ptype,
            is_custom=True,
            capabilities=_capabilities_for(ptype),
        )
        endpoints[ep.alias] = ep
        for legacy_type, key in _TYPE_TO_PROVIDER_KEY.items():
            if key == name and legacy_type not in endpoints:
                endpoints[legacy_type] = ep.with_overrides(alias=legacy_type)

    # deepseek 别名缺省回落 [api]（对齐 config.provider_configs 的既有 fallback）
    if "deepseek" not in endpoints:
        ds = endpoints[DEFAULT_LLM_ALIAS]
        endpoints["deepseek"] = ModelEndpoint(
            alias="deepseek",
            kind=KIND_LLM,
            base_url=api_url,
            api_key=api_key,
            model_name=api_model,
            provider_type="deepseek",
            is_custom=True,
            capabilities=_capabilities_for("deepseek"),
        )
    else:
        ds = endpoints["deepseek"]
        fill = {}
        if not ds.base_url:
            fill["base_url"] = api_url
        if not ds.api_key:
            fill["api_key"] = api_key
        if not ds.model_name:
            fill["model_name"] = api_model
        if fill:
            endpoints["deepseek"] = ds.with_overrides(**fill)
        if endpoints["deepseek"].provider_type != "deepseek":
            endpoints["deepseek"] = endpoints["deepseek"].with_overrides(
                provider_type="deepseek", capabilities=_capabilities_for("deepseek")
            )

    # ---- embedding / rerank（[memory] 配置键，回落 [api]） ----
    endpoints[KIND_EMBEDDING] = ModelEndpoint(
        alias=KIND_EMBEDDING,
        kind=KIND_EMBEDDING,
        base_url=str(memory.get("embedding_api_base", "") or "") or api_url,
        api_key=str(memory.get("embedding_api_key", "") or "") or api_key,
        model_name=str(memory.get("embedding_model", "text-embedding-3-small") or "text-embedding-3-small"),
        is_custom=True,
        # dim 默认 0=未知（A4.9 W-D C1：绝不默认 1536——该值会经
        # _expected_embedding_dim 流入 vector 对账，legacy 部署上等于把
        # 正常 1024 表判成 mismatch）。消费端各自处理未知语义。
        # R2 Minor：畸形值（如 "abc"）不得在启动期崩溃。
        extra={"dim": (lambda _v: int(_v) if str(_v).strip().lstrip("-").isdigit() else 0)(
            memory.get("embedding_dim", 0) or 0)},
    )
    endpoints[KIND_RERANK] = ModelEndpoint(
        alias=KIND_RERANK,
        kind=KIND_RERANK,
        base_url=str(memory.get("rerank_api_base", "") or ""),
        api_key=str(memory.get("rerank_api_key", "") or ""),
        model_name=str(memory.get("rerank_model", "bge-reranker-v2-m3") or "bge-reranker-v2-m3"),
        is_custom=True,
    )

    # ---- ASR（[asr]，url/key/model + 协议细节入 extra） ----
    # 2026-08-31 端点池化（A4.9 W-2 Imp-1/2）：synthesize 直接产出 provider 化
    # 端点——provider_type 由 legacy 布尔/base_url 推导，顶层 key/model 携带
    # 「当前 provider」的值（dashscope 模式取 dashscope_* 而非通用 model 键），
    # 保证 legacy 文件回滚到本合成路径时引擎与密钥不错位。
    _asr_model = asr.get("model")
    _asr_is_mimo = bool(asr.get("is_mimo", False))
    _asr_is_dashscope = bool(asr.get("is_dashscope", False))
    if _asr_is_mimo:
        _asr_provider = "mimo"
    elif _asr_is_dashscope:
        _asr_provider = "dashscope"
    elif str(asr.get("base_url", "") or ""):
        _asr_provider = "general"
    else:
        _asr_provider = "dashscope"
    if _asr_provider == "dashscope":
        _asr_top_model = (str(asr.get("dashscope_model", "") or "")
                          or (str(_asr_model) if _asr_model
                              else "qwen3-asr-flash-realtime-2026-02-10"))
        _asr_top_key = (str(asr.get("dashscope_api_key", "") or "")
                        or str(asr.get("api_key", "") or ""))
    else:
        # 空串语义与 dashscope 分支一致（=未设置 → 消费端默认）。
        # 2026-09-01 用户纠正：早期遗留默认 qwen3-asr 废弃——MiMo=mimo-v2.5-asr，
        # 通用=paraformer-zh（按 provider 区分，与 asr_service.model 一致）。
        _provider_default_model = "mimo-v2.5-asr" if _asr_provider == "mimo" else "paraformer-zh"
        _asr_top_model = (str(_asr_model) if _asr_model else _provider_default_model)
        _asr_top_key = str(asr.get("api_key", "") or "")
    endpoints[KIND_ASR] = ModelEndpoint(
        alias=KIND_ASR,
        kind=KIND_ASR,
        base_url=str(asr.get("base_url", "") or ""),
        api_key=str(_asr_top_key or ""),
        model_name=str(_asr_top_model or ""),
        provider_type=str(_asr_provider),
        is_custom=True,
        extra={
            "is_dashscope": _asr_is_dashscope,
            "is_mimo": _asr_is_mimo,
            "dashscope_api_key": str(asr.get("dashscope_api_key", "") or ""),
            "dashscope_model": str(asr.get("dashscope_model", "") or ""),
            "websocket_url": str(asr.get("websocket_url", "") or ""),
            "dashscope_vocabulary_url": str(asr.get("dashscope_vocabulary_url", "") or ""),
            "vocabulary_prefix": str(asr.get("vocabulary_prefix", "") or ""),
        },
    )

    # ---- TTS（[voice] tts_* → mimo provider → [api] 的既有解析链） ----
    mimo_ep = endpoints.get("mimo")
    tts_url = (
        str(voice.get("tts_base_url", "") or "")
        or (mimo_ep.base_url if mimo_ep else "")
        or api_url
    )
    tts_key = (
        str(voice.get("tts_api_key", "") or "")
        or (mimo_ep.api_key if mimo_ep else "")
        or api_key
    )
    endpoints[KIND_TTS] = ModelEndpoint(
        alias=KIND_TTS,
        kind=KIND_TTS,
        base_url=tts_url,
        api_key=tts_key,
        model_name=str(voice.get("tts_model", "mimo-v2.5-tts") or "mimo-v2.5-tts"),
        provider_type="mimo",
        is_custom=True,
        extra={
            "voice": str(voice.get("tts_voice", "") or ""),
            "style_instruction": str(voice.get("tts_style_instruction", "") or ""),
        },
    )

    # ---- deathmatch judge（[deathmatch.judge] 显式配置时独立端点） ----
    judge_cfg = deathmatch.get("judge", {}) or {}
    if judge_cfg.get("base_url"):
        endpoints["deathmatch.judge"] = ModelEndpoint(
            alias="deathmatch.judge",
            kind=KIND_LLM,
            base_url=str(judge_cfg.get("base_url", "") or ""),
            api_key=str(judge_cfg.get("api_key", "") or ""),
            model_name=str(judge_cfg.get("model_name", "") or ""),
            is_custom=True,
        )

    # ==================== routing（purpose → 别名/覆盖） ====================
    routing["main"] = DEFAULT_LLM_ALIAS
    # legacy deathmatch judge 空配置时显式传 [api] url/key → is_custom=True 语义
    routing["judge"] = "deathmatch.judge" if "deathmatch.judge" in endpoints else {
        "alias": DEFAULT_LLM_ALIAS, "is_custom": True,
    }
    routing["deathmatch.judge"] = routing["judge"]
    # VLM (2026-08-31): canonical multimodal alias "vlm" — shared by the
    # agent's vision_interpret tool, the deathmatch visual critic and any
    # future vision caller. Reads the [vlm] section first, falling back to
    # the legacy [deathmatch.vlm] section (pre-canonical configs). Empty
    # values = unconfigured → callers skip/fail friendly. The "vlm" alias
    # is hidden from user-visible lists (public_aliases) like deathmatch.*
    # endpoints: it is a purpose endpoint, not a chat-model choice.
    vlm_cfg = conf.get("vlm", {}) or deathmatch.get("vlm", {}) or {}
    endpoints["vlm"] = ModelEndpoint(
        alias="vlm",
        kind=KIND_LLM,
        base_url=str(vlm_cfg.get("base_url", "") or ""),
        api_key=str(vlm_cfg.get("api_key", "") or ""),
        model_name=str(vlm_cfg.get("model_name", "") or ""),
        is_custom=True,
    )
    # Legacy alias kept for pre-canonical explicit references (docs/toml) —
    # sourced STRICTLY from the legacy [deathmatch.vlm] section (A4.9 W2-M1:
    # in a both-sections deployment the two aliases keep their own values).
    _legacy_vlm_cfg = deathmatch.get("vlm", {}) or {}
    endpoints["deathmatch.vlm"] = ModelEndpoint(
        alias="deathmatch.vlm",
        kind=KIND_LLM,
        base_url=str(_legacy_vlm_cfg.get("base_url", "") or ""),
        api_key=str(_legacy_vlm_cfg.get("api_key", "") or ""),
        model_name=str(_legacy_vlm_cfg.get("model_name", "") or ""),
        is_custom=True,
    )
    routing["vlm"] = "vlm"

    voice_provider = str(voice.get("provider", "") or "")
    voice_model = str(voice.get("model_name", "") or "")
    # legacy voice._build_llm 恒传 base_url → is_custom=True 语义；
    # provider 别名本身即 is_custom=True，仅 main 目标需要显式标注。
    if voice_provider and voice_provider in endpoints and voice_provider != DEFAULT_LLM_ALIAS:
        routing["voice"] = {"alias": voice_provider, "model_name": voice_model} if voice_model else voice_provider
    else:
        _voice_target: Dict[str, Any] = {"alias": DEFAULT_LLM_ALIAS, "is_custom": True}
        if voice_model:
            _voice_target["model_name"] = voice_model
        routing["voice"] = _voice_target

    # voice 子模型（legacy [voice] 覆盖键 → 子 purpose 路由；2026-08-31 池化）。
    # 空键不生成 = 子分类继承语音主端点（助手全链路继承语义）。
    _voice_sub_models = {
        "voice.duplex": voice.get("duplex_model"),
        "voice.intent": voice.get("intent_model"),
        "voice.interjection": voice.get("interjection_model"),
        "voice.memory_interjection": (
            voice.get("memory_interjection_model") or voice.get("interjection_model")
        ),
    }
    for _sub_key, _sub_model in _voice_sub_models.items():
        _m = str(_sub_model or "")
        if _m:
            if isinstance(routing["voice"], dict):
                routing[_sub_key] = {**routing["voice"], "model_name": _m}
            else:
                routing[_sub_key] = {"alias": routing["voice"], "model_name": _m, "is_custom": True}

    auxiliary = agent.get("auxiliary", {}) or {}
    for purpose, key in _AUX_PURPOSE_KEYS.items():
        model = str(auxiliary.get(key, "") or "")
        if not model:
            routing[purpose] = DEFAULT_LLM_ALIAS
        elif purpose == "coordinator":
            # legacy resolve_aux_model_context 显式传 url/key → is_custom=True
            routing[purpose] = {"alias": DEFAULT_LLM_ALIAS, "model_name": model, "is_custom": True}
        else:
            # legacy AuxiliaryClient 裸构造 + model 覆盖 → is_custom=False
            routing[purpose] = {"alias": DEFAULT_LLM_ALIAS, "model_name": model}

    for purpose, key in _MEMORY_PURPOSE_KEYS.items():
        value = str(memory.get(key, "") or "")
        routing[purpose] = value if value and value in endpoints else DEFAULT_LLM_ALIAS

    mm_provider = str((memory.get("multimodal", {}) or {}).get("provider", "") or "")
    routing["multimodal"] = mm_provider if mm_provider and mm_provider in endpoints else DEFAULT_LLM_ALIAS

    digest_model = str((agent.get("tool_digest", {}) or {}).get("model", "") or "")
    if digest_model and digest_model in endpoints:
        routing["tool_digest"] = digest_model
    elif digest_model:
        routing["tool_digest"] = {"alias": DEFAULT_LLM_ALIAS, "model_name": digest_model}
    else:
        routing["tool_digest"] = DEFAULT_LLM_ALIAS

    return endpoints, routing


def _endpoint_from_toml(alias: str, data: dict) -> ModelEndpoint:
    return ModelEndpoint(
        alias=str(alias),
        kind=str(data.get("kind", KIND_LLM) or KIND_LLM),
        base_url=str(data.get("base_url", "") or ""),
        api_key=str(data.get("api_key", "") or ""),
        model_name=str(data.get("model_name", "") or ""),
        provider_type=str(data.get("provider_type", "") or ""),
        display_name=str(data.get("display_name", "") or ""),
        is_custom=bool(data.get("is_custom", True)),
        params=dict(data.get("params", {}) or {}),
        capabilities=dict(data.get("capabilities", {}) or {}),
        extra=dict(data.get("extra", {}) or {}),
    )


class ModelRegistry:
    """端点注册表。from_sources 注入配置字典（测试友好）；生产用 get_model_registry()。"""

    def __init__(self, conf: dict, endpoints: Dict[str, ModelEndpoint], routing: Dict[str, Any],
                 explicit_routing_keys=None):
        self._conf = conf or {}
        self._endpoints = dict(endpoints)
        self._routing = dict(routing)
        # 显式路由键（合并配置 [routing] ∪ legacy [voice] 子模型覆盖升格）：
        # 语音子用途以此判定「专门设置例外」vs「继承助手/主语音端点」。
        self._explicit_routing_keys = set(explicit_routing_keys or ())

    @classmethod
    def from_sources(cls, conf: dict) -> "ModelRegistry":
        """端点注册表构建：synthesize（legacy 键 → 等价表，回滚网）+ 合并配置
        中的显式 [endpoints.*]/[routing] 段覆盖（2026-08-31 两文件合并：
        config_model.toml 是唯一显式来源，model_registry.toml 已废弃）。"""
        endpoints, routing = synthesize(conf)
        for alias, ep_data in ((conf.get("endpoints") or {}).items()):
            if isinstance(ep_data, dict):
                endpoints[str(alias)] = _endpoint_from_toml(str(alias), ep_data)
        for purpose, target in ((conf.get("routing") or {}).items()):
            routing[str(purpose)] = target
        reg = cls(conf, endpoints, routing)
        # 显式路由键 = [routing] 段 ∪ legacy [voice] 子模型覆盖升格（空值不发
        # = 继承语义；非空 = 用户显式设置 → 语音子分类走池成员而非继承助手）。
        explicit = set((conf.get("routing") or {}).keys())
        voice_cfg = conf.get("voice", {}) or {}
        _sub_models = {
            "voice.duplex": voice_cfg.get("duplex_model"),
            "voice.intent": voice_cfg.get("intent_model"),
            "voice.interjection": voice_cfg.get("interjection_model"),
            # legacy 链：memory_interjection_model → interjection_model
            "voice.memory_interjection": (
                voice_cfg.get("memory_interjection_model") or voice_cfg.get("interjection_model")
            ),
        }
        for _key, _m in _sub_models.items():
            if str(_m or ""):
                explicit.add(_key)
        reg._explicit_routing_keys = explicit
        reg._finalize_vlm_routing()
        return reg

    def has_explicit_routing(self, purpose: str) -> bool:
        """该 purpose 是否被显式配置（[routing] 段或 legacy 覆盖键）——
        语音子用途的「专门设置例外」判定（助手全链路继承反馈 2026-08-21）。"""
        return purpose in self._explicit_routing_keys

    def _finalize_vlm_routing(self) -> None:
        """Legacy compat (2026-08-31): a deployment that explicitly configured
        [endpoints."deathmatch.vlm"] (pre-canonical) keeps working — when the
        canonical "vlm" endpoint is empty but deathmatch.vlm is filled, the
        vlm purpose routes to the legacy endpoint."""
        canon = self._endpoints.get("vlm")
        legacy = self._endpoints.get("deathmatch.vlm")
        canon_filled = bool(canon and canon.base_url and canon.model_name)
        legacy_filled = bool(legacy and legacy.base_url and legacy.model_name)
        if legacy_filled and not canon_filled:
            self._routing["vlm"] = "deathmatch.vlm"

    # ---------------- 查询 ----------------

    def get(self, alias: str) -> ModelEndpoint:
        try:
            return self._endpoints[alias]
        except KeyError:
            raise KeyError(f"unknown model alias: {alias!r}") from None

    def has(self, alias: str) -> bool:
        return alias in self._endpoints

    def list_aliases(self, kind: Optional[str] = None) -> List[ModelEndpoint]:
        eps = self._endpoints.values()
        return [ep for ep in eps if kind is None or ep.kind == kind]

    def public_aliases(self, kind: Optional[str] = None) -> List[dict]:
        # 前端可见视图：剔除内部别名（legacy:* 行级端点、deathmatch.* 专用端点、
        # vlm 等 purpose 专用端点、provider_type 兼容映射重复项如
        # qwen3.8_vllm ↔ qwen3.8）。
        def _visible(ep: ModelEndpoint) -> bool:
            if ep.alias.startswith(("legacy:", "derived:", "deathmatch.")):
                return False
            if ep.alias in _INTERNAL_PURPOSE_ALIASES:
                return False
            if ep.alias in _TYPE_TO_PROVIDER_KEY and _TYPE_TO_PROVIDER_KEY[ep.alias] in self._endpoints:
                return False
            return True
        return [ep.public_dict() for ep in self.list_aliases(kind=kind) if _visible(ep)]

    def default_alias(self, kind: str = KIND_LLM) -> str:
        if kind == KIND_LLM:
            return "deepseek" if "deepseek" in self._endpoints else DEFAULT_LLM_ALIAS
        return kind

    def provider_names(self) -> List[str]:
        """[providers.*] 显式提供者键列表（对齐 legacy ProviderRouter.list_available
        去掉 "default" 后的结果；main/deepseek 合成别名与 legacy:* 不算）。
        Internal purpose aliases (vlm 等) are NOT chat-fallback candidates
        (A4.9 W2-M2: a configured vision endpoint must never be picked as a
        chat fallback / MoA reference)."""
        skip = {DEFAULT_LLM_ALIAS, "deepseek", *_TYPE_TO_PROVIDER_KEY.keys(), *_INTERNAL_PURPOSE_ALIASES}
        return [
            alias for alias, ep in self._endpoints.items()
            if ep.kind == KIND_LLM and alias not in skip
            and not alias.startswith(("legacy:", "deathmatch."))
        ]

    def resolve(self, purpose: str) -> ModelEndpoint:
        target = self._routing.get(purpose, DEFAULT_LLM_ALIAS)
        overrides: Dict[str, Any] = {}
        if isinstance(target, dict):
            if target.get("model_name"):
                overrides["model_name"] = str(target["model_name"])
            if "is_custom" in target:
                overrides["is_custom"] = bool(target["is_custom"])
            target = str(target.get("alias", DEFAULT_LLM_ALIAS) or DEFAULT_LLM_ALIAS)
        if not isinstance(target, str) or target not in self._endpoints:
            target = DEFAULT_LLM_ALIAS
        ep = self._endpoints[target]
        if overrides.get("model_name") and overrides["model_name"] == ep.model_name:
            overrides.pop("model_name")
        if "is_custom" in overrides and overrides["is_custom"] == ep.is_custom:
            overrides.pop("is_custom")
        return ep.with_overrides(**overrides) if overrides else ep

    # ---------------- 助手解析 ----------------

    def _provider_cfg(self, provider_type: str) -> dict:
        """对齐 config.get_provider_config（含 _PROVIDER_TYPE_ALIASES 与 deepseek 回落）。"""
        providers = self._conf.get("providers", {}) or {}
        api = self._conf.get("api", {}) or {}
        key = provider_type if provider_type in providers else _TYPE_TO_PROVIDER_KEY.get(provider_type, provider_type)
        pdata = providers.get(key)
        if pdata is None and provider_type == "deepseek":
            pdata = {}
        if pdata is None:
            return {"base_url": "", "api_key": "", "model_name": ""}
        base_url = str(pdata.get("base_url", "") or "")
        api_key = str(pdata.get("api_key", "") or "")
        model_name = str(pdata.get("model_name", "") or "")
        if key == "deepseek":
            base_url = base_url or str(api.get("base_url", "") or "")
            api_key = api_key or str(api.get("api_key", "") or "")
            model_name = model_name or str(api.get("model_name", "") or "")
        return {"base_url": base_url, "api_key": api_key, "model_name": model_name}

    def endpoint_for_assistant(self, assistant) -> ModelEndpoint:
        """助手 → 主 LLM 端点。

        优先级：model_alias（新）→ legacy provider_type/custom_* 行级字段
        （包装为 inline 端点，语义与 create_llm_service 完全一致）→ "main"。
        """
        if assistant is None:
            return self.get(DEFAULT_LLM_ALIAS)

        alias = str(getattr(assistant, "model_alias", None) or "")
        if alias:
            if alias in self._endpoints:
                return self._endpoints[alias]
            logger.warning("assistant model_alias %r not in registry — falling back to main", alias)
            return self.get(DEFAULT_LLM_ALIAS)

        api = self._conf.get("api", {}) or {}
        provider_type = getattr(assistant, "provider_type", None) or "deepseek"
        use_custom = bool(getattr(assistant, "use_custom_model", False))
        row_url = getattr(assistant, "custom_api_url", None) or ""
        row_key = getattr(assistant, "custom_api_key", None) or ""
        row_model = getattr(assistant, "custom_model_name", None) or ""

        if provider_type == "custom":
            url, key, model = row_url, row_key, row_model
        elif provider_type in ("qwen3.8_vllm", "qwen3.8_next"):
            cfg = self._provider_cfg(provider_type)
            url = row_url or cfg["base_url"]
            key = row_key or cfg["api_key"]
            model = row_model or cfg["model_name"]
        else:
            cfg = self._provider_cfg(provider_type)
            url = cfg["base_url"]
            key = row_key or cfg["api_key"]
            model = row_model or cfg["model_name"]
            if use_custom and row_url:
                url = row_url
                key = row_key or key

        if not url:
            # legacy：custom_api_url=None → 裸 LLMService（[api] + 全局默认采样），
            # 但行级 key/model 覆盖仍生效（custom_api_key/custom_model_name 照传）。
            main = self.get(DEFAULT_LLM_ALIAS)
            overrides: Dict[str, Any] = {}
            if model and model != main.model_name:
                overrides["model_name"] = model
            if key and key != main.api_key:
                overrides["api_key"] = key
            return main.with_overrides(**overrides) if overrides else main

        return ModelEndpoint(
            alias=f"legacy:{provider_type}",
            kind=KIND_LLM,
            base_url=url,
            api_key=key or str(api.get("api_key", "") or ""),
            model_name=model,
            provider_type=provider_type,
            is_custom=True,
            capabilities=_capabilities_for(provider_type),
        )

    def endpoint_for_subtask(self, assistant, main_ep: Optional[ModelEndpoint] = None) -> ModelEndpoint:
        """Subagent 任务模型端点：subtask_model_alias → legacy subtask_custom_* → 跟随主端点。"""
        main_ep = main_ep or self.endpoint_for_assistant(assistant)
        if assistant is None:
            return main_ep
        alias = str(getattr(assistant, "subtask_model_alias", None) or "")
        if alias:
            if alias in self._endpoints:
                return self._endpoints[alias]
            logger.warning("assistant subtask_model_alias %r not in registry — using main endpoint", alias)
            return main_ep
        if bool(getattr(assistant, "use_subtask_model", False)):
            url = getattr(assistant, "subtask_custom_api_url", None) or ""
            key = getattr(assistant, "subtask_custom_api_key", None) or ""
            model = getattr(assistant, "subtask_custom_model_name", None) or ""
            if url and key and model:
                return ModelEndpoint(
                    alias="legacy:subtask",
                    kind=KIND_LLM,
                    base_url=url,
                    api_key=key,
                    model_name=model,
                    provider_type=getattr(assistant, "subtask_provider_type", None) or "",
                    is_custom=True,
                )
        return main_ep


_registry: Optional["ModelRegistry"] = None


def get_model_registry() -> "ModelRegistry":
    global _registry
    if _registry is None:
        from app.core.config import get_config
        cfg = get_config()
        conf = getattr(cfg, "_config", {}) or {}
        legacy_toml = cfg.backend_root / "model_registry.toml"
        if legacy_toml.exists():
            logger.warning(
                "model_registry.toml is DEPRECATED and IGNORED (2026-08-31 "
                "两文件合并) — run scripts/consolidate_model_config.py --apply "
                "to merge it into config_model.toml, then delete the file."
            )
        _registry = ModelRegistry.from_sources(conf)
        _warn_if_no_usable_llm(_registry)
    return _registry


def _warn_if_no_usable_llm(registry: "ModelRegistry") -> None:
    """启动期守卫（A4.9 评审 Important-2）：没有任何可用 LLM 端点 = 部署残留
    或迁移缺失（如 config_model.toml 只有 legacy [api] 段且已被白名单忽略）。
    只 ERROR 不抛——保持 registry 可查询，但故障在启动日志而非首次调用时暴露。"""
    usable = [
        ep for ep in registry.list_aliases(kind=KIND_LLM)
        if ep.base_url.strip() and ep.model_name.strip()
    ]
    if not usable:
        logger.error(
            "ModelRegistry has NO usable LLM endpoint (base_url+model_name "
            "both filled). Check [endpoints.*] in config_model.toml — legacy "
            "sections ([api]/[providers]) are no longer merged." )


def reload_model_registry() -> "ModelRegistry":
    """改 config_model.toml 后免重启重载（管理用途）。端点/路由现由合并配置
    承载，必须先清配置缓存再重建注册表。"""
    global _registry
    from app.core.config import clear_config_cache
    clear_config_cache()
    _registry = None
    return get_model_registry()

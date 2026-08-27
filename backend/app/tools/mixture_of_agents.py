# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


def _check_moa_enabled() -> bool:
    return bool(config.agent_moa.get("enabled", False))


async def mixture_of_agents(args: dict, **kwargs) -> str:
    prompt = args.get("prompt", "")
    context = args.get("context", "")
    reference_providers = args.get("reference_providers")
    max_references = int(args.get("max_references", 3))

    if not prompt:
        return json.dumps({"error": "prompt is required"}, ensure_ascii=False)

    try:
        from app.services.moa_service import MoAService
        service = MoAService()
        result = await service.run_moa(
            prompt=prompt,
            context=context,
            reference_providers=reference_providers,
            max_references=max_references,
        )
        return json.dumps({
            "aggregated_response": result.aggregated_response,
            "reference_count": len(result.reference_responses),
            "models_used": result.model_used,
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("MoA tool failed")
        return json.dumps({"error": f"MoA execution failed: {str(e)}"}, ensure_ascii=False)


registry.register(
    name="mixture_of_agents",
    toolset="reasoning",
    schema={
        "type": "function",
        "function": {
            "name": "mixture_of_agents",
            "description": (
                "Mixture-of-Agents: 将问题并行发送给多个AI模型，再由聚合模型综合各模型的回答生成更完善的最终答案。"
                "适用于复杂推理、需要多角度分析、或需要交叉验证的问题。"
                "需要至少配置2个provider才能使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "需要多模型推理的问题或任务描述",
                    },
                    "context": {
                        "type": "string",
                        "description": "可选的上下文信息，会附在问题前面提供给参考模型",
                    },
                    "reference_providers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选的参考模型provider名称列表。留空则自动选择所有可用provider",
                    },
                    "max_references": {
                        "type": "integer",
                        "description": "最大参考模型数量，默认3",
                        "default": 3,
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    handler=mixture_of_agents,
    check_fn=_check_moa_enabled,
)

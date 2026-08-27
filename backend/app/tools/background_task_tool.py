# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tool: background_task — LLM calls this to submit long-running background research tasks."""
import json
import logging
import uuid
from datetime import datetime

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


async def background_task(args: dict, **kwargs) -> str:
    """Submit a long-running task to the background agent worker.
    The worker will run the AgentLoop independently and save results as a
    conversation + note when complete.
    """
    goal = args.get("goal", "")
    title = args.get("title", "")
    assistant_id = args.get("assistant_id", "")

    if not goal:
        return json.dumps({"error": "缺少 goal 参数（任务描述）"}, ensure_ascii=False)

    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    assistant = kwargs.get("assistant")

    if not user:
        return json.dumps({"error": "无法获取用户信息"}, ensure_ascii=False)

    from app.db.database import AsyncSessionLocal
    from app.db.database import AgentTask

    task_id = str(uuid.uuid4())
    iterations_max = config.agent_tool_loop_max_iterations
    conv_id = conversation.id if conversation else None

    async with AsyncSessionLocal() as task_db:
        bt = AgentTask(
            id=task_id,
            user_id=user.id,
            conversation_id=conv_id,
            assistant_id=assistant_id or None,
            title=title or None,
            goal=goal,
            context=None,
            task_type="general",
            status="pending",
            progress=0.0,
            iterations_done=0,
            iterations_max=iterations_max,
        )
        task_db.add(bt)
        await task_db.commit()

    logger.info(
        "background_task tool created task %s: goal=%.80s conv=%s",
        task_id, goal, conv_id,
    )

    return json.dumps({
        "success": True,
        "task_id": task_id,
        "goal": goal,
        "message": (
            "此任务将在后台独立执行，您可以关闭页面。"
            "完成后结果会自动保存到对话记录中。"
        ),
    }, ensure_ascii=False)


registry.register(
    name="background_task",
    toolset="system",
    schema={
        "name": "background_task",
        "description": (
            "将复杂的长线任务提交到后台执行。当用户的任务需要大量搜索、浏览、"
            "数据处理等需要较长时间完成的操作时，使用此工具将任务放入后台执行，"
            "而不是在当前对话中同步等待。后台任务独立运行，完成后自动将结果"
            "保存为新对话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "完整的任务描述。例如：'帮我收集近24小时的全球新闻，进入每个原始页面获取内容后进行归纳总结'",
                },
                "title": {
                    "type": "string",
                    "description": "可选的任务标题，用于展示。默认为任务描述的前40字。",
                },
                "assistant_id": {
                    "type": "string",
                    "description": "可选，指定使用的助手ID。",
                },
            },
            "required": ["goal"],
        },
    },
    handler=background_task,
    is_async=True,
    description="提交后台长线任务",
    emoji="🔄",
)

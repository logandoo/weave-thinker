# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""超时回执的统一文案（2026-11-01 审计 F3）。

背景：`terminal` / `execute_code` 的超时分支原先无条件断言「已产生上述部分
stdout 输出」，并把逃生口指向研究型的 `background_task`。两处都与实现不符：

1. Python 子进程接管道时默认块缓冲，被 SIGKILL 后用户态缓冲丢失 → 部分输出
   可能为空（现场 conv a4d54ec7）。沙箱现已强制 `PYTHONUNBUFFERED=1`，但旧
   脚本/被抹掉 env 的场景仍可能出现空输出，文案必须按实际有无输出分叉。
2. 真正对口的出口是**同一个工具**的 `durable=true`（持久作业，可轮询
   job_status/job_logs/job_artifacts）与 `process`（交互式长会话）；
   `background_task` 只负责「另起一个研究型子代理」，不继承会话上下文。

文案只在超时分支使用，属模型可见契约，改动需同步 `backend/tests/
test_terminal_timeout_contract.py`。
"""
from __future__ import annotations

__all__ = ["build_timeout_guidance"]


def build_timeout_guidance(
    timeout: float,
    has_output: bool,
    *,
    tool_name: str = "terminal",
    durable_hint: str = "用**同一个** terminal 工具的 durable=true 提交持久作业",
) -> str:
    """超时 guidance。``has_output`` 决定是否还能说「看上面的部分输出」。"""
    if has_output:
        lead = (
            f"命令执行超过 {timeout:g}s 上限被 SIGKILL 终止；"
            "上面是终止前已落盘的部分输出，可据此判断进度。"
        )
        empty_note = ""
    else:
        lead = f"命令执行超过 {timeout:g}s 上限被 SIGKILL 终止，且没有任何输出落盘。"
        empty_note = (
            "空输出不代表进程没跑：Python 等语言的 stdout 接管道时默认块缓冲，"
            "SIGKILL 会丢掉进程内缓冲。要看到进度，请在脚本里 print(..., flush=True) "
            "或用 python3 -u（沙箱已默认 PYTHONUNBUFFERED=1）。"
        )
    return (
        lead + empty_note +
        f"调整方案：(1) 单次调用只做「提交 + 取句柄」，把等待拆到下一次调用（每次 < {timeout:g}s）；"
        f"(2) 确实要跑很久的{'命令' if tool_name == 'terminal' else '计算'}，{durable_hint}"
        "（返回 job_id，用 job_status 轮询、job_logs(offset) 读日志、job_artifacts 取产物，"
        "后端重启不中断）；需要交互式长会话时用 process 工具；"
        "(3) 把长任务拆成多个小步骤，每批保存中间结果到文件，下一批从文件继续；"
        "(4) 检查是否存在死循环或阻塞等待。"
        "background_task 是另起一个研究型子代理（研究/检索类长线任务），不继承本会话上下文，"
        "不要用它来「把这条命令丢到后台跑」。"
        "不要用相同命令原样重试——会再次超时。"
        "（若输出尾部标注了捕获上限截断，请分页/重定向到文件后读取完整输出。）"
    )

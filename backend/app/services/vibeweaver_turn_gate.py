# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W17 vibeweaver 轮次门控 — 纯分类器（force-inject 与执法层的轮级开关）。

背景：`[agent.skills] force_inject=["vibeweaver"]` 曾无条件把 SKILL.md 注入每个
会话 system prompt，且 gate/audit 钩子常开——非代码任务（写作/文档/问答）被拖入
完整工程仪式（生产 conv dd3b2408：141 工具调用 + gate 红拦截 + 上下文爆仓 + 空
回答）。本模块给出轮级判定：

- 用户显式提及 vibeweaver（自然语言提及 / `/vibeweaver` / `[skill:vibeweaver]`）
  → 无条件激活；
- coordinator `coding_task`（LLM 判定，主判据）true/false → 直接采用；
- coordinator 不可用（None：死亡模式/显式技能调用/解析失败）→ 关键词启发式兜底，
  兜底面刻意宽松：coordinator 缺位时宁可多注入（真代码轮失执法的代价高于仪式开销，
  且仪式仅在模型真的写 tests/ 产物时才会被 gate 追认）；主判据缺位才是本模块防线。

纯函数、无 IO、无配置读取——逃生口（task_gating_enabled）在调用方 chat.py。
"""
from __future__ import annotations

import re

# 显式提及：技能名本身（大小写不敏感）即视为授权——`/vibeweaver` 与
# `[skill:vibeweaver]` 是其子串，单词边界即可覆盖三种形态。
_EXPLICIT_RE = re.compile(r"vibeweaver", re.IGNORECASE)

# 兜底启发式（仅 coordinator 不可用时）：软件工程信号词。刻意宽松——这是
# LLM 缺位时的次优面，主判据永远是 coordinator.coding_task。
_CODING_RE = re.compile(
    r"(" 
    r"代码|编程|调试|报错|异常|堆栈|漏洞|脚本|编译|部署|回滚|依赖|仓库|分支|"
    r"接口|函数|变量|正则|单测|测试用例|单元测试|集成测试|重构|性能优化|"
    r"前端|后端|全栈|数据库|建表|索引|迁移|缓存|并发|线程|"
    r"git\b|npm\b|npx\b|pip\b|pnpm\b|yarn\b|docker|dockerfile|kubernetes|k8s|"
    r"python|javascript|typescript|java\b|golang|rust\b|c\+\+|sql\b|html\b|css\b|"
    r"vue\b|react\b|node(\.js)?\b|fastapi|flask|django|spring|webpack|vite|"
    r"bug|debug|refactor|deploy|api\b|sdk\b|cli\b|ide\b|ci/cd|unit\s*test|"
    r"空指针|nullpointer|exception|traceback|segfault|内存泄漏|死锁"
    r")",
    re.IGNORECASE,
)


def explicit_trigger_hit(text: str) -> bool:
    """用户消息是否显式点名 vibeweaver（提及即授权）。"""
    if not text:
        return False
    return bool(_EXPLICIT_RE.search(text))


def coding_heuristic(text: str) -> bool:
    """关键词兜底：仅 coordinator 判定缺位时使用。非权威判据。"""
    if not text:
        return False
    return bool(_CODING_RE.search(text))


def resolve_turn_active(text: str, coord_coding_task, context_text: str = "") -> bool:
    """本轮 vibeweaver 是否激活。

    coord_coding_task: coordinator 的三态判定（True/False/None）。None 表示
    coordinator 未给出结论（死亡模式/技能调用/失败），此时回退关键词启发式。

    context_text（P1/P2，2026-10-10 审计 I-2）：近 3 条用户消息拼接——coordinator
    缺位时「继续/把它做完」类跟帖消息自身无关键词，只看末条会漏判工程型会话；
    启发式兜底吃 ctx。**explicit 提及仍只看末条**（防历史提及对无关新话题过度激活）。
    """
    if explicit_trigger_hit(text or ""):
        return True
    if isinstance(coord_coding_task, bool):
        return coord_coding_task
    return coding_heuristic(text or "") or coding_heuristic(context_text or "")


def resolve_effective_active(
    text: str, coord_coding_task, gating_enabled: bool, context_text: str = ""
) -> bool:
    """轮级判定 + 逃生口：gating_enabled=False（`[vibeweaver] task_gating_enabled`
    或 env VIBEWEAVER_GATING=off）恢复 2026-10-10 前旧行为=无条件激活。"""
    if not gating_enabled:
        return True
    return resolve_turn_active(text, coord_coding_task, context_text)


def extract_recent_user_text(messages, limit: int = 3):
    """从消息列表提取 (末条 str 型 user 文本, 近 limit 条 user 文本 join 老→新)。

    消费方：chat.py / agent_worker / agent_scheduler（三处统一口径，防漂移）。
    非 str content（多模态等）跳过；空列表 / limit<=0 → ("", "")。
    """
    if limit <= 0:
        return "", ""
    texts = []
    for m in reversed(list(messages or [])):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
            if len(texts) >= limit:
                break
    if not texts:
        return "", ""
    return texts[0], "\n".join(reversed(texts))

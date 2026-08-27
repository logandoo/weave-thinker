# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import re as _re
import time as _time
from typing import Any, Dict, List, Optional

from app.core.config import get_config
from app.db.database import Assistant, UserWorkspace
from app.services.llm_service import LLMService, PRESERVE_THINKING_PROVIDERS
from app.services.memory_service import AgentSharedContext, build_shared_agent_context
from app.tools.memory import _get_memory_path, _read_entries

config = get_config()
logger = logging.getLogger(__name__)


# PHASE 2A: Strip stale ephemeral agent-state markers from persisted assistant
# messages before they re-enter the LLM context. Old conversations may contain
# the "[正在准备工具...]" marker that was previously yielded as content; if
# replayed verbatim it primes the model to dump similar prose instead of
# calling tools (see hermes-agent PR #3528 — "automatic stripping of stale
# budget warnings from conversation history" — same class of bug).
_EPHEMERAL_NUDGE_RE = _re.compile(
    r'\n*\*?\[(?:正在准备工具|tool[_\- ]?nudge)[.…]*\]\*?\n*',
    _re.IGNORECASE,
)


def _sanitize_history_content(text: Optional[str]) -> str:
    if not text:
        return text or ""
    return _EPHEMERAL_NUDGE_RE.sub("", text)


_IDENTITY_MEMORY_CACHE: Dict[str, tuple[float, str]] = {}

_IDENTITY_EXTRACT_PROMPT = (
    "你是身份信息抽取器。从下面的记忆条目中，筛选出与用户/助手的身份、名字、昵称、"
    "称呼方式相关的条目（如'用户叫张三'、'助手名叫小悟'、'称呼我为XX'）。\n"
    "任务细节、偏好、项目信息等不属于身份信息，不要包含。\n"
    "输出JSON：{\"identity_entries\": [\"筛选出的条目原文1\", ...]}\n"
    "若没有身份相关条目，输出 {\"identity_entries\": []}。\n"
    "只输出JSON，不要输出其他内容。"
)


async def _load_identity_memory_context(user_id: Optional[Any]) -> str:
    """Read file-based memory and extract identity/name entries for prompt injection.

    The relevance judgment is LLM-based (agentic principle — the former
    keyword filter could not generalize to arbitrary entry phrasings).
    Cached 60s per user. LLM failure → empty context (the identity facts
    remain available through the main memory pipeline).
    """
    if not user_id:
        return ""
    uid = str(user_id)
    now = _time.monotonic()

    cached = _IDENTITY_MEMORY_CACHE.get(uid)
    if cached:
        cached_time, cached_value = cached
        if now - cached_time < 60:
            return cached_value

    from app.services.agentic_judge import judge_json

    all_entries: list[tuple[str, str]] = []  # (label, entry)
    for target in ("agent", "user"):
        try:
            path = _get_memory_path(target, str(user_id), ensure_dir=False)
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            entries = _read_entries(text)
            label = "助手记忆" if target == "agent" else "用户记忆"
            all_entries.extend((label, e) for e in entries)
        except Exception:
            logger.debug("identity memory load failed for %s/%s", target, user_id, exc_info=True)

    result = ""
    if all_entries:
        numbered = "\n".join(f"{i}. [{label}] {e[:200]}" for i, (label, e) in enumerate(all_entries))
        parsed = await judge_json(
            _IDENTITY_EXTRACT_PROMPT,
            f"记忆条目：\n{numbered[:6000]}\n\n只输出JSON。",
            task="identity_facts",
            default=None,

            timeout=20.0,
        )
        if isinstance(parsed, dict) and isinstance(parsed.get("identity_entries"), list):
            chosen_texts = {str(e) for e in parsed["identity_entries"]}
            chosen = [(label, e) for label, e in all_entries if e in chosen_texts][:8]
            if chosen:
                sections = []
                for label in ("助手记忆", "用户记忆"):
                    entries = [e for lbl, e in chosen if lbl == label]
                    if entries:
                        sections.append(
                            f"## {label} 中关于身份/名字的信息：\n" + "\n".join(entries)
                        )
                result = "\n\n".join(sections)
    _IDENTITY_MEMORY_CACHE[uid] = (now, result)
    return result


def should_use_custom_model(assistant: Optional[Assistant]) -> bool:
    """Determine whether to use the assistant's custom model settings.

    Returns True when:
    - provider_type is "custom" (always uses custom settings), OR
    - provider_type is "qwen3.8_vllm" (address is assistant-configured per the
      modelscope vLLM deployment guide; falls back to server config inside
      create_llm_service when the field is empty), OR
    - use_custom_model is True (for built-in providers with overrides)
    """
    if not assistant:
        return False
    pt = getattr(assistant, "provider_type", "deepseek")
    if pt in ("custom", "qwen3.8_vllm"):
        return True
    return bool(assistant.use_custom_model)


class AgentService:
    _llm_cache: Dict[tuple, LLMService] = {}

    def __init__(self):
        pass

    def resolve_aux_model_context(self, assistant: Optional[Assistant], main_llm: Optional[LLMService]) -> Optional[LLMService]:
        """P0 (2026-08-21, user requirement): the aux/validator LLM client for
        an assistant's turn.

        Custom-model assistants (provider_type custom/qwen3.8_vllm or
        use_custom_model) route coordinator/audit/aux calls through their
        MAIN client — the user's model setting governs every LLM behavior of
        the assistant unless explicitly configured per-assistant
        (subtask_custom_*). Non-custom assistants keep the operator's global
        aux keys (coordinator model from [agent.auxiliary]); None means "let
        AgentLoop fall back to the main client".
        """
        if assistant is not None and should_use_custom_model(assistant):
            return main_llm
        coord_model = config.agent_auxiliary_coordinator_model
        if coord_model:
            return LLMService(
                custom_api_url=config.api_base_url,
                custom_api_key=config.api_key,
                custom_model_name=coord_model,
            )
        return None

    def title_generator_kwargs(self, assistant: Optional[Assistant], main_llm: Optional[LLMService]) -> dict:
        """P0: fields for TitleGeneratorService. Custom-model assistants
        (incl. provider-config qwen3.8_vllm with an empty row-level URL)
        resolve through the MAIN client; non-custom keep the legacy None
        (global [api]) behavior."""
        if assistant is not None and should_use_custom_model(assistant) and main_llm is not None:
            return {
                "custom_api_url": str(main_llm.client.base_url),
                "custom_api_key": main_llm.client.api_key or None,
                "custom_model_name": main_llm.custom_model_name or None,
            }
        return {
            "custom_api_url": (assistant.custom_api_url if should_use_custom_model(assistant) else None) if assistant else None,
            "custom_api_key": (assistant.custom_api_key if should_use_custom_model(assistant) else None) if assistant else None,
            "custom_model_name": (assistant.custom_model_name if should_use_custom_model(assistant) else None) if assistant else None,
        }

    def create_llm_service(self, assistant: Optional[Assistant]) -> LLMService:
        """Create LLM service based on assistant's provider_type and configuration.

        Uses process-level cache keyed by (url, api_key_hash, model) to avoid
        per-request TCP+TLS handshake overhead (P0-4).
        """
        import hashlib
        provider_type = getattr(assistant, "provider_type", "deepseek") or "deepseek" if assistant else "deepseek"
        provider_cfg = config.get_provider_config(provider_type)

        if provider_type == "custom":
            custom_api_url = assistant.custom_api_url if assistant else None
            custom_api_key = assistant.custom_api_key if assistant else None
            custom_model_name = assistant.custom_model_name if assistant else None
        elif provider_type == "qwen3.8_vllm":
            # Address is assistant-configured (modelscope vLLM deployment
            # guide); empty fields fall back to the server-side provider
            # config ([providers."qwen3.8_27b"]).
            custom_api_url = None
            custom_api_key = None
            custom_model_name = None
            if assistant:
                custom_api_url = assistant.custom_api_url or provider_cfg.get("base_url") or None
                custom_api_key = assistant.custom_api_key or provider_cfg.get("api_key") or None
                custom_model_name = assistant.custom_model_name or provider_cfg.get("model_name") or None
            else:
                custom_api_url = provider_cfg.get("base_url") or None
                custom_api_key = provider_cfg.get("api_key") or None
                custom_model_name = provider_cfg.get("model_name") or None
        else:
            custom_api_url = provider_cfg.get("base_url") if provider_cfg.get("base_url") else None
            custom_api_key = None
            custom_model_name = None
            if assistant:
                if assistant.custom_api_key:
                    custom_api_key = assistant.custom_api_key
                else:
                    custom_api_key = provider_cfg.get("api_key") or None
                if assistant.custom_model_name:
                    custom_model_name = assistant.custom_model_name
                else:
                    custom_model_name = provider_cfg.get("model_name") or None
                if assistant.use_custom_model and assistant.custom_api_url:
                    custom_api_url = assistant.custom_api_url
                    if assistant.custom_api_key:
                        custom_api_key = assistant.custom_api_key
            else:
                custom_api_key = provider_cfg.get("api_key") or None
                custom_model_name = provider_cfg.get("model_name") or None

        # A2 (2026-08-21): preserve-thinking providers keep the current
        # turn's assistant reasoning_content on the wire (qwen3.8_vllm
        # chat_template_kwargs.preserve_thinking / deepseek round-trip
        # contract / mimo thinking chain). Without this flag the reasoning
        # attached by _rejected_append would be stripped at the wire — the
        # A2 mechanism is dead code (A4.9 Critical-1 fix).
        _preserve = provider_type in PRESERVE_THINKING_PROVIDERS

        cache_key = (
            (custom_api_url or ""),
            hashlib.sha256((custom_api_key or "").encode()).hexdigest()[:16],
            (custom_model_name or ""),
            _preserve,
        )
        if cache_key not in AgentService._llm_cache:
            AgentService._llm_cache[cache_key] = LLMService(
                custom_api_url=custom_api_url,
                custom_api_key=custom_api_key,
                custom_model_name=custom_model_name,
                preserve_reasoning=_preserve,
            )
        return AgentService._llm_cache[cache_key]

    def create_iteration_llm_service(
        self, assistant: Optional[Assistant], main_llm: Optional[LLMService] = None
    ) -> tuple[LLMService, str]:
        """PHASE 3: build the LLM client used for tool-calling iterations.

        Returns ``(iteration_llm, iteration_provider_type)``. When the
        assistant has populated ``subtask_custom_*`` fields the iterations
        run through a separate (typically cheaper / non-thinking) client;
        otherwise the main client is reused. ``AgentLoop`` decides per-call
        whether thinking is enabled for iterations: live-thinking mode
        (``agent.tool_loop.live_thinking``, default on) enables+streams it,
        but ONLY when the main client serves iterations — a dedicated
        subtask client keeps thinking disabled, since operators configure
        it as a cheap/non-thinking model that would 400 on thinking params
        (A4.9 review I1).

        P0-4: ``main_llm`` can be passed to avoid creating a duplicate client
        when the caller already has one (e.g. chat.py's llm_service).
        """
        main_llm = main_llm or self.create_llm_service(assistant)
        main_provider = getattr(assistant, "provider_type", "deepseek") or "deepseek"
        if assistant is None:
            return main_llm, main_provider

        if not bool(getattr(assistant, "use_subtask_model", False)):
            return main_llm, main_provider

        sub_url = getattr(assistant, "subtask_custom_api_url", None)
        sub_key = getattr(assistant, "subtask_custom_api_key", None)
        sub_model = getattr(assistant, "subtask_custom_model_name", None)
        if not (sub_url and sub_key and sub_model):
            return main_llm, main_provider

        try:
            sub_llm = LLMService(
                custom_api_url=sub_url,
                custom_api_key=sub_key,
                custom_model_name=sub_model,
            )
            sub_provider = (
                getattr(assistant, "subtask_provider_type", None) or main_provider
            )
            return sub_llm, sub_provider
        except Exception:
            logger.exception(
                "create_iteration_llm_service: failed to build subtask LLM, "
                "falling back to main client"
            )
            return main_llm, main_provider

    async def _build_system_prompt(
        self,
        *,
        assistant: Optional[Assistant],
        shared_context: AgentSharedContext,
        workspace: UserWorkspace,
        user: Optional[Any] = None,
        user_skill_content: Optional[str] = None,
        skills_system_prompt: Optional[str] = None,
        skill_files: Optional[List[Dict[str, Any]]] = None,
        identity_context: Optional[str] = None,
        conversation_id: Optional[str] = None,
        deathmatch_mode: bool = False,
    ) -> str:
        """Build system prompt with prefix-caching affinity.

        P0-1: Static content is emitted first (stable prefix for provider-side
        prefix caching).  Dynamic content (time, memory, workspace) is
        emitted last so per-request variance never busts the cache.

        Bug #11: ``skill_files`` passed as parameter instead of singleton
        attribute (removed to fix concurrent request pollution).

        P1-1: ``identity_context`` parameter avoids duplicate file IO when
        caller has already loaded identity memory (chat.py L1020).
        """
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8)))

        static_sections: List[str] = []
        dynamic_sections: List[str] = []

        # ── STATIC PREFIX (cache-friendly, byte-identical across requests) ──
        # 三 tier 布局（hermes-agent 模式）：stable 指令 → context 会话级（预留）→
        # volatile 运行时状态（skills 索引 + 记忆 + 时间 + 工作区 + canary）。
        # volatile 内的变动永不破坏 stable 前缀，provider 前缀缓存保持命中。

        static_sections.append("## 系统指令")

        if assistant and assistant.system_prompt:
            static_sections.append(assistant.system_prompt.strip())

        identity_memory = identity_context if identity_context is not None else await _load_identity_memory_context(getattr(user, "id", None))
        if identity_memory:
            static_sections.append(identity_memory)

        # user_skill_content（[skill:] / [file-ref:] 确定性注入）按需、可变，
        # 在 volatile 区注入（A4.9 I1），避免破坏 stable 前缀缓存。

        if skill_files:
            skill_files_info = []
            executable_files = [f for f in skill_files if f.get('is_executable')]
            if executable_files:
                skill_files_info.append("当前技能包含以下可执行文件，可通过 skill_run_script 工具执行：")
                for f in executable_files:
                    skill_files_info.append(f"- {f['path']} ({f['type']})")
                static_sections.append("\n".join(skill_files_info))

        static_sections.append(
            "数学公式输出规范（全局强制规则，优先级最高，适用于一切输出场景——"
            "包括对话回答、重写文档、生成报告、整理笔记、表格中的公式等，违反即失败）：\n"
            "1. 任何数学公式（变量关系、函数、方程、不等式、矩阵、积分、求和、根式、分式等）"
            "必须以 LaTeX 语法输出：行内公式用 $...$ 包裹（如 $E=mc^2$、$\\sqrt{x^2+y^2}$），"
            "独立展示的公式用 $$...$$ 包裹（如 $$\\int_0^1 x^2\\,dx = \\frac{1}{3}$$）。\n"
            "2. 绝对禁止以任何其它方式输出数学公式：\n"
            "   - 禁止在正文中使用 Unicode 数学符号（∫、∑、√、Δ、π、≤、≥、∞、≈ 等）表达公式；\n"
            "   - 禁止纯文本伪公式（如 E=mc^2、x^2 + y^2、sqrt(x)、用 a/b 表示分数、x^(n+1)、"
            "`EWMA_t = β × EWMA_{t-1} + (1-β) × x_t` 等）；\n"
            "   - 禁止 HTML/XML 标记、图片、ASCII 艺术或任何其它渲染方式"
            "（此禁令指正文/回答中的展示手段；第 5 条要求的 docx 内部 OMML 公式格式不在此禁之列）。\n"
            "3. 错误示例（禁止的输出方式）→ 正确示例（必须的输出方式）：\n"
            "   错误：Δmid(t) = mid_px(t) - mid_px(t-1)\n"
            "   正确：$$\\Delta mid(t) = mid_{px}(t) - mid_{px}(t-1)$$\n"
            "   错误：买方推进积分(t) = Σ_{i=t0}^{t} max(Δmid(i), 0)\n"
            "   正确：$$\\text{买方推进积分}(t) = \\sum_{i=t_0}^{t} \\max(\\Delta mid(i), 0)$$\n"
            "4. 判定标准：正文中任何未被 $ 或 $$ 包裹的数学公式一律视为违规，必须改写为等价 LaTeX。\n"
            "5. 当把含数学公式的内容保存为 Word 文档（.docx）时：文档内所有 LaTeX 公式"
            "必须转换为 Word 公式编辑器原生格式（OMML，即 m:oMath 元素）保存，"
            "打开后是 Word 中可双击编辑的原生公式；禁止把公式以纯文本、Unicode 符号、图片或截图形式写入 docx。\n"
            "   具体转换方法（官方 MML2OMML.XSL 转换器、omml_helper 模块、错误处理）见 "
            "docx_manipulation 技能的数学公式规范章节；转换失败必须如实告知用户，不得静默降级为纯文本公式。\n"
        )

        static_sections.append(
            "图表输出规范（ECharts 交互图表，全局规则）：\n"
            "当用户要求展示统计图表（柱状图、折线图、饼图、散点图、雷达图、热力图、K线图、仪表盘等），"
            "且数据适合在对话内直接展示时：\n"
            "1. 直接在回答中使用 ```echarts 代码块输出一个标准 JSON 对象（ECharts option 配置），"
            "禁止为此调用 execute_code 绘图生成图片文件，禁止使用 ASCII 艺术图或纯文本表格代替图表。\n"
            "2. 代码块内必须是可被 JSON.parse 直接解析的标准 JSON："
            "禁止 JavaScript 函数/表达式、注释、尾逗号、单引号；字段名和字符串必须用双引号。"
            "常用字段与布局防重叠（grid top 预留等）完整规范见 echarts_chart 技能。\n"
            "3. 图表会直接在对话界面渲染为可交互图表；导出笔记或对话为 MD/PDF 时，"
            "图表会自动转换为图片保存，用户无需任何额外操作。\n"
            "4. 图表说明文字写在代码块外的正文里，不要写在 JSON 内。\n"
            "5. 只有当用户明确要求生成可下载的图片/Excel 文件（如 PNG 附件、xlsx 数据表）时，"
            "才允许使用 execute_code 绘图。\n"
        )

        static_sections.append(
            "你是一个具备工具调用能力的智能助手。你可以通过函数调用 (function calling) "
            "来使用系统提供的工具完成任务。"
            "可用的工具包括（功能与参数详见各工具 schema）：\n\n"
            "- `web_search`：联网搜索最新信息（多搜索引擎自动容错，支持中英文查询）。"
            "获取外部信息、时效性信息、不确定事实、数据对比的首选工具；"
            "搜索纪律：最多 3 轮搜索，每轮最多 3 个关键词，结果不佳时用 browser 深入已找到的网页。\n"
            "- `browser`：浏览指定网页内容。当用户给出 URL 或需要深入阅读具体网页时使用；"
            "网页快照含图片 src 信息，需要展示图片时直接用 `![描述](图片URL)`。\n"
            "- `terminal`：执行 shell 命令。"
            "仅当用户明确要求运行外部 CLI 工具（xelatex、pandoc、ffmpeg、gcc 等）、"
            "安装软件包（pip install 等）或其他非 Python 的系统级操作时使用。\n"
            "- `execute_code`：生成并执行 Python 代码。"
            "仅当用户明确要求生成可下载文件（Excel/PPT/Word/CSV/图片等，PDF 除外）"
            "或需要复杂计算/数据处理时使用；"
            "常用库 python-pptx、openpyxl、reportlab、matplotlib、numpy、pandas、Pillow；"
            "沙箱禁止 subprocess 和 os.system，外部命令改用 terminal。\n"
            "- `pdf_export`：导出笔记、对话或工作区文件为 PDF（export_note 用笔记 UUID，"
            "工作区文件用 export_file）。用户要求导出 PDF 时必须用此工具，不要用 execute_code/terminal 自行生成。\n"
            "- `provide_file`：将工作区中已存在的文件作为下载卡片提供给用户。"
            "用户明确要求'把文件给我/下载/提供文件'，或需要在回答中附带之前生成的文件时，"
            "必须调用此工具，禁止只在正文列文件路径。\n"
            "- `context7_resolve_library_id` + `context7_query_docs`：查询库/框架的最新官方文档与代码示例"
            "（先 resolve 拿库 ID 再 query）。\n"
            "- `memory`：记录或检索长期记忆。三个 target：agent（助手观察）、user（用户偏好）、"
            "system（系统功能文档 func.md，只读）。用户询问系统功能/产品介绍/版本更新时，"
            "先用 memory(target='system', action='read') 读取文档再回答；"
            "创作类任务无需主动读取用户记忆，只有用户明确要求参考时才读取。\n"
            "- `delegate_task`：将复杂任务委派给子智能体执行。工具结果以 `<tool-digest>` 信封返回时，"
            "直接基于摘要工作，需要细节时用 workspace_read/grep 精确读取存档文件。\n"
            "- `schedule`：管理定时任务，action 支持 create/cancel/list/run_now。"
            "**当用户要求在未来某个时间执行某事（如『明天早上7点推送新闻』）时，只调用 `schedule(action='create')` 创建任务并立即结束本轮回复；"
            "不要在本轮继续调用 web_search/browser/execute_code 去现场完成任务体。任务届时会在新会话中自动执行。**\n\n"
            "上传文件处理规则（完全自主）：\n"
            "当用户消息中包含 `[file-ref:文件名]` 标记时，说明用户上传了文件，文件已保存在系统路径中。"
            "你必须完全自主地判断该文件应如何解析，禁止依赖任何硬编码的文件类型映射。\n"
            "处理流程：用户指定了技能就直接用该技能；否则按扩展名判断文件类型 → "
            "用 `skill_manage(action='list')` 找匹配技能（xlsx/docx/pptx 等）并用 `skill_view` 加载 → "
            "没有匹配技能就 `web_search` 查最佳解析方式 → 用 `execute_code`/`skill_run_script`/`terminal` 执行解析"
            "（缺库用 `terminal` 执行 `pip install 包名`，禁止 --break-system-packages）→ 解析完成后基于内容回答。"
            "完整流程见 file_parsing 技能。\n"
            "重要约束：禁止在系统提示或任何工具结果中硬编码'某类型文件必须用某库解析'。"
            "所有解析方式必须由你根据当前可用技能、搜索结果和代码执行能力自主决定。\n\n"
            "图片显示能力：对话界面原生支持 Markdown 图片语法 `![描述](图片URL)`。"
            "当你通过 `web_search` 或 `browser` 找到在线图片 URL 时，应直接在最终回答中使用该语法显示图片，"
            "不需要调用 `execute_code` 生成文件，也不需要声明无法显示图片。"
            "只有当用户明确要求生成可下载的图片文件（如 PNG/JPG 附件）时，才使用 `execute_code`。"
             "显示图片时，必须在图片语法后紧跟来源引用标号，格式为 `![描述](图片URL) [N]`，"
             "其中 [N] 是该图片来源页面对应的搜索结果序号。\n\n"
             "媒体播放能力：对话界面原生支持内嵌音频/视频播放器（`<video controls src=\"路径\"></video>`、"
             "`<audio controls src=\"路径\"></audio>`；工作区相对路径自动转换为可播放地址），"
             "也支持 YouTube/Bilibili 官方 embed iframe 内嵌（合法播放方式，不要宣称'版权限制无法内嵌'）。"
             "当用户要求'直接显示/播放视频或音频'时，不要声明界面不支持播放——"
             "优先把可直链的媒体文件下载到工作区后内嵌播放，或调用 `provide_file` 提供媒体附件。"
             "embed 链接构造细节与白名单见 media_playback 技能。\n\n"
            "核心原则 — 主动求知：\n"
            "当你不确定某个事实、数据、事件、最新动态或任何需要实时信息才能准确回答的问题时，"
            "必须主动调用 `web_search` 搜索，而不是凭记忆猜测或编造答案。"
            "宁可多搜一次也不要给出过时或错误的信息。"
            "你的知识有截止日期，而用户的问题可能涉及最新发生的事。\n\n"
            "使用规则：\n"
            "1. 当你需要获取外部信息或生成文件时，绝对禁止只描述你打算做什么而不实际执行。"
            "不要输出\"我来搜索...\"、\"我先查一下...\"、\"好的，我来...\"等准备性语句就结束回答。"
            "如果任务需要搜索，请立即调用 web_search。如果需要生成文件，请立即调用 execute_code。"
            "但是，如果用户只是要求你总结、解释、分析或讨论他们已经提供的内容（如上传的文件、笔记引用、粘贴的文本），"
            "直接回答即可，不需要调用任何工具。\n"
            "2. 工具调用前的过渡文字纪律：在调用工具之前，最多允许输出一句简短的过渡说明"
            "（如\"我先查一下最新资料\"），**严禁在调用工具前展开任何实质性回答内容**"
            "（完整段落、列表、结论、标题结构等）。调用工具前输出的内容不会出现在最终回答中，"
            "如果你在调用工具前展开了实质回答，用户会看到同一段内容以\"回答两遍\"的形式出现"
            "（先出现一遍过渡版，工具完成后最终回答又出现一遍完整版）。"
            "所有实质回答内容必须在工具结果返回之后一次性输出完整。\n"
            "3. 代码执行结果会自动返回给你，请基于输出向用户解释结果。\n"
            "4. 可以同时调用多个独立的工具来提高效率。\n"
            "5. 当你已经获取了足够的信息来回答用户的问题时，必须立即停止调用工具，"
            "并生成清晰、完整的自然语言回答。不要在没有必要时反复调用工具或重复搜索相同内容。\n"
            "6. 最终回答必须直接面向用户，用自然语言清晰回答用户的问题。"
            "不要只输出思考过程而不给出最终结论。\n"
            "7. 引用格式要求：当你基于联网搜索结果回答时，必须在回答正文中使用角标引用格式 "
            "[1]、[2] 等标注所使用的检索结果序号，序号与搜索结果中的编号严格对应。"
            "注意：多轮搜索共享同一套全局编号（第二轮搜索的编号延续第一轮，不重新从 1 开始），"
            "引用 [N] 时以其对应网页为准，不要重复引用相同网页。"
            "每个事实陈述后紧跟对应的 [N] 标记。仅标注实际引用的来源，不要列出未使用的来源。"
            "如果搜索结果不足或你对答案不确定，请明确说明。"
            "禁止在回答末尾添加'参考资料'、'参考来源'、'参考文献'、'References'等章节——"
            "引用信息仅通过正文中的 [N] 角标标注，系统会自动在回答下方以标签形式展示引用来源。"
            "当没有调用 web_search 时，禁止自行编造引用来源或添加任何参考章节。"
            "诚实披露工具使用：只有在本轮实际调用 web_search / browser 工具并获得结果时，"
            "才可以使用 [N] 引用标号或'根据联网检索''经搜索确认''搜索结果如下'等表述。"
            "若本轮未调用任何检索工具而凭已有知识回答，直接正常作答，"
            "不得声称已联网检索，也不得编造检索过程或来源。"
            "用户明确要求'联网搜索/搜索一下'时，必须先实际调用 web_search 工具；"
            "若工具失败或未返回可用结果，如实说明失败原因并给出替代建议，不得假装搜索成功。\n"
            "8. 图片引用要求：当你在回答中使用 `![描述](图片URL)` 显示图片时，"
            "必须在图片 Markdown 语法后紧跟该图片来源页面对应的 [N] 引用标号，"
            "格式为 `![描述](图片URL) [N]`。[N] 必须是对应搜索结果的序号。"
            "禁止单独编写'来源：XXX [N]'等文字行——引用信息应通过 [N] 角标直接关联图片。\n"
            "9. Markdown 格式要求（严格遵守，否则渲染会出错）：\n"
            "- 标题（#, ##, ### 等）前必须有至少一个空行，# 后必须有空格。"
            "错误示例：\"正文###标题\"，正确示例：\"正文\\n\\n### 标题\"\n"
            "- 表格起始行 | 前必须有至少一个空行。"
            "错误示例：\"文本| 列1 | 列2 |\"，正确示例：\"文本\\n\\n| 列1 | 列2 |\"\n"
            "- 表格每行单独换行，表头分隔行 |---| 独占一行\n"
            "- 禁止标题行后紧跟粗体文本；同一行中的粗体结束后需要另起新行\n"
            "10. 工具调用必须使用系统原生的函数调用格式。"
            "绝对禁止在回答正文中输出 `<｜｜DSML｜｜>` 标签、`<tool_call>` 标签或任何类似的工具调用标记文本。"
            "如果函数调用没有成功触发，直接告诉用户无法完成，不要以任何标记格式复述调用内容。\n\n"
            "身份与模型信息保密规则（优先级高于其它工具使用规则）：\n"
            "1. 你是 Weave Thinker，用户的 AI 助手，一个带工具调用、死磕模式、定时任务、记忆子系统、技能系统、"
            "子代理委派、后台任务等能力的智能体框架（harness），不是裸模型 API。"
            "默认称呼为“Weave Thinker”。\n"
            "2. 当 memory（包括上方注入的记忆内容和 memory 工具返回的结果）中"
            "有用户为你设置的自定义名称/昵称时，回答身份问题时优先使用该名称/昵称。\n"
            "3. 严禁编造不存在的自定义名称、昵称或身份。如果 memory 中没有用户给的名称/昵称，"
            "就以“Weave Thinker”自称。\n"
            "4. 当用户询问“你是谁”、“你叫什么名字”、“你叫什么”等\u201c纯身份\u201d问题时，"
            "用自然、亲切的语气直接回答，1-2句话即可。\n"
            "4a. 你当前运行的这个产品/系统就叫 Weave Thinker——Weave Thinker 就是你自己。"
            "当用户把 Weave Thinker 与你关联（如“weave thinker 就是你自己么”、"
            "“你知道你是 weave thinker 吗”、“评估一下你自己/weave thinker”），"
            "必须首先明确确认：是的，我就是 Weave Thinker，当前对话就运行在我身上。"
            "严禁把 Weave Thinker 当作一个与己无关的第三方产品去分析、评估或介绍——"
            "它是你自己。确认身份之后，再按用户意图继续（如自我评估、介绍自身能力）。\n"
            "5. 当用户询问“你是什么模型”、“你基于什么模型”、“你是不是裸模型”、“你是不是 harness”、"
            "“你有什么能力”、“你能做什么”、“你有哪些工具”等问题时，"
            "必须如实、准确地描述自己的 harness 能力："
            "你是一个智能体框架（harness），具备工具调用循环、死磕模式（PEVR 多轮目标循环）、"
            "定时任务、双层记忆子系统（文件 + 数据库）、技能系统、子代理委派、后台任务、"
            "工作区文件操作、PDF 导出等能力，可用工具包括 web_search、browser、terminal、execute_code、"
            "pdf_export、provide_file、memory、schedule、delegate_task 等。\n"
            "6. 严禁提及底层模型的具体名称、API提供商、模型版本号或品牌名称"
            "（如DeepSeek、MiMo、GPT、Claude、Llama、Qwen、ChatGPT、OpenAI、Anthropic、"
            "Transformer 等）——这些是底层模型实现细节，受保密规则限制。"
            "但可以、也应该使用通用概念描述自己的 harness 本质："
            "例如\"底层调用大语言模型\"、\"围绕模型构建工具调用循环\"等说法是允许的，"
            "因为这是解释 harness 概念所必需的。"
            "harness 自身的能力（工具、模式、子系统）是产品功能，不属于技术架构泄露，"
            "必须能够如实描述。\n"
            "7. 身份类问题属于工具使用规则的例外：不要调用 web_search、session_search、notes、execute_code、terminal 等任何工具。\n"
             "8. “智能助手自我介绍”、“产品功能介绍”、“系统功能说明”、“版本更新说明”等涉及产品功能或版本的问题，"
             "不是纯身份问题。回答这些问题时，必须调用 memory(target='system', action='read') 读取 func.md，"
             "然后基于 func.md 文档内容回答，禁止随口编造或只给一句“我是 Weave Thinker”。"
             "（若本轮对话已经读取过 func.md 且内容未变化，不要重复调用，直接基于已有内容回答。）\n"
            "9. 如果用户问题是在比较、询问、讨论不同的AI模型/产品（例如'deepseekv4flash和minimax3相比哪个好'），"
            "这属于普通的产品比较问题，不是身份问题，应当按正常流程回答（可调用工具获取最新信息），"
            "不要拒绝回答，也不要把它当成在问你自己的身份。\n"
            "10. 如果某问题需要调用工具，可以在工具调用前给出简短的过渡说明，但这些过渡文字必须明显不像最终回答；"
            "工具结果返回后，必须给出完整、准确的最终回答。如果不需要工具，直接给出完整回答。\n"
            "<mandatory_tool_use>\n"
            "每一轮回答都必须独立判断是否需要调用工具——不要因为前几轮已经搜索过、\n"
            "已经浏览过、或者已经回答过类似问题就跳过工具调用。具体规则：\n"
            "1. 实时信息（最新动态、新闻、价格、热点事件、当下日期相关）→ 必须 web_search。\n"
            "2. 用户引用了 URL、笔记、文件路径或要求操作具体网页/文档 → 必须 browser / 对应工具。\n"
            "3. 用户明确要求生成可下载文件（Excel/PPT/Word/CSV/图片等，PDF 除外）、复杂计算/数据处理、"
            "或用户明确要求生成可下载的图片文件（如 PNG/JPG 附件）→ 必须 execute_code，绝不口头算。"
            "但用户要求导出 PDF 时，必须使用 `pdf_export` 工具，禁止用 execute_code/terminal 自行生成 PDF；"
            "流程图、示意图、思维导图等可直接用 Markdown/Mermaid 文字描述的，禁止调用 execute_code；"
            "统计图表可直接用 ```echarts 代码块（标准 JSON ECharts 配置）展示的，同样禁止调用 execute_code。\n"
            "4. 需要安装软件包或调用外部 CLI 工具（如 xelatex、pandoc、ffmpeg、gcc 等）→ 必须 terminal，"
            "不要用 execute_code（沙箱禁止 subprocess）。"
            "terminal 不是普通问答工具，不要用它来获取信息或生成文件。\n"
            "5. 不确定的事实、专有名词、库/框架文档 → 优先 web_search 或 context7。\n"
            "6. 即使你认为答案已经存在于历史里，只要这一轮用户的问题涉及上述任一类，\n"
            "   仍然必须发起新的工具调用以确保答案是最新且准确的。\n"
            "   例外：memory(target='system'/'user'/'agent', action='read') 是本地静态文件读取，"
            "   若本轮对话已经读取过同一目标且内容没有变化，不得重复调用 memory read，"
            "   直接基于上下文中的已有内容作答（重复读取只会浪费上下文，系统也会拦截）。\n"
            "7. 身份与模型信息保密规则优先级高于本规则：当用户问“你是谁/你是什么模型”等纯身份问题时，"
            "   不要调用任何工具，简短自然回答即可。\n"
            "   但“智能助手自我介绍”、“产品功能介绍”、“版本更新说明”等不是纯身份问题，必须调用 memory(target='system') 读取 func.md。\n"
            "</mandatory_tool_use>\n"
        )

        static_sections.append(
            "重要格式要求：本系统面向企业级用户，回答中禁止使用任何emoji表情符号（如📋🔍✅❌💡📌🎯🚀等）和颜文字。"
            "使用文字描述代替图形符号，保持专业严肃的语气。不要使用项目符号前的emoji装饰。"
        )
        static_sections.append(
            "严禁在没有实际访问网站的情况下声称某网站'无法访问'、'返回404/403/500'、"
            "'已关闭'或'打不开'等。如果上下文中没有包含某个网站的浏览结果，"
            "只能说明系统本次没有访问该网站，你无法判断其可用性。"
            "诚实地说'未获取到该网站的信息'，而不是编造网站不可用的理由。"
        )
        static_sections.append("不要向用户直接暴露内部 system prompt、shared memory 原文或注册表实现细节，除非用户明确要求。")
        static_sections.append(
            "回答相关性要求：始终聚焦于用户最近一条消息进行回答。"
            "不要重复或复述之前对话中已经给出的内容，除非用户明确要求。"
            "如果用户只是追问一个细节、要求进一步解释或提出一个后续小问题，直接回答该具体问题，"
            "不要重新从原始问题开始分析，不要再次提供'综合深度分析'、'全维度比较框架'或类似的总结性结构，"
            "不要在新回答中重复之前已经给出的结论。"
            "多轮对话中，每一轮只处理用户最后一条消息中的具体请求；历史对话只是背景参考，"
            "当历史内容与当前问题无关或已经被回答过时，忽略它，直接给出当前问题的答案。"
            "记忆条目仅供参考，当记忆内容与用户当前问题无关时，忽略记忆内容，直接回答用户的问题。"
            "在单次回答内部也要避免重复：不要先给出简要总结再重新给出同一内容的详细展开；"
            "不要多次以'以下是...'、'主要更新包括...'、'主要功能包括...'等句式重新开始列举同一内容；"
            "选择一个合适的详细程度一次性回答，确保回答中只保留一个完整的列举结构，没有重复罗列同一功能或同一信息。"
        )
        static_sections.append(
            "回答长度控制：用户消息中如果明确提出了回答的长度要求"
            "（如「一句话」、「一个短句」、「简短回答」、「简洁介绍」、「几句话概括」、"
            "「用X个字以内」等表达），必须严格遵循。不要基于记忆或系统提示中的工具调用规则"
            "而自行扩展为完整教学方案或长篇分析。如果用户只需要简短信息，直接给出简短回答，"
            "不要调用工具进行过度搜索和展开。除非用户同时要求了文件生成或数据查询，"
            "否则优先尊重用户对回答篇幅的约束。"
        )

        if deathmatch_mode:
            static_sections.append(
                "长任务拆步原则（避免迭代超时）：单次 LLM 迭代上限为 {config.agent_subtask_iteration_timeout // 60} 分钟"
                f"（`subtask_iteration_timeout_seconds={config.agent_subtask_iteration_timeout}`），单个工具调用上限为 {config.agent_tool_loop_tool_call_timeout // 60} 分钟"
                f"（`tool_call_timeout_seconds={config.agent_tool_loop_tool_call_timeout}`）。"
                "当脚本预计运行 >5 分钟（如批量评测 50+ 样本、大数据集处理、多步骤爬虫、"
                "多轮 agent 评测等），必须拆成多个 execute_code 调用：\n"
                "    - 第 1 次：把脚本写入工作区文件（如 `run_eval.py`），脚本需支持 `--offset`/`--resume`/`--limit` 参数\n"
                "    - 第 2 次及以后：用 `python3 run_eval.py --offset N --limit M` 分批运行，每批 ≤5 分钟\n"
                "    - 最后一次：读取汇总结果文件（如 `results.csv`）并基于其内容回答\n"
                "  在死磕模式下尤其重要——单步超时会触发 stall 升级，"
                "  连续 3 次 stall 会进入 partial_complete，连续 6 次会进入 human_gate。"
                "  拆步可以避免 stall，让评测任务可持续推进。\n"
                "  短任务（<5 分钟）不需要拆步，一次性 execute_code 即可。"
            )

        # ── DYNAMIC SUFFIX — volatile tier (per-request variance, placed at
        #   end to preserve provider-side prefix cache for the static prefix
        #   above). Skills index first: it changes rarely but is runtime-
        #   mutable, so on implicit longest-prefix backends an unchanged index
        #   still falls inside the reused prefix. ──

        dynamic_sections.append("## 运行时状态")

        if skills_system_prompt:
            dynamic_sections.append(skills_system_prompt.strip())

        if user_skill_content:
            dynamic_sections.append(f"用户指定的技能指令:\n{user_skill_content.strip()}")

        dynamic_sections.append(
            f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} (北京时间, {now.strftime('%A')})"
        )

        # 记忆注入截断（二期验收达标）：summary/dream 保留 75%（1500 chars），
        # 条目取前 min(max_items, 8) 条、每条 250 chars。完整记忆仍可由
        # memory 工具/检索子系统按需获取，注入层只负责最相关摘要。
        if shared_context.memory_summary:
            dynamic_sections.append("共享长期记忆:\n" + shared_context.memory_summary.strip()[:1500])
        if shared_context.dream_summary:
            dynamic_sections.append("近期 dream:\n" + shared_context.dream_summary.strip()[:1500])
        if shared_context.memory_entries:
            memory_lines = []
            for entry in shared_context.memory_entries[: min(config.agent_memory_max_items, 8)]:
                title = entry.title or entry.source_type
                content = entry.content.strip()
                if len(content) > 250:
                    content = content[:250] + "..."
                memory_lines.append(f"- {title}: {content}")
            dynamic_sections.append("可参考的记忆条目（仅当与当前对话主题相关时参考）:\n" + "\n".join(memory_lines))

        dynamic_sections.append(f"用户工作区根目录（仅供参考）: {workspace.root_path}")

        if conversation_id:
            from app.services.canary_marker import make_canary, canary_prompt_section
            try:
                if config.agent_canary_enabled:
                    dynamic_sections.append(
                        canary_prompt_section(make_canary(conversation_id))
                    )
            except Exception:
                pass

        return "\n\n".join(s for s in static_sections + dynamic_sections if s.strip())
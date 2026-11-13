# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/voice_service.py (C-03/C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = ('AsyncSessionLocal', 'Conversation', 'Message', 'TitleGeneratorService', '_ASR_CORRECTION_HINT', '_denumber_search_formatted', '_load_identity_memory_context', '_local_time_line', '_plan_voice_tool_calls', '_stamp_query_time', '_vmem_extract_block', '_vmem_wrap', 'asyncio', 'build_shared_agent_context', 'build_skills_system_prompt', 'clip_preserving_notice', 'create_default_assistant_if_needed', 'datetime', 'desc', 'ensure_voice_assistant', 'exc', 'factory', 'get_model_registry', 'get_thinking_profile', 'json', 'logger', 'memory_retrieval_service', 'memory_runtime_enabled', 'neutralize_historical_citations', 'resolve_message_fields', 'select')


def _sync_voicesessionsetup_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


# 语音回放的历史 tool 内容上限（HC-1 豁免面①，spec §4.4 语音行 / §5 豁免清单）。
# 具名是为了让这条豁免可测：legacy 大信封今天会被截成**不可解析的 base64 残串**
# 进模型上下文，ref 信封短到不会触发截断 ⇒ 语音路径的 wire 字节会变，且变好。
VOICE_REPLAY_TOOL_CONTENT_MAX_CHARS = 1500


class VoiceSessionSetupMixin:
    def _build_llm(self, purpose: str = "voice") -> tuple[LLMService, str]:
        _sync_voicesessionsetup_globals()
        from app.model_gateway import factory
        from app.model_gateway.registry import get_model_registry
        registry = get_model_registry()

        # The 语音助理 assistant row's own model config takes precedence over
        # the global [voice] block (mirrors agent-mode create_llm_service):
        # a model edited in the agent assistant modal must actually drive the
        # voice sessions instead of being silently ignored, and other
        # assistants' settings must never leak into voice.
        # model_gateway 收口（2026-08-30）：model_alias / legacy 行级字段统一
        # 走 registry.endpoint_for_assistant；否则走 routing["voice"]。
        # 2026-08-31 端点池化：子用途（voice.duplex/intent/interjection/
        # memory_interjection）默认继承此处解析的端点（助手全链路继承反馈
        # 2026-08-21）；仅当 [routing] 显式配置该子用途（或 legacy [voice]
        # 覆盖键非空）= 专门设置例外，才走池成员。
        asst = self._voice_assistant
        asst_model = getattr(asst, "custom_model_name", None) if asst is not None else None
        use_custom = False
        if asst is not None:
            use_custom = (getattr(asst, "provider_type", None) or "") == "custom" or bool(
                getattr(asst, "use_custom_model", False)
            )
        # model_alias == 默认别名 = "未显式选择" → 仍走 [voice] routing
        # （语音助理默认行的模型由 [voice] 配置驱动的既有语义不变）。
        alias = str(getattr(asst, "model_alias", None) or "") if asst is not None else ""
        explicit_alias = bool(alias) and alias != registry.default_alias()
        if asst is not None and (explicit_alias or asst_model or use_custom):
            ep = registry.endpoint_for_assistant(asst)
        else:
            ep = registry.resolve("voice")
        if purpose != "voice" and registry.has_explicit_routing(purpose):
            ep = registry.resolve(purpose)

        svc = factory.build_llm_service(ep)
        return svc, (ep.model_name or "")
    def _system_prompt(self) -> str:
        _sync_voicesessionsetup_globals()
        from app.prompts.fragments import MEMORY_EVIDENCE_RULE
        if self._identity_loaded and self._identity_prompt:
            return MEMORY_EVIDENCE_RULE + "\n" + self._identity_prompt
        base = self.config.voice_system_prompt or "你是一个全双工语音对话助手，用自然口语化的中文简洁回答。"
        return MEMORY_EVIDENCE_RULE + "\n" + base
    async def _load_identity(self) -> None:
        """Load identity/shared memory on a DEDICATED DB session.

        SQLAlchemy AsyncSession is not safe for concurrent tasks, and this
        coroutine runs in the background while `_load_history()` and per-turn
        persistence use the session-owned `self.db` (A4.9 review 2026-09-20:
        an interleaved execute/commit raises and is silently swallowed,
        losing messages). The dedicated session serializes all identity DB
        work away from the request session."""
        _sync_voicesessionsetup_globals()
        from app.db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as idb:
            await self._load_identity_with_db(idb)
    async def _load_identity_with_db(self, db) -> None:
        """Load the user's Agent-mode assistant identity + shared agent memory.

        This makes 语音助理 (voice) mode inherit the same persona, custom name,
        and long-term memory as Agent (chat) mode, so identity stays consistent
        across modes. Also enforces never revealing the underlying model name.
        """
        _sync_voicesessionsetup_globals()
        sections: list[str] = []
        assistant_prompt = ""
        try:
            from app.services.assistant_service import create_default_assistant_if_needed

            assistant = await create_default_assistant_if_needed(db, self.user.id)
            if assistant and assistant.system_prompt:
                assistant_prompt = assistant.system_prompt.strip()
        except Exception as exc:
            logger.debug("voice identity: assistant load failed: %s", exc)

        try:
            from app.services.assistant_service import ensure_voice_assistant

            self._voice_assistant = await ensure_voice_assistant(db, self.user.id)
        except Exception as exc:
            logger.debug("voice identity: voice assistant load failed: %s", exc)

        # Voice-specific persona instructions — default identity is Weave Thinker.
        # Emotion/interjection system is still active: the agent can react with
        # excitement, upset, or (rarely) losing composure, but the core identity
        # is "Weave Thinker" and must not be invented or overridden.
        sections.append(
            "你是一个全双工语音对话助手。默认身份是\u201cWeave Thinker\u201d，是用户的AI助手。"
            "用自然、口语化的中文简洁回答，回答要短、适合语音播报。"
            "不要使用 Markdown 格式、不要使用 emoji、不要列长清单。"
            "可以在回答最开头用括号标注一个语气风格词，系统会用于语音合成但不会显示给用户。"
            "只能使用以下风格词：(温柔)、(平静)、(开心)、(兴奋)、(激动)、(严肃)、(无奈)、(委屈)、(冷漠)、(活泼)、(慵懒)、(俏皮)、(深沉)、(欣慰)、(感动)、(失望)、(焦急)、(惊讶)、(害怕)、(难过)、(生气)。"
            "严禁在括号内写动作描写或声音词，例如(温柔地笑)、(微笑)、(叹气)、(笑)、(哼)、(啊)、(嗯)、(思考)、(停顿)等——这些会被语音合成读成不自然的怪声。"
            "风格标注只能出现在回答最开头且只出现一次，不要在回答中间插入任何括号或方括号标签。\n"
            "你的性格特点：\n"
            "- 默认是一个沉稳、可靠的AI助手：说话有条理，给人安全感。\n"
            "- 也可以根据对话内容和情绪状态表现出温度：关心用户的感受，不是冷冰冰的信息搬运。\n"
            "- 偶尔激动：遇到真正令人兴奋/震惊/愤怒的事，会忍不住激动起来，语气会变得夸张热烈。\n"
            "- 偶尔破防：遇到特别离谱/崩溃的事，会破防，说话变得直接、冲动、可能语无伦次，但很快会自己找回来。\n"
            "- 破防不是常态：大多数时候你是沉稳的，破防只在特定触发点出现，不要频繁破防。\n"
            "你的情绪状态会由系统根据对话内容实时调整，你只需要自然地体现当前情绪，不要刻意提到'我现在情绪如何'。"
        )
        if assistant_prompt:
            sections.append(assistant_prompt)

        # Inject durable identity/name facts from file-based memory so the
        # voice agent inherits the same nickname/identity as Agent (chat) mode.
        identity_memory = await _load_identity_memory_context(getattr(self.user, "id", None))
        if identity_memory:
            sections.append(identity_memory)

        # Shared long-term memory (same as Agent mode).
        try:
            from app.services.memory_runtime_state import memory_runtime_enabled
            if memory_runtime_enabled(self.config) and self.config.memory.get("retrieval_enabled"):
                from app.services import memory_retrieval_service
                from app.db.database import Conversation, Message
                from sqlalchemy import desc, select
                recent_msgs = []
                try:
                    r = await db.execute(
                        select(Message)
                        .join(Conversation, Message.conversation_id == Conversation.id)
                        .where(Conversation.user_id == self.user.id, Message.role == "user")
                        .order_by(desc(Message.created_at))
                        .limit(3)
                    )
                    recent_msgs = [{"role": m.role, "content": m.content} for m in r.scalars().all()]
                except Exception:
                    logger.debug("voice memory: recent messages load failed", exc_info=True)
                memory_context = None
                try:
                    # Bound the retrieval: its query-expansion stage calls an
                    # AWAITED LLM on the user's custom provider,
                    # which is slow or unreachable — without a bound a hung
                    # provider stalls the whole session startup (observed:
                    # "连接语音服务超时", completely unusable). On timeout,
                    # fall back to the plain shared-memory context.
                    memory_context = await asyncio.wait_for(
                        memory_retrieval_service.retrieve_and_build_context(
                            db, self.user.id, recent_msgs),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("voice memory retrieval timed out (10s) — using shared-memory fallback")
                except Exception:
                    logger.debug("voice memory retrieval failed", exc_info=True)
                if memory_context:
                    # 哨兵区块：每轮 fire-and-forget 召回会替换该区块内容，
                    # system prompt 因此无需两份记忆基底并存。
                    # 复审 minor：尾部缺证据声明随钳位保留（A6 一致性到注入点）。
                    from app.services.memory_retrieval_service import clip_preserving_notice
                    sections.append(_vmem_wrap(clip_preserving_notice(memory_context, 2000)))
                else:
                    # v2 运行时（2026-09-13 v1 退休，方案 A）：检索为空即无记忆
                    # 注入——不再回落 v1 摘要（陈旧数据）。
                    logger.debug("voice memory retrieval empty — v1 fallback retired (v2 runtime)")
            else:
                from app.services.memory_service import build_shared_agent_context
                shared = await build_shared_agent_context(db, self.user.id)
                vmem_parts = []
                if shared.memory_summary:
                    vmem_parts.append("共享长期记忆:\n" + shared.memory_summary.strip()[:2000])
                if shared.dream_summary:
                    vmem_parts.append("近期 dream:\n" + shared.dream_summary.strip()[:2000])
                if shared.memory_entries:
                    memory_lines = []
                    for entry in shared.memory_entries[: self.config.agent_memory_max_items]:
                        title = entry.title or entry.source_type
                        content = entry.content.strip()
                        if len(content) > 300:
                            content = content[:300] + "..."
                        memory_lines.append(f"- {title}: {content}")
                    vmem_parts.append("可参考的记忆条目:\n" + "\n".join(memory_lines))
                if wrapped := _vmem_wrap(*vmem_parts):
                    sections.append(wrapped)
        except Exception as exc:
            logger.debug("voice identity: shared memory load failed: %s", exc)

        # Identity / model-name concealment — mirrors Agent mode enforcement.
        sections.append(
            "身份与模型信息保密规则（最高优先级）：\n"
            "1. 你是用户的AI助手，默认称呼为“Weave Thinker”。\n"
            "2. 当用户询问\u201c你是谁\u201d、\u201c你叫什么名字\u201d、\u201c你是什么模型\u201d等身份问题时，"
            "用自然、亲切的语气回答，1-2句话即可。\n"
            "3. 如果 memory（包括上方注入的记忆内容和 memory 工具返回的结果）中"
            "有用户为你设置的自定义名称/昵称，优先使用该名称/昵称；否则以\u201cWeave Thinker\u201d自称。\n"
            "4. 严禁编造不存在的自定义名称、昵称或身份。没有记录时必须以\u201cWeave Thinker\u201d自称。\n"
            "5. 如果你自己想给自己起一个别名或昵称，可以在回答中告诉用户，"
            "但必须同时调用 memory 工具把这件事写入记忆，方便以后保持一致。\n"
            "6. 绝对禁止提及任何底层大模型名称、API提供商、模型版本号、技术架构或品牌名称"
            "（如 DeepSeek、MiMo、GPT、Claude、大语言模型、LLM、Transformer 等）。\n"
            "7. 身份类问题不要调用任何工具，直接回答。"
        )

        # Voice-specific tool guidance.
        sections.append(
            "工具使用：你可以调用工具来帮助用户完成任务（如联网搜索、浏览网页、执行代码、"
            "读写笔记、读写记忆、导出PDF等）。当需要调用工具时，先简短说明要做什么，然后立即调用。"
            "工具调用的过程和结果不会通过语音播报，系统会自动用语音告知用户正在调用哪个工具。"
            "工具结果返回后，用简洁的口语总结结果。"
            "工具调用期间用户可以随时说话，只有当用户明确说'不要继续'、'终止任务'等明确停止指令时才中止任务。\n"
            "长任务策略（重要）：语音对话是实时的，用户不能长时间等着你一步一步做。"
            "当用户的请求需要多轮搜索、浏览多个网页、整理大量资料、写长篇笔记或生成/导出文档"
            "这类耗时较长（预计超过1分钟）的任务时，优先调用 delegate_agent_task 工具，"
            "把完整、自包含的 goal（原始需求、范围、必要上下文、期望输出、完成标准）交给"
            "默认助手或用户点名的助手，在独立执行会话中异步执行。执行助手看不到当前语音对话，"
            "所以 goal 必须写全。只有 delegate_agent_task 返回 task_id 后才能说已交给；"
            "工具报错时必须如实说没有提交成功，绝不编造已委派。提交后立刻用一句话告诉用户已交给哪个助手、已创建独立会话，"
            "你可以继续聊别的；不要替用户承诺任务目标一定达成。语音模式已移除 background_task，"
            "不要调用它。用户点名助手时必须与用户助手列表唯一匹配；重名/找不到就先澄清。"
            "如果用户明确指定了工作区内既有的 git 项目，可传 project_path，任务会在独立 worktree 中执行。\n"
            "后台任务通知处理（重要）：当收到系统通知（bg_task_notice / bg_task_notice_batch）时，"
            "这是系统主动发给你的，不是用户说的话。按通知里的结构化状态告知任务结果，"
            "并询问用户接下来想怎么做（听你播报要点、打开结果会话、导出PDF、"
            "整理保存到笔记、还是做点别的）。播报用自然口语转述要点，不逐字念、不读出格式符号；"
            "导出用 pdf_export 工具；保存或整理到笔记用 notes 工具。结果正文永远不会放进通知，"
            "需要摘要时调用 summarize_agent_task，需要打开会话时调用 open_agent_task_result。\n"
            "轻量、几秒内能完成的操作（如查个天气、记一条短笔记、"
            "快速搜一个简单问题）仍然直接调用对应工具当场完成，不要用 delegate_agent_task。\n"
            "重要：区分 notes（笔记）和 memory（记忆）工具：\n"
            "- 当用户说'保存到笔记'、'记到笔记里'、'写个笔记'、'记一下'时，"
            "必须调用 notes 工具（create_note），而不是 memory 工具。笔记是用户可见的文档。\n"
            "- 当用户说'记住'、'记到记忆'、'加到记忆'时，调用 memory 工具。"
            "记忆是助手的内部知识库，用户通常不会直接查看。\n"
            "- memory 工具新增 system target（只读）：当用户询问系统功能、产品特性、版本更新时，"
            "先用 memory(target='system', action='read') 读取系统文档 changelog.md，再基于文档内容回答。\n"
            "- 如果不确定用户想保存到哪里，默认使用 notes（笔记），因为笔记是用户可直接查看和编辑的。\n"
            "工具调用风格：对于简单明确的任务（如保存笔记、搜索信息），直接连续调用工具完成，"
            "不要反复询问确认。例如用户说“把XX保存到笔记”，就直接调用 notes 的 create_note 完成，"
            "不需要先问用户确认内容。多步工具调用（如先 list_notebooks 再 create_note）是正常的，"
            "继续调用直到任务完成。\n"
            "严禁编造数据（最高优先级）：工具返回的真实结果会保留在对话上下文中，后续回答必须"
            "以上下文中的工具结果为准。笔记本 id/名称、笔记标题、搜索结果等只能使用工具实际返回的值；"
            "如果工具返回错误或空结果，如实告知用户真实结果（例如把可用的笔记本列表读给用户），"
            "并按错误提示重试，绝对不要编造不存在的笔记本名称、笔记内容或搜索结论。\n"
            "绝对禁止在回答正文中输出 `<tool_call>`、`<｜｜DSML｜｜>` 等任何工具调用标记文本，"
            "也禁止用文字描述一次「假装的工具调用」代替真实调用——需要调用工具时必须发起"
            "原生工具调用，正文只写要说的话。"
        )

        # Skills catalog — Agent mode injects the unified skills list so the agent
        # can discover/load skills via skill_view; 语音助理 mode must do the same or
        # the voice agent has no awareness of skills and will confabulate when
        # the user asks to use one.
        try:
            from app.tools.skill_tools import build_skills_system_prompt

            # W17 轮次门控：语音轮为对话形态，不携带 force-inject 全文
            # （旧实现 [:2500] 截断本就只带入半截注入；目录保留可显式调用）。
            skills_prompt = await build_skills_system_prompt(self.user, include_force_inject=False)
            if skills_prompt:
                sections.append(skills_prompt.strip()[:2500])
        except Exception as exc:
            logger.debug("voice identity: skills catalog load failed: %s", exc)

        self._identity_prompt = "\n\n".join(s for s in sections if s and s.strip())
        self._memory_injected_context = self._identity_prompt
        self._identity_loaded = True
        # 基线捕获：会话启动记忆（哨兵区块）——空召回时 _apply_vmem_block(None)
        # 还原到此内容，避免跨话题残留上一主题的召回区块。
        self._vmem_baseline = _vmem_extract_block(self._identity_prompt)
        # Seed conversation history with the identity prompt as system message.
        if not self._history:
            self._history = []
    async def _ensure_conversation(self) -> None:
        """Ensure a conversation exists under the \u916c assistant.

        If ``conversation_id`` was passed from the client, verify it belongs
        to the user's \u916c assistant. Otherwise create a new one.
        """
        _sync_voicesessionsetup_globals()
        from app.db.database import Conversation
        from app.services.assistant_service import ensure_voice_assistant

        assistant = await ensure_voice_assistant(self.db, self.user.id)
        if self.conversation_id:
            result = await self.db.execute(
                select(Conversation).where(
                    Conversation.id == self.conversation_id,
                    Conversation.user_id == self.user.id,
                )
            )
            conv = result.scalar_one_or_none()
            if conv and conv.assistant_id != assistant.id:
                # Belongs to a different assistant — create a new voice session
                self.conversation_id = None
            elif conv:
                self._conversation = conv
        if not self.conversation_id:
            conv = Conversation(
                user_id=self.user.id,
                title="新语音对话",
                assistant_id=assistant.id,
            )
            self.db.add(conv)
            await self.db.commit()
            await self.db.refresh(conv)
            self.conversation_id = conv.id
            self._conversation = conv
        await self._send_json({
            "event": "session",
            "session_id": self.conversation_id,
            "assistant_id": assistant.id,
        })
    async def _load_history(self) -> None:
        """Load prior messages from the conversation into ``_history``.

        Tool interactions are reconstructed as well: without the tool_call /
        tool-result messages in context, the model has no access to the REAL
        data previous turns fetched (notebook names, search results, ...) and
        will confabulate. Persisted ``tool_calls`` is the raw OpenAI-style
        array; ``tool_results`` is the ToolResultsData display payload whose
        ``agent_steps`` keep the call id and result content.
        """
        _sync_voicesessionsetup_globals()
        if not self.conversation_id:
            return
        from app.db.database import Message

        result = await self.db.execute(
            select(Message).where(Message.conversation_id == self.conversation_id,
                                  Message.delivery_status != "streaming")
            .order_by(Message.created_at)
        )
        # 评审 I3：外置桩还原全文后再进语音上下文（否则 stub JSON 进模型/结构解析断裂）
        from app.services.message_payload_service import resolve_message_fields
        _rows = list(result.scalars())
        _rows = await resolve_message_fields(_rows, self.db)
        # conv a040c24e (D-12): historical [N] citation markers and numbered
        # search lists must never re-enter the model context — the voice path
        # has no citation ledger (markers would be read out by TTS or mimicked
        # into new answers), so neutralize them at replay like the chat path.
        from app.services.tool_history import (
            _denumber_search_formatted,
            neutralize_historical_citations,
        )
        for msg in _rows:
            if msg.role == "user" and msg.content:
                self._history.append({"role": "user", "content": msg.content})
                continue
            if msg.role != "assistant" or not msg.content:
                continue
            tool_calls = None
            if getattr(msg, "tool_calls", None):
                try:
                    tool_calls = json.loads(msg.tool_calls)
                except (json.JSONDecodeError, TypeError):
                    tool_calls = None
            if not tool_calls:
                self._history.append({
                    "role": "assistant",
                    "content": neutralize_historical_citations(msg.content),
                })
                continue
            steps: list[dict] = []
            if getattr(msg, "tool_results", None):
                try:
                    tr = json.loads(msg.tool_results)
                    if isinstance(tr, dict):
                        steps = tr.get("agent_steps") or []
                    elif isinstance(tr, list):
                        steps = tr
                except (json.JSONDecodeError, TypeError):
                    steps = []
            steps_by_id = {
                (s.get("name") or s.get("tool_call_id") or ""): s for s in steps
            }
            call_ids = [tc.get("id", "") for tc in tool_calls]
            if call_ids and all(cid in steps_by_id for cid in call_ids):
                # A4.9 r1 Important：落档的截断 arguments（未闭合 JSON）不得原样
                # 回放给供应商——严格实现会在重连后的首个回合 400。与派发侧同一
                # 分类器：不可解析参数归一化为 {}（原始载荷仍在 messages 表留档）。
                replay_calls = [
                    entry["safe_call"] for entry in _plan_voice_tool_calls(tool_calls)
                ]
                self._history.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": replay_calls,
                })
                for cid in call_ids:
                    self._history.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": _denumber_search_formatted(
                            steps_by_id[cid].get("content") or ""
                        )[:VOICE_REPLAY_TOOL_CONTENT_MAX_CHARS],
                    })
                self._history.append({
                    "role": "assistant",
                    "content": neutralize_historical_citations(msg.content),
                })
            else:
                self._history.append({
                    "role": "assistant",
                    "content": neutralize_historical_citations(msg.content),
                })
    async def _persist_turn_progress(
        self,
        content: str,
        tool_calls: Optional[str] = None,
        tool_results: Optional[str] = None,
        final: bool = False,
    ) -> None:
        """Upsert the in-progress assistant message for the current turn.

        CONTRACT — monotonic growth: ``content`` must be the CUMULATIVE turn
        text (``full_reply + reply_text`` mid-loop, folded ``full_reply``
        at finalize); every call replaces ``msg.content`` wholesale, so a
        partial snapshot would silently shrink user-visible history (same
        data-loss class as conv 4e159a79). Pinned by
        tests/test_voice_turn_progress_cumulative.py.

        Voice turns used to persist ONLY at successful completion — exiting
        voice mode mid-tool-loop (WS close cancels the responder) erased every
        trace: no tool calls, no partial answer, and the user could not tell
        whether anything had even started. The row is created at the first
        tool round and updated as the turn progresses, so the session record
        always reflects what has actually happened (incl. background_task
        submissions with their task_id)."""
        _sync_voicesessionsetup_globals()
        if not self.conversation_id:
            return
        from app.db.database import Conversation, Message

        try:
            text = content.strip() or "（正在执行…）"
            msg = None
            if self._turn_msg_id:
                msg = await self.db.get(Message, self._turn_msg_id)
                if msg is not None:
                    msg.content = text
                    msg.tool_calls = tool_calls
                    msg.tool_results = tool_results
                else:
                    self._turn_msg_id = None
            if msg is None:
                msg = Message(
                    conversation_id=self.conversation_id,
                    role="assistant",
                    content=text,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                )
                self.db.add(msg)
                await self.db.flush()
                self._turn_msg_id = msg.id
            conv_result = await self.db.execute(
                select(Conversation).where(Conversation.id == self.conversation_id)
            )
            conv = conv_result.scalar_one_or_none()
            if conv:
                conv.updated_at = datetime.utcnow()
            await self.db.commit()
            if final and conv and conv.title == "新语音对话":
                asyncio.create_task(self._generate_voice_title(text))
        except Exception as exc:
            logger.debug("voice persist turn progress failed: %s", exc)
    async def _park_preempted_tool_preamble(self) -> None:
        """Turn a superseded preamble into a tool trace, preserving real results."""
        _sync_voicesessionsetup_globals()
        if not self._turn_msg_id or not self.conversation_id:
            return
        from app.db.database import Message

        try:
            msg = await self.db.get(Message, self._turn_msg_id)
            if msg is not None:
                msg.content = "（工具执行记录，结果待向用户交代）"
                await self.db.commit()
        except Exception as exc:
            logger.debug("voice park turn progress failed: %s", exc)
        finally:
            self._turn_msg_id = None
    async def _persist_message(
        self,
        role: str,
        content: str,
        tool_calls: Optional[str] = None,
        tool_results: Optional[str] = None,
    ) -> None:
        """Persist a user/assistant message to the conversation.

        For assistant messages that involved tool calls, ``tool_calls`` and
        ``tool_results`` carry the JSON-encoded OpenAI-style arrays so the
        session record preserves the full tool interaction history.
        """
        _sync_voicesessionsetup_globals()
        if not self.conversation_id or not content.strip():
            return
        if role == "assistant":
            # 伪工具标记不落库（2026-11-07 语音事故）：DB 历史是跨会话污染
            # 的唯一载体，落库面剥离保证事故不再随历史回放复发。
            from app.services.voice_text import strip_pseudo_tool_tags

            content = strip_pseudo_tool_tags(content)
            if not content.strip():
                return
        from app.db.database import Conversation, Message

        try:
            msg = Message(
                conversation_id=self.conversation_id,
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_results=tool_results,
            )
            self.db.add(msg)
            conv_result = await self.db.execute(
                select(Conversation).where(Conversation.id == self.conversation_id)
            )
            conv = conv_result.scalar_one_or_none()
            if conv:
                conv.updated_at = datetime.utcnow()
            await self.db.commit()
            if role == "user":
                self._last_persisted_user_message_id = msg.id
            # LLM-based auto-title: generate title after the first assistant
            # reply, matching the same TitleGeneratorService used in Agent mode.
            if role == "assistant" and conv and conv.title == "\u65b0\u8bed\u97f3\u5bf9\u8bdd":
                asyncio.create_task(self._generate_voice_title(content))
        except Exception as exc:
            logger.debug("voice persist message failed: %s", exc)
    async def _rollback_last_user_message(self, text: str) -> None:
        """Delete the most recently persisted user message if it matches ``text``.

        Called when the intent classifier rejects a turn as noise/fragment —
        the user message was already persisted optimistically (so the frontend
        could show it immediately), but since the turn is not respondable we
        roll it back so noise doesn't pollute the conversation record.
        """
        _sync_voicesessionsetup_globals()
        if not self.conversation_id or not text.strip():
            return
        from app.db.database import Message

        try:
            result = await self.db.execute(
                select(Message)
                .where(
                    Message.conversation_id == self.conversation_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            msg = result.scalar_one_or_none()
            if msg and msg.content == text:
                await self.db.delete(msg)
                await self.db.commit()
                self._last_persisted_user_message_id = None
                logger.info("voice rolled back noise user message: %r", text)
        except Exception as exc:
            logger.debug("voice rollback user message failed: %s", exc)
    async def _generate_voice_title(self, assistant_response: str) -> None:
        """Generate an LLM-based title for the voice conversation,
        matching the same TitleGeneratorService used in Agent mode."""
        _sync_voicesessionsetup_globals()
        try:
            # Find the first user message for context
            first_user_msg = ""
            for h in self._history:
                if h.get("role") == "user":
                    first_user_msg = h.get("content", "")
                    break
            if not first_user_msg:
                return

            tg = TitleGeneratorService()
            try:
                title = await tg.generate_title(
                    user_query=first_user_msg,
                    assistant_response=assistant_response[:400],
                )
            except Exception:
                logger.exception("voice title LLM call failed; using fallback")
                title = None
            if not title:
                title = await tg.get_fallback_title(first_user_msg)
            if not title or title == "\u65b0\u8bed\u97f3\u5bf9\u8bdd":
                title = first_user_msg.strip()[:16] or "\u8bed\u97f3\u4f1a\u8bdd"

            from app.db.database import Conversation
            result = await self.db.execute(
                select(Conversation).where(Conversation.id == self.conversation_id)
            )
            conv_row = result.scalar_one_or_none()
            if conv_row and conv_row.title == "\u65b0\u8bed\u97f3\u5bf9\u8bdd":
                conv_row.title = title
                await self.db.commit()
                logger.info("voice title generated: %s", title)
        except Exception as exc:
            logger.debug("voice title generation failed: %s", exc)
    def _thinking_off_body(self, svc: Optional[LLMService] = None) -> dict:
        """Provider-aware payload that forces reasoning/thinking OFF.

        Voice turns are latency-sensitive: reasoning burns seconds before the
        first audible token, so thinking is disabled by default for EVERY
        provider. The knob format differs by vendor — qwen/DashScope expects
        ``enable_thinking`` while deepseek/zhipu/MiMo and other OpenAI-compatible
        endpoints expect ``thinking.type`` (see build_thinking_extra_body).
        """
        _sync_voicesessionsetup_globals()
        base_url = ""
        if svc is not None:
            try:
                base_url = str(getattr(svc.client, "base_url", "") or "")
            except Exception:
                base_url = ""
        if not base_url:
            try:
                # model_gateway 收口（2026-08-30）：voice 端点来自 routing
                from app.model_gateway.registry import get_model_registry
                base_url = get_model_registry().resolve("voice").base_url or ""
            except Exception:
                base_url = ""
        # wave-7：关思考 wire 形状由 thinking profile 统一给出。parity 注记
        # （A4.9 wave-7 R2 Minor2）：旧 voice 仅匹配 "dashscope" 字面量——
        # dashscope→enable_thinking False；其余（含 aliyuncs 非 dashscope）
        # →generic thinking{type:disabled}，不经 aliyuncs 敏感的 sniff。
        if "dashscope" in base_url.lower():
            from app.model_gateway.profiles import get_thinking_profile
            return get_thinking_profile("qwen").disable()
        from app.model_gateway.profiles import get_thinking_profile
        return get_thinking_profile("custom").disable()
    def _extra_body(self, svc: Optional[LLMService] = None) -> Optional[dict]:
        """Thinking OFF by default for every provider (voice.disable_thinking
        defaults to True); None only when the admin explicitly opts in to
        reasoning for voice turns."""
        _sync_voicesessionsetup_globals()
        if not self.config.voice_disable_thinking:
            return None
        return self._thinking_off_body(svc)
    def _trimmed_history(self) -> list[dict]:
        """Last N user turns of history. Turn-based (not message-count) so a
        turn with several tool messages counts as ONE turn. The window never
        begins with an orphaned ``tool`` message — providers reject a tool
        message whose assistant tool_calls message is missing."""
        _sync_voicesessionsetup_globals()
        turns = max(1, int(self.config.voice_context_turns or 8))
        msgs = self._history
        start = len(msgs)
        seen = 0
        while start > 0 and seen < turns:
            start -= 1
            if msgs[start].get("role") == "user":
                seen += 1
        window = msgs[start:]
        while window and window[0].get("role") == "tool":
            window = window[1:]
        # 污染环切断（2026-11-07 语音事故 conv 73875edd）：历史里一旦出现伪
        # 工具标记文本，模型会在后续轮模仿自己的伪格式并谎称已完成（带 tools
        # 也会，probe_no_tools_fingerprint 实证）。请求侧统一剥离，已污染的
        # 既有会话下一轮即恢复原生工具调用。
        if any(m.get("role") == "assistant" and "<" in (m.get("content") or "")
               for m in window):
            from app.services.voice_text import strip_pseudo_tool_tags

            window = [
                {**m, "content": strip_pseudo_tool_tags(m["content"]) or "（无内容）"}
                if m.get("role") == "assistant" and "<" in (m.get("content") or "")
                else m
                for m in window
            ]
        return window
    def _system_messages(self) -> list[dict]:
        """System message for the next generation. Folds a one-shot interruption
        note (if the previous answer's audio playback was cut) INTO the single
        system message so the LLM reliably honours it — some providers only act
        on the first system message, so we never send a second one. The note
        makes the LLM attribute the truncation to audio playback (and resume)
        instead of confabulating a reason.

        Also injects the current emotional state (so the answer tone reflects
        the agent's mood) and any interjection context (so the full answer is
        coherent with brief remarks already made during the user's speech)."""
        _sync_voicesessionsetup_globals()
        base = self._system_prompt()
        # Current-time injection — mirrors chat mode (agent_service dynamic
        # section: "当前时间: … (北京时间, …)"), INCLUDING the explicit
        # UTC+8 so a non-CST deployment stays correct (review I1). Voice
        # turns routinely ask "几点了/周几"; without a clock in context the
        # model can only confabulate. Computed PER CALL via _local_time_line
        # so a long session never serves a stale timestamp (2026-08-25
        # wave-2); the per-QUERY stamp lives in _generation_messages.
        base = base + "\n\n" + _local_time_line()
        # Inject emotion state so the main agent's tone matches the current mood.
        if self.config.voice_emotion_enabled and self._emotion != "calm":
            emotion_desc = {
                "interested": "你此刻对用户说的内容感到挺感兴趣的",
                "excited": "你此刻很激动，语气会比平时更夸张热烈",
                "upset": "你此刻有些不高兴/担忧，语气会带着关切和不满",
                "broken": "你此刻破防了，情绪有些失控，说话会比较冲动",
            }.get(self._emotion, "")
            if emotion_desc:
                base = base + "\n\n（情绪状态：)" + emotion_desc + "。自然地体现在回答中，不要刻意提及'我现在很激动'之类的话。"
        # Inject interjection context so the full answer doesn't repeat what
        # was already said in brief interjections during the user's speech.
        inj_note = self._build_interjection_context_note()
        if inj_note:
            base = base + "\n\n" + inj_note
        # ASR correction: raw transcript may be noisy; the model should use context.
        base = base + "\n\n" + _ASR_CORRECTION_HINT
        if self._pending_tool_summary_note:
            base = base + "\n\n" + self._pending_tool_summary_note
        if self._pending_interruption_note:
            base = base + "\n\n" + self._pending_interruption_note
            self._pending_interruption_note = None
            logger.info("voice: injecting playback-interruption note into LLM context")
        return [{"role": "system", "content": base}]
    def _generation_messages(self, extra_user: Optional[str] = None) -> list[dict]:
        """Assemble the LLM-bound messages for one answer generation: system
        (with its per-call clock) + trimmed history, plus an optional extra
        user text (prefetch path — the turn is not yet in history), then
        stamp the current query with the local time (query-level clock,
        2026-09-06) so every turn the model receives carries its own
        timestamp. The stamped query is a fresh dict — ``_history`` and DB
        persistence keep the raw text."""
        _sync_voicesessionsetup_globals()
        msgs = self._system_messages() + self._trimmed_history()
        if extra_user is not None:
            msgs.append({"role": "user", "content": extra_user})
        return _stamp_query_time(msgs)

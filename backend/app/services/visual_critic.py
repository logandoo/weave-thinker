# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""B4c VLM 视觉批评层（AutoDesign 2608.13560 visual critic 移植）.

对可视交付物（HTML/SVG 等）的版式质量做运行时校验：browser 截图 → 多模态
模型判版式（裁剪/重叠/可读性）→ 结论并入 verifier issues。文本诊断看不见
的版式失败由此可检。

视觉端点解析（2026-11-01 起）：**主模型探针优先**——当前死磕回合的助手模型
持有 `.endpoint` 时先探它，其次 `[endpoints.vlm]`；探针不通过才回落独立视觉
端点（`[deathmatch] visual_critic_enabled` 默认 false）。
任何一层未启用/未配置/失败 → fail-open 返回 None，绝不影响判定链。
"""
import asyncio
import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.services.playwright_thread import pw_ephemeral

logger = logging.getLogger(__name__)

# 可视交付物扩展名（确定性判定，无需 LLM）
VISUAL_EXTENSIONS = frozenset({".html", ".htm", ".svg"})

_VLM_TIMEOUT_SECONDS = 120.0
# 版式判定是短 JSON；思考模型会把预算烧在 reasoning 上，给足但仍设上界。
_VLM_MAX_TOKENS = 1024
_MAX_ISSUES = 6


async def _resolve_vlm(assistant_llm=None) -> Optional[object]:
    """视觉端点解析（2026-11-01：主模型探针优先 → 失败才回落 [endpoints.vlm]）。

    ``assistant_llm``（死磕回合的助手模型客户端）持有 .endpoint 时优先探它，
    否则 registry 主端点；主模型经行为探针确认有视觉就直接用它做版式批评。"""
    from app.model_gateway.vision_probe import resolve_vision_endpoint
    main_ep = getattr(assistant_llm, "endpoint", None)
    ep, reason = await resolve_vision_endpoint(main_endpoint=main_ep)
    if ep is None and reason not in ("no_vision",):
        logger.info("visual_critic: vision endpoint unavailable (%s)", reason)
    return ep


@pw_ephemeral
def _screenshot_html(file_path: str) -> Optional[bytes]:
    """Render an HTML/SVG file headless and return PNG bytes (sync — run via
    asyncio.to_thread; 同步 Playwright 不能在 asyncio loop 跑).
    2026-10-04：一次性线程隔离（自带 `with sync_playwright()` 会话；与池化
    会话不得同线程——A4.9 wave2 Critical）。"""
    try:
        from pathlib import Path
        from playwright.sync_api import sync_playwright
        # as_uri() percent-encodes — a naive "file://" concat breaks on
        # filenames containing # / ? / % / spaces (A4.9 W2-I2).
        url = Path(os.path.abspath(file_path)).as_uri()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="networkidle", timeout=15000)
                return page.screenshot(full_page=True, type="png")
            finally:
                browser.close()
    except Exception as exc:
        logger.info("visual_critic: screenshot failed for %s: %s", file_path, exc)
        return None


async def _call_vlm(ep, image_png: bytes, step: Dict[str, Any], goal: str) -> Optional[Dict[str, Any]]:
    """Send the screenshot + step expectations to the vision endpoint and parse
    JSON. A4.9 R2-I6：改走 model_gateway（`complete_chat_parts`）而不是裸
    `openai.AsyncOpenAI`——主模型优先后，端点常常是思考模型，裸客户端不套
    thinking profile / max_tokens，reasoning-only 回复会让判定静默变 None。"""
    try:
        from app.model_gateway import factory
        prompt = (
            "你是交付物版式审查员。下面是某个任务步骤产出的渲染截图。\n"
            f"任务目标（节选）: {(goal or '')[:400]}\n"
            f"步骤预期: {str(step.get('expected_output') or step.get('description') or '')[:300]}\n"
            "检查版式问题：内容裁剪/溢出、元素重叠、文字不可读、图表残缺、布局崩坏。"
            "版式可接受时 layout_ok=true；存在影响交付质量的问题时 layout_ok=false 并列出 issues。\n"
            '只输出JSON：{"layout_ok": true|false, "issues": ["问题1", ...]}'
        )
        b64 = base64.b64encode(image_png).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }]
        # 空 Key 绝不回落主密钥（同 vision_interpret / 探针约定）；is_custom=True
        # 保证用该端点自己的 base_url（A4.9 R2-I4）。
        ep_wired = ep.with_overrides(api_key=ep.api_key or "no-key", is_custom=True)
        llm = factory.build_llm_service(ep_wired)
        raw, _reasoning = await asyncio.wait_for(
            llm.complete_chat_parts(messages, temperature=0, max_tokens=_VLM_MAX_TOKENS),
            timeout=_VLM_TIMEOUT_SECONDS,
        )
        raw = (raw or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, dict) or "layout_ok" not in parsed:
            return None
        return {
            "layout_ok": bool(parsed.get("layout_ok")),
            "issues": [str(i)[:200] for i in (parsed.get("issues") or [])][:_MAX_ISSUES],
        }
    except Exception as exc:
        logger.info("visual_critic: VLM call failed: %s", exc)
        return None


async def critique_visual_artifacts(
    files: List[Dict[str, Any]],
    step: Dict[str, Any],
    goal: str,
    *,
    workspace_path: str = "",
    assistant_llm=None,
) -> Optional[Dict[str, Any]]:
    """Critique the first visual artifact among `files` (this turn's new/changed
    files). Returns {"layout_ok": bool, "issues": [...]} or None (fail-open:
    disabled / unconfigured / not visual / any error).

    A4.9 R1-I2/R2-I4：端点解析（现含主模型探针，最坏 60s）必须放在「本轮真的
    有可视交付物」判定之后——否则没有 .html/.svg 的步骤也要白白探一次。"""
    target = None
    for f in files or []:
        path = str(f.get("path") or "")
        if os.path.splitext(path)[1].lower() in VISUAL_EXTENSIONS:
            target = path if os.path.isabs(path) else os.path.join(workspace_path, path)
            break
    if not target or not os.path.exists(target):
        return None
    ep = await _resolve_vlm(assistant_llm)
    if ep is None:
        return None
    png = await asyncio.to_thread(_screenshot_html, target)
    if not png:
        return None
    verdict = await _call_vlm(ep, png, step, goal)
    if verdict is not None:
        verdict["file"] = os.path.basename(target)
    return verdict

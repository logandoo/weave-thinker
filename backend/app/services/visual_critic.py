# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""B4c VLM 视觉批评层（AutoDesign 2608.13560 visual critic 移植）.

对可视交付物（HTML/SVG 等）的版式质量做运行时校验：browser 截图 → 多模态
模型判版式（裁剪/重叠/可读性）→ 结论并入 verifier issues。文本诊断看不见
的版式失败由此可检。

配置面（管理员/开发者可选）：
- config_model.toml `[endpoints.vlm]` 独立多模态端点（空占位=未配置；purpose "vlm"）
- `[deathmatch] visual_critic_enabled`（默认 false）

任何一层未启用/未配置/失败 → fail-open 返回 None，绝不影响判定链。
"""
import asyncio
import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 可视交付物扩展名（确定性判定，无需 LLM）
VISUAL_EXTENSIONS = frozenset({".html", ".htm", ".svg"})

_VLM_TIMEOUT_SECONDS = 60.0
_MAX_ISSUES = 6


def _vlm_endpoint():
    """Resolve the dedicated multimodal endpoint; None when unconfigured
    (placeholder endpoint with empty base_url/model_name, or resolution
    failure)."""
    try:
        from app.model_gateway.registry import get_model_registry
        ep = get_model_registry().resolve("vlm")
        if not (ep.base_url or "") or not (ep.model_name or ""):
            return None
        return ep
    except Exception:
        return None


def _screenshot_html(file_path: str) -> Optional[bytes]:
    """Render an HTML/SVG file headless and return PNG bytes (sync — run via
    asyncio.to_thread; 同步 Playwright 不能在 asyncio loop 跑)."""
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
    """Send the screenshot + step expectations to the multimodal endpoint
    (OpenAI-compatible vision message). Returns parsed JSON or None."""
    try:
        import openai
        prompt = (
            "你是交付物版式审查员。下面是某个任务步骤产出的渲染截图。\n"
            f"任务目标（节选）: {(goal or '')[:400]}\n"
            f"步骤预期: {str(step.get('expected_output') or step.get('description') or '')[:300]}\n"
            "检查版式问题：内容裁剪/溢出、元素重叠、文字不可读、图表残缺、布局崩坏。"
            "版式可接受时 layout_ok=true；存在影响交付质量的问题时 layout_ok=false 并列出 issues。\n"
            '只输出JSON：{"layout_ok": true|false, "issues": ["问题1", ...]}'
        )
        b64 = base64.b64encode(image_png).decode("ascii")
        async with openai.AsyncOpenAI(
            base_url=ep.base_url,
            api_key=ep.api_key or "no-key",
            timeout=_VLM_TIMEOUT_SECONDS,
        ) as client:
            resp = await client.chat.completions.create(
                model=ep.model_name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }],
                temperature=0,
            )
        raw = (resp.choices[0].message.content or "").strip()
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
) -> Optional[Dict[str, Any]]:
    """Critique the first visual artifact among `files` (this turn's new/changed
    files). Returns {"layout_ok": bool, "issues": [...]} or None (fail-open:
    disabled / unconfigured / not visual / any error)."""
    ep = _vlm_endpoint()
    if ep is None:
        return None
    target = None
    for f in files or []:
        path = str(f.get("path") or "")
        if os.path.splitext(path)[1].lower() in VISUAL_EXTENSIONS:
            target = path if os.path.isabs(path) else os.path.join(workspace_path, path)
            break
    if not target or not os.path.exists(target):
        return None
    png = await asyncio.to_thread(_screenshot_html, target)
    if not png:
        return None
    verdict = await _call_vlm(ep, png, step, goal)
    if verdict is not None:
        verdict["file"] = os.path.basename(target)
    return verdict

# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


def _sanitize_filename(name: str) -> str:
    import re
    name = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    if not name:
        name = 'untitled'
    return name[:80]


_BIBLIOGRAPHY_HEADER_RE = re.compile(
    r'(?:\n\n?---[^\S\n]*\n+)?(?:^|\n)[^\S\n]*(?:#{1,6}[^\S\n]*|\*{1,2}[^\S\n]*)?'
    r'(?:参考文献|参考资料|参考来源|References|Sources|Reference)[^\S\n]*'
    r'(?:\*{1,2})?[^\S\n]*\n[\s\S]*$',
    re.IGNORECASE,
)
_CITATION_RE = re.compile(r'\[(\d{1,2})\]')
_PUBLISH_DATE_RE = re.compile(r'/(20[12]\d)[/\-](0[1-9]|1[0-2])[/\-]?(0[1-9]|[12]\d|3[01])?')


def _extract_publish_date(url: str) -> str:
    m = _PUBLISH_DATE_RE.search(url)
    if not m:
        ym = re.match(r'.*?/(20[12]\d)/', url)
        return f'{ym.group(1)}年' if ym else ''
    year, month, day = m.group(1), m.group(2), m.group(3)
    if day:
        return f'{year}年{int(month)}月{int(day)}日'
    return f'{year}年{int(month)}月'


def _append_citations_section(content: str, tool_results_json: str) -> str:
    """Strip any existing bibliography section and append a fresh one built
    from the web_search results stored in tool_results. Mirrors the frontend
    buildCitationsSection logic so PDF exports include references."""
    import json as _json

    try:
        data = _json.loads(tool_results_json)
    except Exception:
        return content

    results = data if isinstance(data, list) else (data.get('results') or [])
    if not results:
        return content

    body = _BIBLIOGRAPHY_HEADER_RE.sub('', content).rstrip()

    used = set()
    for m in _CITATION_RE.finditer(content):
        used.add(int(m.group(1)))
    if not used:
        used = set(range(1, len(results) + 1))

    sorted_idx = sorted(used)
    index_map = {old: new for new, old in enumerate(sorted_idx, 1)}

    lines = []
    for old in sorted_idx:
        r = results[old - 1] if old - 1 < len(results) else None
        if not r:
            continue
        url = r.get('url', '')
        try:
            domain = url.split('/')[2].replace('www.', '') if '/' in url and len(url.split('/')) > 2 else url
        except Exception:
            domain = url
        pub_date = _extract_publish_date(url)
        date_str = f' ({pub_date})' if pub_date else ''
        new_idx = index_map[old]
        title = r.get('title', '')
        lines.append(f'[{new_idx}] "{title}." *{domain}.* {url}{date_str}.')

    if not lines:
        return body

    # Renumber inline citations in code-free text
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', body)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = _CITATION_RE.sub(
                lambda mm: f'[{index_map.get(int(mm.group(1)), int(mm.group(1)))}]', part
            )
    body = ''.join(parts)

    return body + '\n\n---\n\n**参考来源**\n\n' + '\n\n'.join(lines)


async def _ensure_export_dir(workspace_root: str) -> Path:
    export_dir = Path(workspace_root) / "pdf_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir


async def _load_note(db, note_id: str, user_id: str):
    from sqlalchemy import select
    from app.db.database import Notebook, Note
    result = await db.execute(
        select(Note).join(Notebook, Notebook.id == Note.notebook_id)
        .where(Note.id == note_id, Notebook.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def _load_conversation(db, conversation_id: str, user_id: str):
    from sqlalchemy import select
    from app.db.database import Conversation
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _load_messages(db, conversation_id: str):
    from sqlalchemy import select
    from app.db.database import Message
    result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return result.scalars().all()


async def _get_workspace_root(db, user: Any) -> str:
    from app.services.workspace_service import ensure_user_workspace
    workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
    return str(Path(workspace.root_path).resolve())


def _resolve_workspace_file(target: str, workspace_root: str) -> Optional[str]:
    """Resolve *target* to an existing file inside *workspace_root*.

    Mirrors ``provide_file._resolve_within_workspace`` so pdf_export can
    accept the same path forms the agent already uses for provide_file:
    absolute path, workspace-relative path, or bare filename (recursive
    search). Returns the resolved absolute path or ``None``.
    """
    from app.tools.provide_file import _resolve_within_workspace
    return _resolve_within_workspace(target, workspace_root)


# Text-like file extensions that can be rendered as PDF via the markdown
# pipeline. Binary formats (docx/pptx/xlsx/pdf/...) are rejected — the agent
# should use ``provide_file`` to attach those as download cards instead.
_TEXT_PDF_EXTS = {".md", ".markdown", ".txt", ".text"}


async def pdf_export(args: Dict[str, Any], **kwargs) -> str:
    action = (args.get("action") or "export_note").lower()
    user = kwargs.get("user")
    db = kwargs.get("db")
    if not user:
        return json.dumps({"error": "User not available"}, ensure_ascii=False)

    close_db = False
    if db is None:
        from app.db.database import AsyncSessionLocal
        db = AsyncSessionLocal()
        close_db = True

    try:
        workspace_root = await _get_workspace_root(db, user)
        export_dir = await _ensure_export_dir(workspace_root)

        if action == "export_note":
            note_id = args.get("note_id")
            if not note_id:
                return json.dumps({"error": "note_id is required"}, ensure_ascii=False)
            note = await _load_note(db, note_id, user.id)
            if not note:
                return json.dumps(
                    {
                        "error": (
                            f"Note '{note_id}' not found。note_id 必须是笔记的 UUID"
                            "（通过 notes 工具或 /api/notes 接口获取），不能是文件名。"
                            "如需导出工作区中的文件（如 report.md），请改用 "
                            "action=\"export_file\" 并提供 file_path 参数。"
                        )
                    },
                    ensure_ascii=False,
                )

            from app.api.notes import _render_note_pdf

            pdf_bytes = await asyncio.to_thread(_render_note_pdf, note, workspace_root)
            safe_title = _sanitize_filename(note.title or "note")
            filename = f"{safe_title}_{uuid.uuid4().hex[:8]}.pdf"
            file_path = export_dir / filename
            file_path.write_bytes(pdf_bytes)

            return json.dumps(
                {
                    "success": True,
                    "action": action,
                    "filename": filename,
                    "file_path": str(file_path),
                    "download_url": f"/api/files/download?path={str(file_path)}",
                    "size": len(pdf_bytes),
                    "message": f"已导出笔记《{note.title or '无标题'}》PDF",
                },
                ensure_ascii=False,
            )

        if action == "export_file":
            # Export a workspace file (markdown/text) as a PDF. This closes the
            # gap where the agent creates a report file via terminal/execute_code
            # and then tries to export it as PDF — previously the agent would
            # pass the filename to export_note, which expects a DB Note UUID and
            # returned a misleading "Note 'X' not found" error (the file
            # existed in the workspace all along). See conv
            # 长文档 PDF 导出回归用例。
            file_path_arg = args.get("file_path") or args.get("path")
            if not file_path_arg:
                return json.dumps(
                    {"error": "file_path is required for export_file"},
                    ensure_ascii=False,
                )
            resolved = _resolve_workspace_file(str(file_path_arg), workspace_root)
            if resolved is None:
                return json.dumps(
                    {
                        "error": (
                            f"工作区中未找到文件 '{file_path_arg}'。"
                            "file_path 可以是绝对路径、工作区相对路径或纯文件名。"
                            "请确认文件已生成且位于用户工作区内。"
                        )
                    },
                    ensure_ascii=False,
                )
            ext = Path(resolved).suffix.lower()
            if ext not in _TEXT_PDF_EXTS:
                return json.dumps(
                    {
                        "error": (
                            f"export_file 仅支持文本/Markdown 文件（.md/.txt），"
                            f"收到 '{ext}'。对于二进制文件（docx/pptx/xlsx/pdf 等），"
                            "请使用 provide_file 工具将其作为下载卡片提供给用户。"
                        )
                    },
                    ensure_ascii=False,
                )
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                    file_content = fh.read()
            except Exception as exc:
                return json.dumps(
                    {"error": f"读取文件失败: {exc}"},
                    ensure_ascii=False,
                )
            if not file_content.strip():
                return json.dumps(
                    {"error": f"文件 '{file_path_arg}' 内容为空，无法导出 PDF"},
                    ensure_ascii=False,
                )

            from app.api.conversation import _render_messages_pdf

            title = Path(resolved).stem
            pdf_bytes = await asyncio.to_thread(
                _render_messages_pdf, title, file_content, "", workspace_root,
            )
            safe_title = _sanitize_filename(title)
            filename = f"{safe_title}_{uuid.uuid4().hex[:8]}.pdf"
            out_path = export_dir / filename
            out_path.write_bytes(pdf_bytes)

            return json.dumps(
                {
                    "success": True,
                    "action": action,
                    "source_file": resolved,
                    "filename": filename,
                    "file_path": str(out_path),
                    "download_url": f"/api/files/download?path={str(out_path)}",
                    "size": len(pdf_bytes),
                    "message": f"已导出工作区文件《{Path(resolved).name}》为 PDF",
                },
                ensure_ascii=False,
            )

        if action == "export_conversation":
            conversation_id = args.get("conversation_id")
            if not conversation_id:
                conversation = kwargs.get("conversation")
                if conversation:
                    conversation_id = str(getattr(conversation, "id", ""))
            if not conversation_id:
                return json.dumps({"error": "conversation_id is required"}, ensure_ascii=False)
            conversation = await _load_conversation(db, conversation_id, user.id)
            if not conversation:
                return json.dumps({"error": f"Conversation '{conversation_id}' not found"}, ensure_ascii=False)
            messages = await _load_messages(db, conversation_id)
            lines = []
            for msg in messages:
                role = "用户" if msg.role == "user" else "助手"
                body = msg.content or ""
                if msg.role == "assistant" and getattr(msg, "tool_results", None):
                    body = _append_citations_section(body, msg.tool_results)
                lines.append(f"## {role}\n\n{body}\n")

            # Append the current turn's in-progress assistant content if
            # provided by the agent loop. The current assistant message is
            # not yet persisted to the database when pdf_export is called
            # mid-turn, so without this the exported PDF would miss the
            # detailed analysis the agent just generated.
            current_turn_content = kwargs.get("current_turn_content", "")
            if current_turn_content and current_turn_content.strip():
                body = current_turn_content
                current_turn_tool_results = kwargs.get("current_turn_tool_results", "")
                if current_turn_tool_results:
                    body = _append_citations_section(body, current_turn_tool_results)
                lines.append(f"## 助手\n\n{body}\n")

            content = "\n".join(lines)

            from app.api.conversation import _render_messages_pdf

            pdf_bytes = await asyncio.to_thread(
                _render_messages_pdf,
                conversation.title or "对话记录", content, "", workspace_root,
            )
            safe_title = _sanitize_filename(conversation.title or "conversation")
            filename = f"{safe_title}_{uuid.uuid4().hex[:8]}.pdf"
            file_path = export_dir / filename
            file_path.write_bytes(pdf_bytes)

            return json.dumps(
                {
                    "success": True,
                    "action": action,
                    "filename": filename,
                    "file_path": str(file_path),
                    "download_url": f"/api/files/download?path={str(file_path)}",
                    "size": len(pdf_bytes),
                    "message": f"已导出对话《{conversation.title or '无标题'}》PDF",
                },
                ensure_ascii=False,
            )

        return json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False)
    except Exception as e:
        logger.exception("PDF export failed")
        return json.dumps({"error": f"PDF export failed: {str(e)}"}, ensure_ascii=False)
    finally:
        if close_db:
            await db.close()


registry.register(
    name="pdf_export",
    toolset="web",
    schema={
        "name": "pdf_export",
        "description": (
            "导出笔记、对话记录或工作区文件为 PDF（用户要求导出 PDF 时使用此工具，"
            "不要用 execute_code/terminal 自行生成）。\n"
            "三种动作：export_note（按笔记 UUID 导出数据库笔记）、"
            "export_conversation（按对话 ID 导出整个对话）、"
            "export_file（导出工作区 Markdown/文本文件，如 report.md）。\n"
            "注意：export_note 的 note_id 必须是笔记 UUID 不是文件名；"
            "导出工作区文件请用 export_file。导出完成后系统渲染下载按钮。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["export_note", "export_conversation", "export_file"],
                    "description": "导出类型",
                },
                "note_id": {
                    "type": "string",
                    "description": "笔记的 UUID（export_note 时必填，必须是 UUID 不是文件名）",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "对话 ID（export_conversation 时可选；如省略则导出当前对话）",
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "工作区文件路径（export_file 时必填）。"
                        "可以是绝对路径、工作区相对路径或纯文件名（会自动搜索）。"
                        "仅支持 .md/.txt 等文本文件。"
                    ),
                },
            },
            "required": ["action"],
        },
    },
    handler=pdf_export,
    is_async=True,
    description="导出笔记、对话或工作区文件为 PDF",
    emoji="",
)

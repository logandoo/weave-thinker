# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from typing import Any, Dict, List, Optional

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


async def _get_db():
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal()


async def _get_user_notebooks(db, user_id: str) -> List[Dict[str, Any]]:
    from sqlalchemy import select, func
    from app.db.database import Notebook, Note
    result = await db.execute(
        select(
            Notebook.id,
            Notebook.name,
            Notebook.is_default,
            func.count(Note.id).label("note_count"),
        )
        .outerjoin(Note, Note.notebook_id == Notebook.id)
        .where(Notebook.user_id == user_id)
        .group_by(Notebook.id)
        .order_by(Notebook.created_at)
    )
    rows = result.all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "is_default": row.is_default,
            "note_count": row.note_count or 0,
        }
        for row in rows
    ]


async def _get_notebook_notes(db, notebook_id: str, user_id: str) -> List[Dict[str, Any]]:
    from sqlalchemy import select
    from app.db.database import Notebook, Note
    result = await db.execute(
        select(Note.id, Note.title, Note.updated_at)
        .join(Notebook, Notebook.id == Note.notebook_id)
        .where(Note.notebook_id == notebook_id, Notebook.user_id == user_id)
        .order_by(Note.updated_at.desc())
    )
    rows = result.all()
    return [
        {
            "id": row.id,
            "title": row.title or "无标题",
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


async def _resolve_notebook(db, ref: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a notebook reference — accepts a notebook UUID or its name
    (case-insensitive). Voice agents often pass the notebook NAME where an id
    is expected; silently returning empty results makes the model hallucinate,
    so resolve names to their real ids here. Returns {id, name} or None."""
    from sqlalchemy import select
    from app.db.database import Notebook

    ref = (ref or "").strip()
    if not ref:
        return None
    result = await db.execute(
        select(Notebook).where(Notebook.user_id == user_id, Notebook.id == ref)
    )
    nb = result.scalar_one_or_none()
    if nb:
        return {"id": nb.id, "name": nb.name}
    result = await db.execute(
        select(Notebook).where(Notebook.user_id == user_id)
    )
    lowered = ref.lower()
    for nb in result.scalars():
        if (nb.name or "").strip().lower() == lowered:
            return {"id": nb.id, "name": nb.name}
    return None


def _notebook_not_found_payload(ref: str, notebooks: List[Dict[str, Any]]) -> str:
    """Error payload that lists the user's real notebooks so the model can
    self-correct instead of inventing notebook names."""
    return json.dumps(
        {
            "error": (
                f"未找到笔记本 '{ref}'。请从以下真实存在的笔记本中选择"
                "（使用其 id 或准确名称），不要编造其他笔记本。"
            ),
            "available_notebooks": [
                {"id": n["id"], "name": n["name"], "note_count": n["note_count"]}
                for n in notebooks
            ],
        },
        ensure_ascii=False,
    )


async def _get_note_by_id(db, note_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    from sqlalchemy import select
    from app.db.database import Notebook, Note
    result = await db.execute(
        select(Note.id, Note.title, Note.content, Note.notebook_id, Notebook.user_id)
        .join(Notebook, Notebook.id == Note.notebook_id)
        .where(Note.id == note_id, Notebook.user_id == user_id)
    )
    row = result.one_or_none()
    if not row:
        return None
    return {
        "id": row.id,
        "title": row.title or "无标题",
        "content": row.content or "",
        "notebook_id": row.notebook_id,
    }


async def _get_or_create_default_notebook(db, user: Any) -> str:
    from sqlalchemy import select
    from app.db.database import Notebook
    result = await db.execute(
        select(Notebook).where(
            Notebook.user_id == user.id,
            Notebook.is_default == True,
        )
    )
    nb = result.scalar_one_or_none()
    if nb:
        return nb.id
    result = await db.execute(
        select(Notebook).where(Notebook.user_id == user.id).order_by(Notebook.created_at)
    )
    nb = result.scalar_one_or_none()
    if nb:
        nb.is_default = True
        await db.commit()
        return nb.id
    nb = Notebook(
        user_id=user.id,
        name="默认笔记本",
        is_default=True,
    )
    db.add(nb)
    await db.commit()
    await db.refresh(nb)
    return nb.id


def _permission_needed_payload(action: str, details: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "error": f"Permission needed: {details.get('description', '危险操作')}",
            "_permission_needed": True,
            "_permission_key": details.get("permission_key", ""),
            "_permission_description": details.get("description", ""),
            "_action": action,
            **details,
        },
        ensure_ascii=False,
    )


async def notes_tool(args: Dict[str, Any], **kwargs) -> str:
    action = (args.get("action") or "").lower()
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
        if action == "list_notebooks":
            notebooks = await _get_user_notebooks(db, user.id)
            return json.dumps({"notebooks": notebooks, "count": len(notebooks)}, ensure_ascii=False)

        if action == "list_notes":
            notebook_id = args.get("notebook_id")
            if not notebook_id:
                notebooks = await _get_user_notebooks(db, user.id)
                if not notebooks:
                    return json.dumps({"notes": [], "count": 0}, ensure_ascii=False)
                notebook_id = notebooks[0]["id"]
            else:
                nb = await _resolve_notebook(db, notebook_id, user.id)
                if not nb:
                    notebooks = await _get_user_notebooks(db, user.id)
                    return _notebook_not_found_payload(notebook_id, notebooks)
                notebook_id = nb["id"]
            notes = await _get_notebook_notes(db, notebook_id, user.id)
            return json.dumps({"notes": notes, "count": len(notes), "notebook_id": notebook_id}, ensure_ascii=False)

        if action == "get_note":
            note_id = args.get("note_id")
            if not note_id:
                return json.dumps({"error": "note_id is required"}, ensure_ascii=False)
            note = await _get_note_by_id(db, note_id, user.id)
            if not note:
                return json.dumps({"error": f"Note '{note_id}' not found"}, ensure_ascii=False)
            return json.dumps(note, ensure_ascii=False)

        if action == "create_notebook":
            name = (args.get("name") or "").strip()
            if not name:
                return json.dumps({"error": "name is required for create_notebook"}, ensure_ascii=False)
            from app.db.database import Notebook
            nb = Notebook(
                user_id=user.id,
                name=name,
                is_default=bool(args.get("is_default", False)),
            )
            db.add(nb)
            await db.commit()
            await db.refresh(nb)
            return json.dumps(
                {
                    "success": True,
                    "id": nb.id,
                    "name": nb.name,
                    "is_default": nb.is_default,
                    "message": f"已创建笔记本《{nb.name}》",
                },
                ensure_ascii=False,
            )

        if action == "update_notebook":
            notebook_id = args.get("notebook_id")
            if not notebook_id:
                return json.dumps({"error": "notebook_id is required for update_notebook"}, ensure_ascii=False)
            nb = await _resolve_notebook(db, notebook_id, user.id)
            if not nb:
                notebooks = await _get_user_notebooks(db, user.id)
                return _notebook_not_found_payload(notebook_id, notebooks)
            name = (args.get("name") or "").strip()
            if not name:
                return json.dumps({"error": "name is required for update_notebook"}, ensure_ascii=False)
            from sqlalchemy import select
            from app.db.database import Notebook
            result = await db.execute(select(Notebook).where(Notebook.id == nb["id"], Notebook.user_id == user.id))
            nb_obj = result.scalar_one_or_none()
            if not nb_obj:
                notebooks = await _get_user_notebooks(db, user.id)
                return _notebook_not_found_payload(notebook_id, notebooks)
            old_name = nb_obj.name
            nb_obj.name = name
            await db.commit()
            await db.refresh(nb_obj)
            return json.dumps(
                {
                    "success": True,
                    "id": nb_obj.id,
                    "name": nb_obj.name,
                    "message": f"已将笔记本《{old_name}》重命名为《{nb_obj.name}》",
                },
                ensure_ascii=False,
            )

        if action == "delete_notebook":
            notebook_id = args.get("notebook_id")
            if not notebook_id:
                return json.dumps({"error": "notebook_id is required for delete_notebook"}, ensure_ascii=False)
            nb = await _resolve_notebook(db, notebook_id, user.id)
            if not nb:
                notebooks = await _get_user_notebooks(db, user.id)
                return _notebook_not_found_payload(notebook_id, notebooks)
            from sqlalchemy import select
            from app.db.database import Notebook
            result = await db.execute(select(Notebook).where(Notebook.id == nb["id"], Notebook.user_id == user.id))
            nb_obj = result.scalar_one_or_none()
            if not nb_obj:
                notebooks = await _get_user_notebooks(db, user.id)
                return _notebook_not_found_payload(notebook_id, notebooks)
            name = nb_obj.name
            await db.delete(nb_obj)
            await db.commit()
            return json.dumps(
                {
                    "success": True,
                    "id": nb["id"],
                    "message": f"已删除笔记本《{name}》及其下属笔记",
                },
                ensure_ascii=False,
            )

        if action == "create_note":
            title = args.get("title") or "无标题"
            content = args.get("content") or ""
            notebook_id = args.get("notebook_id")
            if notebook_id:
                nb = await _resolve_notebook(db, notebook_id, user.id)
                if not nb:
                    notebooks = await _get_user_notebooks(db, user.id)
                    return _notebook_not_found_payload(notebook_id, notebooks)
                notebook_id = nb["id"]
            else:
                notebook_id = await _get_or_create_default_notebook(db, user)
            from app.db.database import Note
            note = Note(
                notebook_id=notebook_id,
                title=title,
                content=content,
            )
            db.add(note)
            await db.commit()
            await db.refresh(note)
            return json.dumps(
                {
                    "success": True,
                    "id": note.id,
                    "title": note.title,
                    "notebook_id": notebook_id,
                    "message": f"已创建笔记《{note.title}》",
                },
                ensure_ascii=False,
            )

        if action == "update_note":
            note_id = args.get("note_id")
            if not note_id:
                return json.dumps({"error": "note_id is required"}, ensure_ascii=False)
            from sqlalchemy import select
            from app.db.database import Notebook, Note
            result = await db.execute(
                select(Note).join(Notebook, Notebook.id == Note.notebook_id)
                .where(Note.id == note_id, Notebook.user_id == user.id)
            )
            note = result.scalar_one_or_none()
            if not note:
                return json.dumps({"error": f"Note '{note_id}' not found"}, ensure_ascii=False)
            if "title" in args:
                note.title = args["title"] or note.title
            if "content" in args:
                note.content = args["content"]
            await db.commit()
            await db.refresh(note)
            return json.dumps(
                {
                    "success": True,
                    "id": note.id,
                    "title": note.title,
                    "message": f"笔记《{note.title or '无标题'}》已修改",
                },
                ensure_ascii=False,
            )

        if action == "delete_note":
            note_id = args.get("note_id")
            if not note_id:
                return json.dumps({"error": "note_id is required"}, ensure_ascii=False)
            from sqlalchemy import select
            from app.db.database import Notebook, Note
            result = await db.execute(
                select(Note).join(Notebook, Notebook.id == Note.notebook_id)
                .where(Note.id == note_id, Notebook.user_id == user.id)
            )
            note = result.scalar_one_or_none()
            if not note:
                return json.dumps({"error": f"Note '{note_id}' not found"}, ensure_ascii=False)
            title = note.title or "无标题"
            await db.delete(note)
            await db.commit()
            return json.dumps(
                {"success": True, "id": note_id, "message": f"已删除笔记《{title}》"},
                ensure_ascii=False,
            )

        return json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False)
    finally:
        if close_db:
            await db.close()


async def _notes_tool_with_permission(args: Dict[str, Any], **kwargs) -> str:
    action = (args.get("action") or "").lower()
    write_actions = ("create_note", "update_note", "delete_note", "create_notebook", "update_notebook", "delete_notebook")
    if not args.get("_permission_granted") and action in write_actions:
        permission_key_map = {
            "create_note": "note_create",
            "update_note": "note_edit",
            "delete_note": "note_delete",
            "create_notebook": "notebook_create",
            "update_notebook": "notebook_edit",
            "delete_notebook": "notebook_delete",
        }
        description_map = {
            "create_note": "Agent 请求创建笔记",
            "update_note": "Agent 请求修改笔记内容",
            "delete_note": "Agent 请求删除笔记",
            "create_notebook": "Agent 请求创建新笔记本",
            "update_notebook": "Agent 请求修改笔记本名称",
            "delete_notebook": "Agent 请求删除笔记本",
        }
        return _permission_needed_payload(
            action,
            {
                "permission_key": permission_key_map[action],
                "description": description_map[action],
                "action": action,
                "note_id": args.get("note_id", ""),
                "notebook_id": args.get("notebook_id", ""),
                "title": args.get("title", ""),
                "name": args.get("name", ""),
            },
        )
    return await notes_tool(args, **kwargs)


registry.register(
    name="notes",
    toolset="notes",
    schema={
        "name": "notes",
        "description": (
            "访问并操作用户的笔记本与笔记。支持读取笔记本列表、笔记列表、笔记内容，"
            "对笔记本进行创建、重命名、删除，以及对笔记进行新增、修改、删除。"
            "重要：写入操作（新增/修改/删除笔记或笔记本）只有在用户在当前对话中明确要求时才可执行，"
            "严禁主动、自发地对笔记进行任何写入操作。"
            "即使用户已授权笔记编辑权限，也不得在没有用户明确指令的情况下修改笔记内容。"
            "如果用户要求向一个不存在的笔记本中添加笔记，应先使用 create_notebook 创建该笔记本，"
            "再使用 create_note 添加笔记；不要假定默认笔记本就是目标笔记本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_notebooks",
                        "list_notes",
                        "get_note",
                        "create_note",
                        "update_note",
                        "delete_note",
                        "create_notebook",
                        "update_notebook",
                        "delete_notebook",
                    ],
                    "description": "要执行的操作",
                },
                "notebook_id": {
                    "type": "string",
                    "description": "笔记本 ID（list_notes / create_note / update_notebook / delete_notebook 使用）。也可传笔记本名称，系统会自动解析为 ID；建议使用 list_notebooks 返回的真实 id。",
                },
                "note_id": {
                    "type": "string",
                    "description": "笔记 ID（get_note / update_note / delete_note 使用）",
                },
                "title": {
                    "type": "string",
                    "description": "笔记标题（create_note / update_note 使用）",
                },
                "content": {
                    "type": "string",
                    "description": "笔记内容（create_note / update_note 使用）",
                },
                "name": {
                    "type": "string",
                    "description": "笔记本名称（create_notebook / update_notebook 使用）",
                },
                "is_default": {
                    "type": "boolean",
                    "description": "是否设为默认笔记本（create_notebook 使用，可选，默认 false）",
                },
            },
            "required": ["action"],
        },
    },
    handler=_notes_tool_with_permission,
    is_async=True,
    description="访问并操作用户的笔记本与笔记",
    emoji="",
)

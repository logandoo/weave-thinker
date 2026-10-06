# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


def _get_skills_dir() -> Path:
    backend_root = config.backend_root
    return backend_root / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a simple YAML-like frontmatter block (---\nkey: value\n---).

    Returns (frontmatter_dict, body_text). Supports flat ``key: value`` lines
    and block scalars (``key: |`` / ``key: >`` + indented continuation lines,
    as used by vibeweaver's multi-line description).
    """
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    header = text[3:end].strip('\n')
    body = text[end + 4:].lstrip('\n')
    meta: Dict[str, Any] = {}
    current_key: Optional[str] = None
    block_key: Optional[str] = None
    block_lines: List[str] = []
    for line in header.splitlines():
        stripped = line.strip()
        if block_key is not None:
            if line.startswith(' ') or line.startswith('\t') or stripped == '':
                block_lines.append(stripped)
                continue
            meta[block_key] = '\n'.join(block_lines).strip('\n')
            block_key = None
            block_lines = []
        if not stripped or stripped.startswith('#'):
            continue
        if line.startswith(' ') or line.startswith('\t'):
            # list item under a sequence key
            if current_key and isinstance(meta.get(current_key), list):
                item = stripped.lstrip('-').strip()
                if item:
                    meta[current_key].append(item)
            continue
        if ':' in stripped:
            key, _, val = stripped.partition(':')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val in ('|', '|-', '|+', '>', '>-', '>+'):
                block_key = key
                block_lines = []
                meta[key] = ''
                current_key = None
                continue
            if val == '':
                # empty value: might be a sequence; start a list and wait for indented items
                meta[key] = []
                current_key = key
            else:
                meta[key] = val
                current_key = key
    if block_key is not None:
        meta[block_key] = '\n'.join(block_lines).strip('\n')
    return meta, body


def _extract_skill_name(file_path: Path, skills_dir: Path) -> str:
    rel = file_path.relative_to(skills_dir)
    parts = rel.parts
    if len(parts) >= 2:
        return parts[-2]
    return file_path.parent.name


def _iter_skill_files(skills_dir: Path):
    if not skills_dir.exists():
        return
    for skill_file in sorted(skills_dir.rglob("SKILL.md")):
        yield skill_file


async def _load_skill_content(skill_file: Path) -> Optional[str]:
    try:
        content = await asyncio.to_thread(skill_file.read_text, encoding="utf-8")
        content = content.strip()
        if not content:
            return None
        frontmatter, body = _parse_frontmatter(content)
        return body or content
    except Exception as e:
        logger.debug("Failed to read skill file %s: %s", skill_file, e)
        return None


async def _load_system_skill(skill_file: Path, skills_dir: Path) -> Optional[Dict[str, Any]]:
    """Load a system skill file into the unified skill dict shape."""
    try:
        raw = await asyncio.to_thread(skill_file.read_text, encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to read system skill %s: %s", skill_file, e)
        return None
    raw = raw.strip()
    if not raw:
        return None
    frontmatter, body = _parse_frontmatter(raw)
    name = frontmatter.get("name") or _extract_skill_name(skill_file, skills_dir)
    return {
        "name": name,
        "description": frontmatter.get("description", "") or "",
        "category": frontmatter.get("category", "system") or "system",
        "content": body or raw,
        "source": "system",
        "path": str(skill_file.relative_to(skills_dir)),
    }


def is_system_skill(name: str) -> bool:
    """Return True if a system skill file exists with this name."""
    skills_dir = _get_skills_dir()
    if not skills_dir.exists():
        return False
    for skill_file in _iter_skill_files(skills_dir):
        if _extract_skill_name(skill_file, skills_dir) == name:
            return True
    return False


async def list_system_skills() -> List[Dict[str, Any]]:
    """Return all system skills as unified dicts (name/description/category/source)."""
    skills_dir = _get_skills_dir()
    if not await asyncio.to_thread(skills_dir.exists):
        return []
    out: List[Dict[str, Any]] = []
    for skill_file in _iter_skill_files(skills_dir):
        skill = await _load_system_skill(skill_file, skills_dir)
        if skill:
            out.append(skill)
    return out


async def resolve_skill(name: str, user: Any = None) -> Optional[Dict[str, Any]]:
    """Unified dual-source skill resolver.

    Looks up system skills (``backend/skills/<name>/SKILL.md``) first, then
    falls back to the user's DB skills. Returns a dict with keys:
    ``name, description, content, source, files?`` or ``None`` if not found.
    """
    if not name:
        return None
    # 1. system skills
    skills_dir = _get_skills_dir()
    if await asyncio.to_thread(skills_dir.exists):
        for skill_file in _iter_skill_files(skills_dir):
            if _extract_skill_name(skill_file, skills_dir) == name:
                skill = await _load_system_skill(skill_file, skills_dir)
                if skill:
                    # attach sibling executable files（B3：递归清单）
                    skill["skill_dir_rel"] = str(skill_file.parent.relative_to(skills_dir)) if skill_file.parent != skills_dir else name
                    sibling_files = await _list_system_skill_files(skill_file.parent)
                    if sibling_files:
                        skill["files"] = sibling_files
                        skill["has_executable_files"] = any(
                            f.get("is_executable") for f in sibling_files
                        )
                    return skill
    # 2. user skills
    user_skills = await _get_user_skills(user, include_files=True)
    for us in user_skills:
        if us["name"] == name:
            result = {
                "name": name,
                "description": us.get("description", ""),
                "content": us["content"],
                "source": "user",
            }
            if us.get("files"):
                result["files"] = us["files"]
                result["has_executable_files"] = any(
                    f.get("is_executable") for f in us["files"]
                )
            return result
    return None


# B3（vibeweaver 集成 P0）：伴读文件递归清单的治理上限。
_SKILL_FILE_MAX_DEPTH = 3
_SKILL_FILE_MAX_COUNT = 200
_SKILL_FILE_INLINE_MAX_BYTES = 32 * 1024
_SKILL_FILE_SKIP_DIRS = {".git", "__pycache__", "node_modules", "memory", "tests"}


async def _list_system_skill_files(skill_dir: Path) -> List[Dict[str, Any]]:
    """List a system skill's files recursively (depth/count/size bounded).

    B3（vibeweaver 集成）：伴读文档与 ``scripts/`` 子目录须可达——Read Contract
    要求 IN FULL 读伴读、``{VW_DIR}/scripts/...`` 引用脚本。SKILL.md 本体不入
    列表（它是 content 字段）。超过内联上限的文件只列元数据，经
    ``skill_view(name, path=...)`` 按需取件。
    """
    if not await asyncio.to_thread(skill_dir.exists):
        return []
    out: List[Dict[str, Any]] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > _SKILL_FILE_MAX_DEPTH or len(out) >= _SKILL_FILE_MAX_COUNT:
            return
        try:
            children = sorted(current.iterdir())
        except OSError:
            return
        for child in children:
            if len(out) >= _SKILL_FILE_MAX_COUNT:
                return
            if child.is_dir():
                if child.name in _SKILL_FILE_SKIP_DIRS:
                    continue
                _walk(child, depth + 1)
                continue
            if child.name == "SKILL.md" and child.parent == skill_dir:
                continue
            try:
                rel = str(child.relative_to(skill_dir))
            except ValueError:
                continue
            size = child.stat().st_size
            ext = child.suffix.lower()
            entry: Dict[str, Any] = {
                "path": rel,
                "type": ext.lstrip('.') or "txt",
                "is_executable": ext in {'.py', '.pyw', '.sh', '.bash', '.js', '.mjs', '.rb', '.pl'},
                "size": size,
            }
            if size <= _SKILL_FILE_INLINE_MAX_BYTES:
                try:
                    entry["content"] = child.read_text(encoding="utf-8")
                except Exception:
                    entry["content"] = ""
            else:
                entry["content_omitted"] = True
            out.append(entry)

    await asyncio.to_thread(_walk, skill_dir, 1)
    return out


async def _read_skill_file(skill_dir: Path, rel_path: str) -> Optional[str]:
    """Containment-checked read of one file inside a system skill dir."""
    if not rel_path or "\x00" in rel_path:
        return None
    try:
        target = (skill_dir / rel_path).resolve()
        root = skill_dir.resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    if not target.is_file():
        return None
    try:
        return await asyncio.to_thread(target.read_text, encoding="utf-8")
    except Exception:
        return None


async def _get_user_skills(user: Any, include_files: bool = False) -> List[Dict[str, Any]]:
    if not user:
        return []
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.db.database import UserSkill, SkillFile, AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            query = select(UserSkill).where(
                UserSkill.user_id == user.id,
                UserSkill.is_active == True
            )
            if include_files:
                query = query.options(selectinload(UserSkill.files))
            result = await session.execute(query)
            skills = result.scalars().all()

        result = []
        for s in skills:
            skill_data = {
                "name": s.name,
                "description": s.description or "",
                "content": s.content,
                "source": "user"
            }
            if include_files and hasattr(s, 'files'):
                skill_data["files"] = [
                    {
                        "path": f.file_path,
                        "content": f.file_content,
                        "type": f.file_type,
                        "is_executable": f.is_executable
                    }
                    for f in s.files
                ]
            result.append(skill_data)
        return result
    except Exception as e:
        logger.debug("Failed to load user skills: %s", e)
        return []


async def skill_list(args: dict, **kwargs) -> str:
    user = kwargs.get("user")

    skills = []

    for sys_skill in await list_system_skills():
        skills.append({
            "name": sys_skill["name"],
            "description": sys_skill.get("description", ""),
            "summary": (sys_skill.get("description", "") or sys_skill["name"])[:80],
            "category": sys_skill.get("category", "system"),
            "source": "system",
        })

    user_skills = await _get_user_skills(user)
    for us in user_skills:
        skills.append({
            "name": us["name"],
            "description": us["description"],
            "summary": us["description"][:80] if us["description"] else us["name"],
            "source": "user"
        })

    return json.dumps({"skills": skills, "count": len(skills)}, ensure_ascii=False)


def _vw_dir_substitute(text: str, vw_dir: str) -> str:
    """把 skill 文本内 {VW_DIR} 占位替换为 skill 根目录绝对路径。"""
    if not text or "{VW_DIR}" not in text:
        return text
    return text.replace("{VW_DIR}", vw_dir)


async def skill_view(args: dict, **kwargs) -> str:
    name = args.get("name", "").strip()
    if not name:
        return json.dumps({"error": "Skill name required"}, ensure_ascii=False)

    user = kwargs.get("user")
    resolved = await resolve_skill(name, user)
    if not resolved:
        return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)

    # vibeweaver 观察通道（A4）：skill 加载事件（audit 的会话筛选信号）。
    try:
        from app.services.agent_event_bus import AgentEvent as _AebEvent, agent_event_bus as _aeb
        _aeb.publish_soon(_AebEvent(
            type="skill.activated",
            session_id=str(kwargs.get("conversation_id") or ""),
            data={"name": name},
        ))
    except Exception:
        pass

    # B3：按文件取件（伴读/脚本，含子目录；大文件列表化后由此取全文）
    rel_path = str(args.get("path") or "").strip()
    if rel_path:
        if resolved.get("source") == "system":
            skills_dir = _get_skills_dir()
            skill_dir = (skills_dir / (resolved.get("skill_dir_rel") or name)).resolve()
            content = await _read_skill_file(skill_dir, rel_path)
            if content is None:
                return json.dumps({"error": f"File '{rel_path}' not found in skill '{name}'"}, ensure_ascii=False)
            vw_dir = str(skill_dir)
        else:
            files = resolved.get("files") or []
            match = next((f for f in files if f.get("path") == rel_path), None)
            if not match:
                return json.dumps({"error": f"File '{rel_path}' not found in skill '{name}'"}, ensure_ascii=False)
            content = match.get("content") or ""
            vw_dir = f"skill_scripts/{name}"
        return json.dumps({
            "name": name,
            "path": rel_path,
            "content": _vw_dir_substitute(content, vw_dir),
            "source": resolved["source"],
        }, ensure_ascii=False)

    vw_dir = (
        str((_get_skills_dir() / (resolved.get("skill_dir_rel") or name)).resolve())
        if resolved.get("source") == "system"
        else f"skill_scripts/{name}"
    )
    result = {
        "name": name,
        "content": _vw_dir_substitute(resolved["content"], vw_dir),
        "source": resolved["source"],
        "description": resolved.get("description", ""),
    }
    if resolved.get("files"):
        result["files"] = [
            {**f, **({"content": _vw_dir_substitute(f.get("content", ""), vw_dir)} if f.get("content") else {})}
            for f in resolved["files"]
        ]
        result["has_executable_files"] = resolved.get("has_executable_files", False)
    if resolved.get("path"):
        result["path"] = resolved["path"]
    result["vw_dir"] = vw_dir
    return json.dumps(result, ensure_ascii=False)


async def skill_manage(args: dict, **kwargs) -> str:
    action = args.get("action", "read")
    name = args.get("name", "").strip()
    content = args.get("content", "") or ""
    description = args.get("description", "") or ""

    user = kwargs.get("user")

    if action == "list":
        return await skill_list(args, **kwargs)

    if action == "view":
        return await skill_view(args, **kwargs)

    if action == "create" or action == "add":
        if not name:
            return json.dumps({"error": "Skill name required for create"}, ensure_ascii=False)
        if not content:
            return json.dumps({"error": "Content required for create"}, ensure_ascii=False)

        # Name collision: user skill may not shadow a system skill.
        if is_system_skill(name):
            return json.dumps({
                "error": f"技能名 '{name}' 与系统预置技能冲突，请更换名称"
            }, ensure_ascii=False)

        if user:
            try:
                from app.db.database import UserSkill, AsyncSessionLocal
                from sqlalchemy import select

                async with AsyncSessionLocal() as session:
                    existing = await session.execute(
                        select(UserSkill).where(
                            UserSkill.user_id == user.id,
                            UserSkill.name == name
                        )
                    )
                    if existing.scalar_one_or_none():
                        return json.dumps({"error": f"Skill '{name}' already exists"}, ensure_ascii=False)

                    skill = UserSkill(
                        user_id=user.id,
                        name=name,
                        description=description,
                        content=content,
                    )
                    session.add(skill)
                    await session.commit()
                    await session.refresh(skill)
                    return json.dumps({"action": "create", "name": name, "source": "user", "id": skill.id}, ensure_ascii=False)
            except Exception as e:
                logger.error("Failed to create user skill: %s", e)
                return json.dumps({"error": f"Failed to create skill: {str(e)}"}, ensure_ascii=False)

        skills_dir = _get_skills_dir()
        await asyncio.to_thread(skills_dir.mkdir, parents=True, exist_ok=True)
        skill_dir = skills_dir / name
        await asyncio.to_thread(skill_dir.mkdir, parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"

        header = ""
        if description:
            header = f"---\ndescription: {description}\n---\n\n"
        await asyncio.to_thread(skill_file.write_text, header + content, encoding="utf-8")
        logger.info("Skill '%s' created at %s", name, skill_file)
        return json.dumps({"action": "create", "name": name, "path": str(skill_file.relative_to(skills_dir)), "source": "system"}, ensure_ascii=False)

    if action == "update" or action == "patch":
        if not name:
            return json.dumps({"error": "Skill name required for update"}, ensure_ascii=False)
        if not content:
            return json.dumps({"error": "Content required for update"}, ensure_ascii=False)

        # System skills are read-only; reject update by name.
        if is_system_skill(name):
            return json.dumps({
                "error": f"系统技能 '{name}' 只读，不可修改"
            }, ensure_ascii=False)

        if user:
            try:
                from app.db.database import UserSkill, AsyncSessionLocal
                from sqlalchemy import select

                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(UserSkill).where(
                            UserSkill.user_id == user.id,
                            UserSkill.name == name
                        )
                    )
                    skill = result.scalar_one_or_none()
                    if not skill:
                        return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)

                    skill.content = content
                    if description:
                        skill.description = description
                    # 同步发射侧契约：content 必赋值（同值重存亦照发事件跳 onupdate，
                    # 复审 B I-2）——必 bump。
                    skill.updated_at = datetime.utcnow()
                    await session.commit()
                    return json.dumps({"action": "update", "name": name, "source": "user"}, ensure_ascii=False)
            except Exception as e:
                logger.error("Failed to update user skill: %s", e)
                return json.dumps({"error": f"Failed to update skill: {str(e)}"}, ensure_ascii=False)

        skills_dir = _get_skills_dir()
        skill_dir = skills_dir / name
        if not await asyncio.to_thread(skill_dir.exists):
            await asyncio.to_thread(skill_dir.mkdir, parents=True, exist_ok=True)

        skill_file = skill_dir / "SKILL.md"
        header = f"---\ndescription: {description}\n---\n\n" if description else ""
        await asyncio.to_thread(skill_file.write_text, header + content, encoding="utf-8")
        logger.info("Skill '%s' updated at %s", name, skill_file)
        return json.dumps({"action": "update", "name": name, "source": "system"}, ensure_ascii=False)

    if action == "delete" or action == "remove":
        if not name:
            return json.dumps({"error": "Skill name required for delete"}, ensure_ascii=False)

        if user:
            try:
                from app.db.database import UserSkill, AsyncSessionLocal
                from sqlalchemy import select

                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(UserSkill).where(
                            UserSkill.user_id == user.id,
                            UserSkill.name == name
                        )
                    )
                    skill = result.scalar_one_or_none()
                    if not skill:
                        return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)

                    await session.delete(skill)
                    await session.commit()
                    return json.dumps({"action": "delete", "name": name, "source": "user"}, ensure_ascii=False)
            except Exception as e:
                logger.error("Failed to delete user skill: %s", e)
                return json.dumps({"error": f"Failed to delete skill: {str(e)}"}, ensure_ascii=False)

        skills_dir = _get_skills_dir()
        skill_dir = skills_dir / name
        if not await asyncio.to_thread(skill_dir.exists):
            return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
        import shutil
        await asyncio.to_thread(shutil.rmtree, skill_dir)
        logger.info("Skill '%s' deleted", name)
        return json.dumps({"action": "delete", "name": name, "source": "system"}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown action: {action}. Use list/view/create/update/delete."}, ensure_ascii=False)


async def _exec_skill_script(script_full_path: Path, interpreter: str, script_args: str, timeout: float, cwd: str, meta: Dict[str, Any]) -> str:
    """共享执行尾：有界超时 + 输出截断，永不抛异常。"""
    cmd = [interpreter, str(script_full_path)]
    if script_args:
        cmd.extend(script_args.split())
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=min(timeout, 120)
        )
        payload = {
            "stdout": stdout.decode("utf-8", errors="replace")[:10000],
            "stderr": stderr.decode("utf-8", errors="replace")[:5000],
            "return_code": proc.returncode,
            "script": meta.get("script", ""),
            "skill": meta.get("skill", ""),
            "cwd": cwd,
        }
        return json.dumps(payload, ensure_ascii=False)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return json.dumps({
            "error": f"Script execution timed out after {timeout}s",
            "script": meta.get("script", ""),
            "skill": meta.get("skill", ""),
        }, ensure_ascii=False)


_INTERP_BY_EXT = {
    '.py': 'python3', '.pyw': 'python3',
    '.sh': 'bash', '.bash': 'bash',
    '.js': 'node', '.mjs': 'node',
    '.rb': 'ruby', '.pl': 'perl',
}

_EXECUTABLE_EXTS = {'.py', '.pyw', '.sh', '.bash', '.js', '.mjs', '.rb', '.pl'}


async def skill_run_script(args: dict, **kwargs) -> str:
    """Execute a script from a skill's files.

    B2（vibeweaver 集成 P0）：系统技能（backend/skills/<name>/，含 scripts/
    子目录）同样可执行——`skill_run_script` 此前仅覆盖用户库技能，导致
    assert_artifacts.py / mm_probe.py / scan_secrets.py 无路可跑。系统技能
    脚本就地执行（只读一等资产），cwd 默认工作区根（产物落工作区），
    可选 cwd 参数限工作区内；用户技能维持物化到 skill_scripts/ 的原语义。
    """
    skill_name = args.get("skill_name", "").strip()
    script_path = args.get("script_path", "").strip()
    script_args = args.get("args", "")
    timeout = args.get("timeout", 30)

    if not skill_name:
        return json.dumps({"error": "skill_name is required"}, ensure_ascii=False)
    if not script_path:
        return json.dumps({"error": "script_path is required"}, ensure_ascii=False)

    user = kwargs.get("user")
    if not user:
        return json.dumps({"error": "User context required to run skill scripts"}, ensure_ascii=False)

    workspace_path = str(kwargs.get("workspace_path") or "")
    cwd_arg = str(args.get("cwd") or "").strip()

    def _resolve_cwd(default: str) -> Optional[str]:
        base = Path(workspace_path).resolve() if workspace_path else Path(default).resolve()
        if not cwd_arg:
            return str(base)
        target = (base / cwd_arg).resolve()
        if target != base and base not in target.parents:
            return None
        return str(target)

    # ---- B2: 系统技能分支 ----
    if is_system_skill(skill_name):
        skills_dir = _get_skills_dir()
        skill_dir: Optional[Path] = None
        for skill_file in await asyncio.to_thread(lambda: list(_iter_skill_files(skills_dir))):
            if _extract_skill_name(skill_file, skills_dir) == skill_name:
                skill_dir = skill_file.parent
                break
        if skill_dir is None:
            return json.dumps({"error": f"Skill '{skill_name}' not found"}, ensure_ascii=False)
        try:
            script_full = (skill_dir / script_path).resolve()
        except OSError:
            script_full = None
        if script_full is None or skill_dir.resolve() not in script_full.parents:
            return json.dumps({"error": f"Script '{script_path}' escapes skill directory"}, ensure_ascii=False)
        if not script_full.is_file():
            return json.dumps({"error": f"Script '{script_path}' not found in skill '{skill_name}'"}, ensure_ascii=False)
        ext = script_full.suffix.lower()
        interpreter = _INTERP_BY_EXT.get(ext)
        if not interpreter:
            return json.dumps({"error": f"Unsupported script type: {ext}"}, ensure_ascii=False)
        cwd = _resolve_cwd(str(skill_dir))
        if cwd is None:
            return json.dumps({"error": "cwd escapes workspace"}, ensure_ascii=False)
        return await _exec_skill_script(
            script_full, interpreter, script_args, timeout, cwd,
            {"script": script_path, "skill": skill_name},
        )

    # ---- 用户技能分支（原语义：物化到 workspace/skill_scripts/）----
    try:
        from sqlalchemy import select
        from app.db.database import UserSkill, SkillFile, AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            skill_result = await session.execute(
                select(UserSkill).where(
                    UserSkill.user_id == user.id,
                    UserSkill.name == skill_name,
                    UserSkill.is_active == True
                )
            )
            skill = skill_result.scalar_one_or_none()
            if not skill:
                return json.dumps({"error": f"Skill '{skill_name}' not found"}, ensure_ascii=False)

            file_result = await session.execute(
                select(SkillFile).where(
                    SkillFile.skill_id == skill.id,
                    SkillFile.file_path == script_path
                )
            )
            skill_file = file_result.scalar_one_or_none()
            if not skill_file:
                return json.dumps({"error": f"Script '{script_path}' not found in skill '{skill_name}'"}, ensure_ascii=False)

            if not skill_file.is_executable:
                return json.dumps({"error": f"File '{script_path}' is not marked as executable"}, ensure_ascii=False)

        import tempfile
        import os

        if workspace_path:
            temp_dir = Path(workspace_path) / "skill_scripts" / skill_name
            await asyncio.to_thread(temp_dir.mkdir, parents=True, exist_ok=True)
        else:
            temp_dir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=f"skill_{skill_name}_"))

        script_full_path = temp_dir / script_path
        await asyncio.to_thread(script_full_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(script_full_path.write_text, skill_file.file_content, encoding="utf-8")

        file_ext = os.path.splitext(script_path)[1].lower()
        interpreter = _INTERP_BY_EXT.get(file_ext)
        if not interpreter:
            return json.dumps({"error": f"Unsupported script type: {file_ext}"}, ensure_ascii=False)

        await asyncio.to_thread(script_full_path.chmod, 0o755)
        cwd = _resolve_cwd(str(temp_dir))
        if cwd is None:
            return json.dumps({"error": "cwd escapes workspace"}, ensure_ascii=False)
        return await _exec_skill_script(
            script_full_path, interpreter, script_args, timeout, cwd,
            {"script": script_path, "skill": skill_name},
        )

    except Exception as e:
        logger.error("Failed to run skill script: %s", e)
        return json.dumps({"error": f"Failed to run script: {str(e)}"}, ensure_ascii=False)


_SKILL_PROMPT_CACHE: Dict[str, tuple[float, str]] = {}


async def build_skills_system_prompt(user: Any = None) -> str:
    user_id = str(user.id) if user and hasattr(user, "id") else "__no_user__"
    cache_key = f"skills_{user_id}"
    now = _time.monotonic()

    cached = _SKILL_PROMPT_CACHE.get(cache_key)
    if cached:
        cached_time, cached_value = cached
        if now - cached_time < 60:
            return cached_value

    skills_by_category: Dict[str, List[tuple]] = {}

    for sys_skill in await list_system_skills():
        category = sys_skill.get("category", "system") or "system"
        skills_by_category.setdefault(category, []).append(
            (sys_skill["name"], sys_skill.get("description", ""))
        )

    user_skills = await _get_user_skills(user)
    for us in user_skills:
        skills_by_category.setdefault("user", []).append((us["name"], us["description"]))

    if not skills_by_category:
        _SKILL_PROMPT_CACHE[cache_key] = (now, "")
        return ""

    lines = [
        "## Skills (mandatory)",
        "Before replying, scan the skills below. If a skill matches or is even partially relevant, load it with skill_view(name) and follow its instructions.",
        "用户也可用 `/skill_name` 或 `[skill:skill_name]` 显式调用某个技能。",
        "",
        "<available_skills>",
    ]
    for category in sorted(skills_by_category):
        lines.append(f"  {category}:")
        for name, desc in sorted(skills_by_category[category], key=lambda x: x[0]):
            if desc:
                lines.append(f"    - {name}: {desc}")
            else:
                lines.append(f"    - {name}")
    lines.append("</available_skills>")
    lines.append("")
    lines.append("Use skill_view(name) to load a skill's full instructions. Use skill_manage(action='create', ...) to save new skills. 系统技能（source=system）只读，用户技能不可与系统技能重名。")
    result = "\n".join(lines)

    # C2（vibeweaver 集成）：force-inject —— 指定 skill 的 SKILL.md 全文
    # 直接注入（基准证明 available 自触发率不足）；{VW_DIR} 占位替换为
    # skill 目录绝对路径。受 force_inject_max_chars 预算约束。
    try:
        force_list = get_config().agent_skills_force_inject
    except Exception:
        force_list = []
    if force_list:
        budget = 60000
        try:
            budget = max(0, int(get_config().agent_skills_force_inject_max_chars))
        except Exception:
            pass
        injected: List[str] = []
        used = 0
        for fname in force_list:
            try:
                resolved = await resolve_skill(fname, user)
            except Exception:
                resolved = None
            if not resolved:
                continue
            # A4.9 I6：仅系统 skill 可被 force-inject——用户库 skill 是用户
            # 自撰内容，不得以「authoritative」身份进入 system prompt。
            if resolved.get("source") != "system":
                logger.warning("force_inject skipped non-system skill '%s'", fname)
                continue
            vw_dir = (
                str((_get_skills_dir() / (resolved.get("skill_dir_rel") or fname)).resolve())
                if resolved.get("source") == "system"
                else f"skill_scripts/{fname}"
            )
            text = _vw_dir_substitute(resolved.get("content") or "", vw_dir)
            if not text:
                continue
            if used + len(text) > budget:
                logger.warning(
                    "force_inject budget exceeded at skill '%s' (%d+%d > %d) — skipped",
                    fname, used, len(text), budget,
                )
                continue
            used += len(text)
            injected.append(
                f"<force_injected_skill name=\"{fname}\">\n{text}\n</force_injected_skill>"
            )
        if injected:
            result = result + "\n\n## Force-injected skill instructions (authoritative)\n\n" + "\n\n".join(injected)

    _SKILL_PROMPT_CACHE[cache_key] = (now, result)
    return result


registry.register(
    name="skill_view",
    toolset="core",
    schema={
        "name": "skill_view",
        "description": "View the full content of a skill by name. Skills contain specialized instructions, workflows, and conventions.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill name to view."},
            },
            "required": ["name"],
        },
    },
    handler=skill_view,
    is_async=True,
    description="Load and view a skill's full instructions",
    emoji="",
)

registry.register(
    name="skill_manage",
    toolset="core",
    schema={
        "name": "skill_manage",
        "description": (
            "Manage skills. Actions:\n"
            "- list: list all available skills\n"
            "- view: view a specific skill by name\n"
            "- create: create a new skill (name + content required)\n"
            "- update: update an existing skill (name + content required)\n"
            "- delete: delete a skill by name"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "view", "create", "update", "delete"], "description": "Action to perform."},
                "name": {"type": "string", "description": "Skill name."},
                "content": {"type": "string", "description": "Skill content (for create/update)."},
                "description": {"type": "string", "description": "Short description (for create)."},
            },
            "required": ["action"],
        },
    },
    handler=skill_manage,
    is_async=True,
    description="Manage skills — list, view, create, update, delete",
    emoji="",
)

registry.register(
    name="skill_run_script",
    toolset="core",
    schema={
        "name": "skill_run_script",
        "description": (
            "Execute a script from a skill's files. "
            "Works for system skills (backend/skills/, incl. scripts/ subdirs) and user skills. "
            "Use this to run Python, Shell, JavaScript, or other scripts that are part of a skill. "
            "User-skill scripts must be uploaded as part of a skill ZIP file and marked as executable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The name of the skill containing the script."},
                "script_path": {"type": "string", "description": "The path to the script within the skill (e.g., 'scripts/helper.py')."},
                "args": {"type": "string", "description": "Optional arguments to pass to the script."},
                "timeout": {"type": "integer", "description": "Execution timeout in seconds (default: 30, max: 120)."},
                "cwd": {"type": "string", "description": "Optional working directory relative to the workspace root (default: workspace root for system skills)."},
            },
            "required": ["skill_name", "script_path"],
        },
    },
    handler=skill_run_script,
    is_async=True,
    description="Execute a script from a skill's files",
    emoji="",
)

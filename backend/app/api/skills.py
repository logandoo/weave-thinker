# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import Optional, List
import asyncio
import json
import zipfile
import io
import re
import os

from app.db.database import get_db, UserSkill, SkillFile, User
from app.schemas.skill import SkillCreate, SkillUpdate, SkillResponse, SkillFileResponse, ExecutableWarning
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/skills", tags=["skills"])

# Executable file extensions that require warning
EXECUTABLE_EXTENSIONS = {
    # Python
    '.py', '.pyw', '.pyc', '.pyo',
    # Shell scripts
    '.sh', '.bash', '.zsh', '.csh', '.fish', '.ksh',
    # Batch/Cmd
    '.bat', '.cmd', '.ps1', '.psm1',
    # Binary executables
    '.exe', '.dll', '.so', '.dylib', '.bin', '.app',
    # Java
    '.jar', '.class',
    # Node.js
    '.js', '.mjs', '.cjs',
    # Ruby
    '.rb', '.erb',
    # Perl
    '.pl', '.pm',
    # PHP
    '.php', '.phtml',
    # Lua
    '.lua',
    # R
    '.R', '.r',
    # Go
    '.go',
    # Rust
    '.rs',
    # C/C++
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp',
}

# Dangerous file patterns
DANGEROUS_PATTERNS = [
    r'\.exe$',
    r'\.dll$',
    r'\.so$',
    r'\.dylib$',
    r'\.bin$',
    r'\.app$',
    r'\.msi$',
    r'\.deb$',
    r'\.rpm$',
    r'\.dmg$',
    r'\.pkg$',
]


def _get_file_type(file_path: str) -> str:
    """Determine file type based on extension."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in {'.py', '.pyw', '.pyc', '.pyo'}:
        return 'python'
    elif ext in {'.sh', '.bash', '.zsh', '.csh', '.fish', '.ksh'}:
        return 'shell'
    elif ext in {'.bat', '.cmd', '.ps1', '.psm1'}:
        return 'batch'
    elif ext in {'.js', '.mjs', '.cjs'}:
        return 'javascript'
    elif ext in {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf'}:
        return 'config'
    elif ext in {'.md', '.txt', '.rst', '.doc'}:
        return 'documentation'
    elif ext in {'.exe', '.dll', '.so', '.dylib', '.bin', '.app'}:
        return 'binary'
    else:
        return 'other'


def _is_executable(file_path: str) -> bool:
    """Check if file is executable based on extension."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in EXECUTABLE_EXTENSIONS


def _is_dangerous(file_path: str) -> bool:
    """Check if file is potentially dangerous."""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, file_path, re.IGNORECASE):
            return True
    return False


def _scan_files_for_executables(files: List[tuple[str, bytes]]) -> List[ExecutableWarning]:
    """Scan a list of files (path, content) for executable files and return warnings."""
    warnings = []
    for file_path, _ in files:
        if _is_executable(file_path) or _is_dangerous(file_path):
            file_type = _get_file_type(file_path)
            is_dangerous = _is_dangerous(file_path)
            warnings.append(ExecutableWarning(
                file_path=file_path,
                file_type=file_type,
                is_dangerous=is_dangerous,
                warning_message=f"{'[DANGEROUS] ' if is_dangerous else ''}Executable file detected: {file_path} ({file_type})"
            ))
    return warnings


def _extract_zip_files(zip_content: bytes) -> List[tuple[str, bytes]]:
    """Synchronous zip extraction helper."""
    folder_files: List[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_content), 'r') as zip_ref:
        for zip_info in zip_ref.infolist():
            if zip_info.is_dir():
                continue
            parts = zip_info.filename.split('/')
            if len(parts) >= 2:
                file_content = zip_ref.read(zip_info)
                folder_files.append((zip_info.filename, file_content))
    return folder_files


def _scan_zip_executables(zip_content: bytes) -> List[ExecutableWarning]:
    """Scan zip file for executable files and return warnings."""
    warnings = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_content), 'r') as zip_ref:
            for zip_info in zip_ref.infolist():
                if zip_info.is_dir():
                    continue
                file_path = zip_info.filename
                if _is_executable(file_path) or _is_dangerous(file_path):
                    file_type = _get_file_type(file_path)
                    is_dangerous = _is_dangerous(file_path)
                    warnings.append(ExecutableWarning(
                        file_path=file_path,
                        file_type=file_type,
                        is_dangerous=is_dangerous,
                        warning_message=f"{'[DANGEROUS] ' if is_dangerous else ''}Executable file detected: {file_path} ({file_type})"
                    ))
    except Exception:
        pass
    return warnings


async def _process_skill_folder(
    skill_name: str,
    files: List[tuple[str, bytes]],
    db: AsyncSession,
    current_user: User
) -> Optional[UserSkill]:
    """Process a single skill folder given relative paths and file contents."""
    skill_md_file = None
    for rel_path, _ in files:
        if rel_path.endswith('SKILL.md'):
            skill_md_file = rel_path
            break

    if not skill_md_file:
        return None

    skill_md_content = next(content for path, content in files if path == skill_md_file)
    try:
        skill_content = skill_md_content.decode('utf-8')
    except UnicodeDecodeError:
        return None

    description = None
    if skill_content.startswith('---'):
        end = skill_content.find('\n---', 3)
        if end != -1:
            frontmatter = skill_content[3:end].strip()
            for line in frontmatter.split('\n'):
                if line.startswith('description:'):
                    description = line.split(':', 1)[1].strip()
                    break
            skill_content = skill_content[end + 4:].lstrip('\n')

    existing = await db.execute(
        select(UserSkill).where(
            UserSkill.user_id == current_user.id,
            UserSkill.name == skill_name
        )
    )
    if existing.scalar_one_or_none():
        return None

    from app.tools.skill_tools import is_system_skill as _is_sys
    if _is_sys(skill_name):
        # Skip system-skill name collision silently during folder upload;
        # the caller aggregates results and the user sees which were skipped.
        return None

    skill = UserSkill(
        user_id=current_user.id,
        name=skill_name,
        description=description,
        content=skill_content,
    )
    db.add(skill)
    await db.flush()

    for rel_path, file_content in files:
        if rel_path == skill_md_file:
            continue
        try:
            decoded_content = file_content.decode('utf-8')
            file_type = _get_file_type(rel_path)
            is_executable = _is_executable(rel_path)

            skill_file = SkillFile(
                skill_id=skill.id,
                file_path=rel_path,
                file_content=decoded_content,
                file_type=file_type,
                is_executable=is_executable,
            )
            db.add(skill_file)
        except Exception:
            continue

    return skill


def _serialize(s: UserSkill) -> SkillResponse:
    return SkillResponse(
        id=s.id,
        user_id=s.user_id,
        name=s.name,
        description=s.description,
        content=s.content,
        is_active=s.is_active,
        source="user",
        category=None,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


async def _serialize_system(sys_skill: dict) -> SkillResponse:
    return SkillResponse(
        id=f"system:{sys_skill['name']}",
        user_id="",
        name=sys_skill["name"],
        description=sys_skill.get("description") or None,
        content=sys_skill.get("content") or "",
        is_active=True,
        source="system",
        category=sys_skill.get("category"),
        created_at="",
        updated_at="",
    )


@router.get("", response_model=list[SkillResponse])
async def list_skills(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Merged list: system skills (read-only) + user DB skills.
    # See loop_improve.md Phase 1.4.
    from app.tools.skill_tools import list_system_skills

    result = await db.execute(
        select(UserSkill).where(UserSkill.user_id == current_user.id).order_by(desc(UserSkill.updated_at))
    )
    user_skills = result.scalars().all()
    out: list[SkillResponse] = [_serialize(s) for s in user_skills]
    for sys_skill in await list_system_skills():
        out.append(await _serialize_system(sys_skill))
    return out


@router.post("", response_model=SkillResponse)
async def create_skill(
    skill_data: SkillCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Name collision: user skill may not shadow a system skill.
    from app.tools.skill_tools import is_system_skill
    if is_system_skill(skill_data.name):
        raise HTTPException(
            status_code=400,
            detail=f"技能名 '{skill_data.name}' 与系统预置技能冲突，请更换名称",
        )

    existing = await db.execute(
        select(UserSkill).where(
            UserSkill.user_id == current_user.id,
            UserSkill.name == skill_data.name
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Skill with this name already exists")

    skill = UserSkill(
        user_id=current_user.id,
        name=skill_data.name,
        description=skill_data.description,
        content=skill_data.content,
        is_active=skill_data.is_active,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return _serialize(skill)


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(UserSkill).where(
            UserSkill.id == skill_id,
            UserSkill.user_id == current_user.id
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return _serialize(skill)


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    skill_data: SkillUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(UserSkill).where(
            UserSkill.id == skill_id,
            UserSkill.user_id == current_user.id
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    if skill_data.name is not None:
        from app.tools.skill_tools import is_system_skill
        if is_system_skill(skill_data.name):
            raise HTTPException(
                status_code=400,
                detail=f"技能名 '{skill_data.name}' 与系统预置技能冲突，请更换名称",
            )
        existing = await db.execute(
            select(UserSkill).where(
                UserSkill.user_id == current_user.id,
                UserSkill.name == skill_data.name,
                UserSkill.id != skill_id
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Skill with this name already exists")
        skill.name = skill_data.name

    if skill_data.description is not None:
        skill.description = skill_data.description
    if skill_data.content is not None:
        skill.content = skill_data.content
    if skill_data.is_active is not None:
        skill.is_active = skill_data.is_active

    await db.commit()
    await db.refresh(skill)
    return _serialize(skill)


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(UserSkill).where(
            UserSkill.id == skill_id,
            UserSkill.user_id == current_user.id
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    await db.delete(skill)
    await db.commit()
    return {"message": "Skill deleted"}


@router.post("/scan-zip")
async def scan_zip_executables(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """Scan zip file for executable files and return warnings before upload."""
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="Only .zip files are supported")

    content = await file.read()
    warnings = await asyncio.to_thread(_scan_zip_executables, content)

    return {
        "filename": file.filename,
        "has_executables": len(warnings) > 0,
        "executable_count": len(warnings),
        "warnings": [w.dict() for w in warnings],
        "dangerous_count": sum(1 for w in warnings if w.is_dangerous),
    }


@router.post("/upload", response_model=list[SkillResponse])
async def upload_skills(
    file: UploadFile = File(...),
    force: bool = Query(False, description="Force upload even with executable warnings"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    skills = []

    if file.filename.endswith('.zip'):
        content = await file.read()

        folder_files = await asyncio.to_thread(_extract_zip_files, content)

        # Check for executables unless force is True
        if not force:
            warnings = await asyncio.to_thread(_scan_files_for_executables, folder_files)
            dangerous_files = [w for w in warnings if w.is_dangerous]
            if dangerous_files:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "DANGEROUS_FILES_DETECTED",
                        "message": "Zip contains potentially dangerous executable files. Use /api/skills/scan-zip to review, then retry with force=true.",
                        "dangerous_files": [w.dict() for w in dangerous_files],
                    }
                )
            if warnings:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "EXECUTABLE_FILES_DETECTED",
                        "message": "Zip contains executable files. Use /api/skills/scan-zip to review, then retry with force=true.",
                        "warnings": [w.dict() for w in warnings],
                    }
                )

        # Group files by skill directory
        skill_dirs = {}
        for full_path, file_content in folder_files:
            parts = full_path.split('/')
            skill_dir = parts[0]
            relative_path = '/'.join(parts[1:])
            skill_dirs.setdefault(skill_dir, []).append((relative_path, file_content))

        for skill_dir, files in skill_dirs.items():
            skill = await _process_skill_folder(skill_dir, files, db, current_user)
            if skill:
                skills.append(skill)
    elif file.filename.endswith('.md'):
        content = await file.read()
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Skill file must be valid UTF-8")
        skill_name = file.filename.replace('.md', '').replace('SKILL', '').rstrip('.')
        if not skill_name:
            skill_name = 'uploaded_skill'

        description = None
        if content.startswith('---'):
            end = content.find('\n---', 3)
            if end != -1:
                frontmatter = content[3:end].strip()
                for line in frontmatter.split('\n'):
                    if line.startswith('description:'):
                        description = line.split(':', 1)[1].strip()
                        break
                content = content[end + 4:].lstrip('\n')

        existing = await db.execute(
            select(UserSkill).where(
                UserSkill.user_id == current_user.id,
                UserSkill.name == skill_name
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Skill with this name already exists")
        from app.tools.skill_tools import is_system_skill as _is_sys
        if _is_sys(skill_name):
            raise HTTPException(
                status_code=400,
                detail=f"技能名 '{skill_name}' 与系统预置技能冲突，请更换名称",
            )

        skill = UserSkill(
            user_id=current_user.id,
            name=skill_name,
            description=description,
            content=content,
        )
        db.add(skill)
        skills.append(skill)
    else:
        raise HTTPException(status_code=400, detail="Only .md and .zip files are supported")

    await db.commit()
    for skill in skills:
        await db.refresh(skill)
    return [_serialize(s) for s in skills]


@router.post("/scan-folder")
async def scan_folder_executables(
    files: List[UploadFile] = File(...),
    paths: List[str] = Form(...),
    current_user: User = Depends(get_current_user)
):
    """Scan an uploaded folder for executable files and return warnings before upload."""
    if len(files) != len(paths):
        raise HTTPException(status_code=400, detail="Files and paths count mismatch")

    folder_files = []
    for upload_file, path in zip(files, paths):
        content = await upload_file.read()
        folder_files.append((path, content))

    warnings = await asyncio.to_thread(_scan_files_for_executables, folder_files)

    return {
        "filename": "folder",
        "has_executables": len(warnings) > 0,
        "executable_count": len(warnings),
        "warnings": [w.dict() for w in warnings],
        "dangerous_count": sum(1 for w in warnings if w.is_dangerous),
    }


@router.post("/upload-folder", response_model=list[SkillResponse])
async def upload_skills_folder(
    files: List[UploadFile] = File(...),
    paths: List[str] = Form(...),
    force: bool = Query(False, description="Force upload even with executable warnings"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload skills from a folder (e.g. selected via webkitdirectory)."""
    if len(files) != len(paths):
        raise HTTPException(status_code=400, detail="Files and paths count mismatch")

    folder_files = []
    for upload_file, path in zip(files, paths):
        content = await upload_file.read()
        folder_files.append((path, content))

    if not force:
        warnings = await asyncio.to_thread(_scan_files_for_executables, folder_files)
        dangerous_files = [w for w in warnings if w.is_dangerous]
        if dangerous_files:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "DANGEROUS_FILES_DETECTED",
                    "message": "Folder contains potentially dangerous executable files. Use /api/skills/scan-folder to review, then retry with force=true.",
                    "dangerous_files": [w.dict() for w in dangerous_files],
                }
            )
        if warnings:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "EXECUTABLE_FILES_DETECTED",
                    "message": "Folder contains executable files. Use /api/skills/scan-folder to review, then retry with force=true.",
                    "warnings": [w.dict() for w in warnings],
                }
            )

    skill_dirs = {}
    for full_path, file_content in folder_files:
        parts = full_path.split('/')
        if len(parts) >= 2:
            skill_dir = parts[0]
            relative_path = '/'.join(parts[1:])
            skill_dirs.setdefault(skill_dir, []).append((relative_path, file_content))

    skills = []
    for skill_dir, files in skill_dirs.items():
        skill = await _process_skill_folder(skill_dir, files, db, current_user)
        if skill:
            skills.append(skill)

    await db.commit()
    for skill in skills:
        await db.refresh(skill)
    return [_serialize(s) for s in skills]


@router.get("/by-name/{skill_name}", response_model=SkillResponse)
async def get_skill_by_name(
    skill_name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Resolve via unified dual-source resolver (system first, then user).
    from app.tools.skill_tools import resolve_skill

    resolved = await resolve_skill(skill_name, current_user)
    if not resolved:
        raise HTTPException(status_code=404, detail="Skill not found")
    if resolved["source"] == "system":
        return await _serialize_system(resolved)
    # user skill: re-fetch from DB to get id/timestamps
    result = await db.execute(
        select(UserSkill).where(
            UserSkill.user_id == current_user.id,
            UserSkill.name == skill_name
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return _serialize(skill)

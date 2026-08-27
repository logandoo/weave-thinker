# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""File upload API — conversation uploads are saved as-is for agentic parsing."""
import asyncio
import base64
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db, User, Notebook, Note
from app.core.deps import get_current_user
from app.services.file_parser import parse_file, detect_file_type, IMAGE_EXTENSIONS
from app.services.workspace_service import ensure_user_workspace
from app.services.http_client import get_shared_async_client
from app.schemas.file_upload import FileParseResult, FileUploadResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])

MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GB
MAX_IMAGE_SIZE = 10 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024

_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
}


def _write_temp_file(suffix: str, content: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        return tmp.name


def _remove_file(path: str) -> None:
    os.unlink(path)


def _sanitize_filename(filename: str) -> str:
    name = filename.replace("..", ".").strip()
    if not name:
        name = "upload"
    return re.sub(r'[\\/:*?"<>|]', "_", name)


async def _save_uploaded_file(
    content: bytes,
    filename: str,
    workspace_root: str | Path,
) -> str:
    safe_name = _sanitize_filename(filename)
    ext = Path(safe_name).suffix.lower() or ".bin"
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    uploads_dir = Path(workspace_root) / "uploads"
    await asyncio.to_thread(uploads_dir.mkdir, parents=True, exist_ok=True)
    file_path = uploads_dir / unique_name
    await asyncio.to_thread(file_path.write_bytes, content)
    return str(file_path.resolve())


async def _stream_save_uploaded_file(
    upload: UploadFile,
    filename: str,
    workspace_root: str | Path,
    max_size: int = MAX_FILE_SIZE,
) -> tuple[str, int]:
    """Stream an upload directly to disk without loading it into memory."""
    safe_name = _sanitize_filename(filename)
    ext = Path(safe_name).suffix.lower() or ".bin"
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    uploads_dir = Path(workspace_root) / "uploads"
    await asyncio.to_thread(uploads_dir.mkdir, parents=True, exist_ok=True)
    file_path = uploads_dir / unique_name

    def _init_file() -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "wb"):
            pass

    def _append_chunks(chunks: list[bytes]) -> None:
        with open(file_path, "ab") as f:
            for chunk in chunks:
                f.write(chunk)

    await asyncio.to_thread(_init_file)

    total_size = 0
    buffer: list[bytes] = []
    buffer_size = 0
    flush_threshold = 32 * 1024 * 1024

    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_size:
            await asyncio.to_thread(file_path.unlink, missing_ok=True)
            raise ValueError(
                f"文件过大 ({total_size // (1024 * 1024)}MB)，"
                f"最大支持 {max_size // (1024 * 1024)}MB"
            )
        buffer.append(chunk)
        buffer_size += len(chunk)
        if buffer_size >= flush_threshold:
            await asyncio.to_thread(_append_chunks, buffer)
            buffer = []
            buffer_size = 0

    if buffer:
        await asyncio.to_thread(_append_chunks, buffer)

    return str(file_path.resolve()), total_size


async def _save_note_image(
    content: bytes,
    user_id: str,
    username: str | None,
    db: AsyncSession,
    ext: str = ".png",
) -> str:
    import asyncio
    if len(content) > MAX_IMAGE_SIZE:
        raise ValueError("图片过大，最大支持 10MB")
    ext = ext.lower()
    if ext not in IMAGE_EXTENSIONS:
        ext = ".png"
    workspace = await ensure_user_workspace(db, user_id, username)
    images_dir = Path(workspace.root_path) / "noteimg"
    await asyncio.to_thread(images_dir.mkdir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = images_dir / filename
    await asyncio.to_thread(file_path.write_bytes, content)
    return f"noteimg/{filename}"


def _extract_mime_from_data_uri(uri: str) -> str:
    m = re.match(r"data:([^;]+);base64,", uri)
    return m.group(1) if m else "image/png"


def _ext_from_mime(mime: str) -> str:
    return _IMAGE_EXT_BY_MIME.get(mime.lower(), ".png")


def _clean_image_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith('"') and '"' in ref[1:]:
        ref = ref[1:ref.index('"', 1)]
    elif ref.startswith("'") and "'" in ref[1:]:
        ref = ref[1:ref.index("'", 1)]
    return ref.split()[0]


async def process_imported_markdown_images(
    markdown: str,
    user_id: str,
    username: str | None,
    db: AsyncSession,
    sibling_files: dict[str, bytes] | None = None,
) -> str:
    if not markdown:
        return markdown

    sibling_files = sibling_files or {}
    replacements: list[tuple[int, int, str]] = []

    data_uri_pattern = re.compile(
        r"!\[([^\]]*)\]\((data:[^;]+;base64,([A-Za-z0-9+/=]+))\)"
    )
    for m in data_uri_pattern.finditer(markdown):
        alt = m.group(1)
        full_uri = m.group(2)
        b64 = m.group(3)
        try:
            data = base64.b64decode(b64)
            mime = _extract_mime_from_data_uri(full_uri)
            ext = _ext_from_mime(mime)
            path = await _save_note_image(data, user_id, username, db, ext)
            replacements.append((m.start(), m.end(), f"![{alt}]({path})"))
        except Exception as e:
            logger.warning("Failed to import embedded image: %s", e)

    url_pattern = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
    for m in url_pattern.finditer(markdown):
        alt = m.group(1)
        url = m.group(2)
        try:
            client = get_shared_async_client()
            r = await client.get(url, timeout=30)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "")
            mime = content_type.split(";")[0].strip()
            ext = _ext_from_mime(mime)
            path = await _save_note_image(r.content, user_id, username, db, ext)
            replacements.append((m.start(), m.end(), f"![{alt}]({path})"))
        except Exception as e:
            logger.warning("Failed to download external image %s: %s", url, e)

    local_pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
    for m in local_pattern.finditer(markdown):
        alt = m.group(1)
        ref = _clean_image_ref(m.group(2))
        if ref.startswith(("http://", "https://", "data:", "blob:")):
            continue
        basename = os.path.basename(ref)
        matched = None
        for filename, data in sibling_files.items():
            if os.path.basename(filename) == basename:
                matched = (filename, data)
                break
        if matched:
            _, data = matched
            ext = os.path.splitext(basename)[1].lower() or ".png"
            try:
                path = await _save_note_image(data, user_id, username, db, ext)
                replacements.append((m.start(), m.end(), f"![{alt}]({path})"))
            except Exception as e:
                logger.warning("Failed to import sibling image %s: %s", basename, e)

    for start, end, new_text in sorted(replacements, reverse=True):
        markdown = markdown[:start] + new_text + markdown[end:]

    return markdown


@router.post("/upload", response_model=FileUploadResponse)
async def upload_and_parse_files(
    files: list[UploadFile] = File(...),
    save_to_notebook: bool = False,
    notebook_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not files:
        raise HTTPException(status_code=400, detail="未选择文件")

    if notebook_id:
        from sqlalchemy import select
        stmt = select(Notebook).where(
            Notebook.id == notebook_id,
            Notebook.user_id == current_user.id,
        )
        result = await db.execute(stmt)
        existing_nb = result.scalar_one_or_none()
        if not existing_nb:
            raise HTTPException(status_code=404, detail="笔记本不存在")

    workspace = await ensure_user_workspace(
        db, current_user.id, current_user.username
    )
    workspace_root = str(Path(workspace.root_path).resolve())

    max_size_mb = MAX_FILE_SIZE // (1024 * 1024)
    results: list[FileParseResult] = []

    if not save_to_notebook:
        # Conversation uploads: stream directly to workspace without loading into memory
        for upload in files:
            filename = upload.filename or "unknown"
            try:
                file_path, size = await _stream_save_uploaded_file(
                    upload, filename, workspace_root
                )
                ext = os.path.splitext(filename)[1].lower()
                results.append(FileParseResult(
                    success=True,
                    filename=filename,
                    file_path=file_path,
                    size=size,
                    file_type=ext.lstrip(".") or "bin",
                ))
            except Exception as e:
                logger.exception("Error saving uploaded file %s", filename)
                results.append(FileParseResult(
                    success=False,
                    error=f"保存失败: {str(e)[:200]}",
                    filename=filename,
                ))
    else:
        upload_bytes: dict[str, bytes] = {}
        for upload in files:
            upload_bytes[upload.filename or "unknown"] = await upload.read()

        md_files: list[tuple[str, bytes]] = []
        doc_files: list[tuple[str, bytes]] = []
        for filename, data in upload_bytes.items():
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".md", ".markdown"):
                md_files.append((filename, data))
            else:
                doc_files.append((filename, data))

        sibling_images = {
            filename: data
            for filename, data in upload_bytes.items()
            if detect_file_type(filename) == "image"
        }

        for filename, content in doc_files:
            if len(content) > MAX_FILE_SIZE:
                results.append(FileParseResult(
                    success=False,
                    error=f"文件过大 ({len(content) // (1024 * 1024)}MB)，最大支持 {max_size_mb}MB",
                    filename=filename,
                ))
                continue

            suffix = os.path.splitext(filename)[1].lower()
            ft = detect_file_type(filename)
            if ft == "image":
                results.append(FileParseResult(
                    success=False,
                    error="暂不支持解析图片文件，请上传 Word、PPT、Excel、CSV、PDF 或 Markdown 文档。",
                    file_type="image",
                    filename=filename,
                ))
                continue

            if ft is None:
                results.append(FileParseResult(
                    success=False,
                    error=f"不支持的文件格式: {suffix}",
                    file_type=suffix,
                    filename=filename,
                ))
                continue

            tmp_path = None
            try:
                tmp_path = await asyncio.to_thread(_write_temp_file, suffix, content)

                parsed = await parse_file(tmp_path, filename)
                if parsed.get("success") and parsed.get("markdown"):
                    parsed["markdown"] = await process_imported_markdown_images(
                        parsed["markdown"],
                        current_user.id,
                        current_user.username,
                        db,
                        sibling_images,
                    )
                results.append(FileParseResult(**parsed))
            except Exception as e:
                logger.exception("Error parsing %s", filename)
                results.append(FileParseResult(
                    success=False,
                    error=f"解析失败: {str(e)[:200]}",
                    filename=filename,
                ))
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        await asyncio.to_thread(_remove_file, tmp_path)
                    except OSError:
                        pass

        for filename, content in md_files:
            if len(content) > MAX_FILE_SIZE:
                results.append(FileParseResult(
                    success=False,
                    error=f"文件过大 ({len(content) // (1024 * 1024)}MB)，最大支持 {max_size_mb}MB",
                    filename=filename,
                ))
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = content.decode("gbk")
                except UnicodeDecodeError:
                    text = content.decode("utf-8", errors="replace")
            try:
                text = await process_imported_markdown_images(
                    text,
                    current_user.id,
                    current_user.username,
                    db,
                    sibling_images,
                )
                results.append(FileParseResult(
                    success=True,
                    markdown=text,
                    file_type="md",
                    filename=filename,
                ))
            except Exception as e:
                logger.exception("Error importing markdown %s", filename)
                results.append(FileParseResult(
                    success=False,
                    error=f"导入失败: {str(e)[:200]}",
                    filename=filename,
                ))

    target_notebook_id = notebook_id
    notebook_name = None

    if save_to_notebook and not target_notebook_id:
        successful = [r for r in results if r.success and r.markdown]
        if successful:
            now = datetime.now().strftime("%Y-%m-%d %H%M%S")
            notebook_name = f"文件导入 {now}"
            notebook = Notebook(user_id=current_user.id, name=notebook_name)
            db.add(notebook)
            await db.commit()
            await db.refresh(notebook)
            target_notebook_id = notebook.id

    if target_notebook_id:
        successful = [r for r in results if r.success and r.markdown]
        for r in successful:
            title = os.path.splitext(r.filename)[0] if r.filename else "未命名文件"
            note = Note(
                notebook_id=target_notebook_id,
                title=title,
                content=r.markdown,
            )
            db.add(note)

        await db.commit()

    return FileUploadResponse(
        results=results,
        notebook_id=target_notebook_id,
        notebook_name=notebook_name,
    )

# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""皮肤目录与用户 UI 偏好 + 上传皮肤（wave-11）。

- GET /api/skins：内置皮肤目录（公开，登录页未来换肤可用）。
- GET/PUT /api/users/me/preferences：当前用户的皮肤偏好（需登录），
  持久化到 users.ui_preferences（JSON 字符串）。
- 上传皮肤（开发者通道，per-user 隔离）：
  POST /api/skins/upload · GET /api/skins/mine ·
  GET /api/skins/{skin_id}/css · DELETE /api/skins/{skin_id}
  文档：docs/API.md「上传皮肤」节。

皮肤令牌契约与社区接入指南见 docs/SKINS.md；前端注册表在
frontend/src/config/skins.ts —— 两边 id 必须一致（tests/api/test_skin_api.py
与 frontend/e2e/skin_system.spec.ts 双向断言）。
"""
import hashlib
import json
import os
import re
import shutil
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status as http_status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.database import User, get_db
from app.schemas.skin import UiPreferencesResponse, UiPreferencesUpdate

router = APIRouter(tags=["skins"])

TOKEN_CONTRACT_VERSION = "1"
DEFAULT_SKIN_ID = "verdant-flat"

# 上传皮肤护栏
MAX_SKIN_CSS_BYTES = 300_000
SKIN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,49}\Z")
_FORBIDDEN_PATTERNS = ("expression(", "javascript:")

CUSTOM_SKINS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "skins_custom")

SKIN_CATALOG = [
    {
        "id": "verdant-flat",
        "name": "青野平面",
        "description": "苔绿画布上的扁平自然系",
        "is_default": True,
        "modes": ["light", "dark"],
    },
    {
        "id": "ink-paper",
        "name": "墨韵纸间",
        "description": "宣纸暖底、朱砂点墨的文房气质",
        "is_default": False,
        "modes": ["light", "dark"],
    },
    {
        "id": "mono-brutal",
        "name": "黑白构成",
        "description": "高对比黑白构成，橙色锐利点缀",
        "is_default": False,
        "modes": ["light", "dark"],
    },
]

_SKIN_IDS = {s["id"] for s in SKIN_CATALOG}


def _user_skin_dir(user_id: int, skin_id: str) -> str:
    return os.path.join(CUSTOM_SKINS_DIR, str(user_id), skin_id)


def _assert_owned_skin(user: User, skin_id: str) -> str:
    """参数护栏（A4.9 Critical）：任意进入文件系统的 skin_id 必须先过
    id 正则 + realpath 容器校验——防止 `..` 类参数把 rmtree/读操作
    带出 backend/skins_custom/{user_id}/ 目录。返回该皮肤目录的 realpath。"""
    if not SKIN_ID_RE.match(skin_id):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="invalid skin id")
    base = os.path.realpath(os.path.join(CUSTOM_SKINS_DIR, str(user.id)))
    target = os.path.realpath(_user_skin_dir(user.id, skin_id))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="invalid skin id")
    return target


def _validate_skin_id(skin_id: str) -> None:
    if not SKIN_ID_RE.match(skin_id):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="invalid skin id (^[a-z0-9][a-z0-9-]{0,49}$，取 .css 文件名)")
    if skin_id in _SKIN_IDS:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"reserved builtin skin id: {skin_id}")


def _validate_skin_css(skin_id: str, data: bytes) -> str:
    if len(data) == 0:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="skin css required")
    if len(data) > MAX_SKIN_CSS_BYTES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"skin css too large: {len(data)} > {MAX_SKIN_CSS_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="skin css must be UTF-8 decodable")
    if f'[data-skin="{skin_id}"]' not in text:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"missing anchor: skin css must contain [data-skin=\"{skin_id}\"]")
    if text.count("{") != text.count("}"):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"unbalanced braces: {text.count('{')} open vs {text.count('}')} close")
    lower = text.lower()
    # '>' 是合法的子代选择器（内置皮肤大量使用）；'<' 在 CSS 中无合法用途 → 仅禁 '<'
    if "<" in text:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="forbidden in skin css: character '<'")
    if re.search(r"@import\s+[^;]*http", lower):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="forbidden in skin css: external @import")
    for pat in _FORBIDDEN_PATTERNS:
        if pat in lower:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=f"forbidden in skin css: {pat}")
    return text


def _load_user_skins(user_id: int) -> list:
    """扫描 per-user 目录读 manifest（损坏文件跳过，不破坏列表）。"""
    base = os.path.join(CUSTOM_SKINS_DIR, str(user_id))
    if not os.path.isdir(base):
        return []
    out = []
    for entry in sorted(os.listdir(base)):
        sdir = os.path.join(base, entry)
        manifest_path = os.path.join(sdir, "manifest.json")
        css_path = os.path.join(sdir, "skin.css")
        if not (os.path.isdir(sdir) and os.path.isfile(manifest_path) and os.path.isfile(css_path)):
            continue
        try:
            with open(manifest_path, "rb") as f:
                m = json.loads(f.read().decode("utf-8"))
            size = os.path.getsize(css_path)
            out.append({
                "id": m.get("id", entry),
                "name": m.get("name", entry),
                "description": m.get("description", ""),
                "size": size,
                "sha256": m.get("sha256", ""),
                "uploaded_at": m.get("uploaded_at", ""),
                "source": "uploaded",
                "is_default": False,
                "modes": ["light", "dark"],
            })
        except (ValueError, OSError):
            continue
    return out


def _user_known_skin_ids(user: User) -> set:
    return _SKIN_IDS | {s["id"] for s in _load_user_skins(user.id)}


@router.get("/api/skins")
async def list_skins():
    return {
        "token_contract_version": TOKEN_CONTRACT_VERSION,
        "default_skin": DEFAULT_SKIN_ID,
        "skins": SKIN_CATALOG,
    }


@router.post("/api/skins/upload", status_code=http_status.HTTP_201_CREATED)
async def upload_skin(
    file: UploadFile = File(...),
    name: str = Form(None),
    description: str = Form(None),
    user: User = Depends(get_current_user),
):
    filename = file.filename or ""
    if not filename.lower().endswith(".css"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="css file required (filename *.css)")
    skin_id = os.path.splitext(filename)[0]
    _validate_skin_id(skin_id)
    data = await file.read()
    _validate_skin_css(skin_id, data)

    sdir = _user_skin_dir(user.id, skin_id)
    os.makedirs(sdir, exist_ok=True)
    paths = [os.path.join(sdir, "skin.css"), os.path.join(sdir, "manifest.json")]
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    with open(paths[0], "wb") as f:
        f.write(data)
    entry = {
        "id": skin_id,
        "name": (name or skin_id).strip()[:50],
        "description": (description or "").strip()[:200],
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "uploaded",
        "is_default": False,
        "modes": ["light", "dark"],
    }
    with open(paths[1], "w", encoding="utf-8") as f:
        json.dump({**{k: entry[k] for k in ("id", "name", "description", "size", "sha256", "uploaded_at")},
                   "token_contract_version": TOKEN_CONTRACT_VERSION}, f, ensure_ascii=False, indent=1)
    return entry


@router.get("/api/skins/mine")
async def list_my_skins(user: User = Depends(get_current_user)):
    return {"skins": _load_user_skins(user.id)}


@router.get("/api/skins/{skin_id}/css", response_class=PlainTextResponse)
async def get_my_skin_css(skin_id: str, user: User = Depends(get_current_user)):
    if skin_id in _SKIN_IDS:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="skin not found")
    sdir = _assert_owned_skin(user, skin_id)
    css_path = os.path.join(sdir, "skin.css")
    if not os.path.isfile(css_path):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="skin not found")
    with open(css_path, "rb") as f:
        data = f.read()
    return PlainTextResponse(data.decode("utf-8"), media_type="text/css; charset=utf-8")


@router.delete("/api/skins/{skin_id}")
async def delete_my_skin(skin_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if skin_id in _SKIN_IDS:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="skin not found")
    sdir = _assert_owned_skin(user, skin_id)
    if not os.path.isdir(sdir):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="skin not found")
    shutil.rmtree(sdir)
    # 若该皮肤正是当前偏好 → 复位默认（保持 preferences 恒为合法值）
    current = _read_skin_preference(user)
    if current == skin_id:
        user.ui_preferences = json.dumps({"skin_id": DEFAULT_SKIN_ID}, ensure_ascii=False)
        db.add(user)
        await db.commit()
    return {"deleted": skin_id}


def _read_skin_preference(user: User) -> str:
    raw = getattr(user, "ui_preferences", None)
    if not raw:
        return DEFAULT_SKIN_ID
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return DEFAULT_SKIN_ID
    skin_id = data.get("skin_id") if isinstance(data, dict) else None
    return skin_id if skin_id in _user_known_skin_ids(user) else DEFAULT_SKIN_ID


@router.get("/api/users/me/preferences", response_model=UiPreferencesResponse)
async def get_ui_preferences(user: User = Depends(get_current_user)):
    return UiPreferencesResponse(skin_id=_read_skin_preference(user))


@router.put("/api/users/me/preferences", response_model=UiPreferencesResponse)
async def update_ui_preferences(
    payload: UiPreferencesUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.skin_id not in _user_known_skin_ids(user):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"unknown skin_id: {payload.skin_id}",
        )
    user.ui_preferences = json.dumps({"skin_id": payload.skin_id}, ensure_ascii=False)
    db.add(user)
    await db.commit()
    return UiPreferencesResponse(skin_id=payload.skin_id)


# 供未来其他模块复用（例如导出/个性化读取偏好）
async def get_user_skin_id(user: User, db: AsyncSession) -> str:
    """按主键重读用户并解析皮肤偏好（避免依赖过期 ORM 实例）。"""
    result = await db.execute(select(User).where(User.id == user.id))
    fresh = result.scalar_one_or_none()
    return _read_skin_preference(fresh or user)

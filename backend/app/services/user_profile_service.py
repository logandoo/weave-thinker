# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户资料服务 — 头像处理（2026-09-13）。

上传的头像字节经 Pillow 校验/重编码：EXIF 方向校正 → 居中裁剪 256×256
→ JPEG q85 → base64 data URL（存 users.avatar_data）。原始字节不落盘、
不直接存储（防伪装文件与超大图）。设计：design/FLOW_DESIGN_user_settings_20260913.html §3。
"""
import base64
import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2MB 上传上限
MAX_PIXELS = 25_000_000             # 解码像素上限（防 decompression bomb）
AVATAR_SIZE = 256
JPEG_QUALITY = 85
ALLOWED_FORMATS = {"PNG", "JPEG", "JPG", "WEBP", "GIF", "BMP"}


def process_avatar(data: bytes) -> str:
    """原始图片字节 → data:image/jpeg;base64,...（256×256）。非法输入抛 ValueError。"""
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)")

    try:
        img = Image.open(io.BytesIO(data))
        # A4.9 Important 修复（2026-09-13）：像素上限必须在 load()/解码之前检查——
        # 头部解析即可得尺寸，先解码再检查等于把内存炸弹放进进程（<2MB 的
        # 25M-178M 像素 PNG 可吃掉数百 MB）。
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("image dimensions too large")
        img.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("not a decodable image") from exc

    fmt = (img.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(f"unsupported image format: {fmt or 'unknown'}")

    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img = ImageOps.fit(
        img, (AVATAR_SIZE, AVATAR_SIZE),
        method=Image.Resampling.LANCZOS, centering=(0.5, 0.5),
    )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"

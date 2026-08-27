# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""PDF font configuration for WeasyPrint.

The backend ships required CJK fonts in backend/Fonts.  This module builds
@font-face CSS and a FontConfiguration instance so that WeasyPrint uses those
fonts first, with a graceful fallback to system fonts when a bundled font is
missing.
"""
import logging
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
FONTS_DIR = _BACKEND_ROOT / "Fonts"

# (family, filename, style, weight)
_BUNDLED_FONTS: List[Tuple[str, str, str, str]] = [
    ("Noto Sans CJK SC", "NotoSansCJKsc-Regular.otf", "normal", "400"),
    ("Noto Sans CJK SC", "NotoSansCJKsc-Bold.otf", "normal", "700"),
    ("Noto Sans Mono CJK SC", "NotoSansMonoCJKsc-Regular.otf", "normal", "400"),
]


def _build_font_face_css(font_config) -> str:
    """Return @font-face CSS for bundled fonts that actually exist."""
    declarations: List[str] = []
    for family, filename, style, weight in _BUNDLED_FONTS:
        path = FONTS_DIR / filename
        if path.exists():
            declarations.append(
                f"@font-face {{ font-family: '{family}'; src: url('{path.as_uri()}'); "
                f"font-style: {style}; font-weight: {weight}; }}"
            )
    return "\n".join(declarations)


def get_font_config_and_css():
    """Return a (FontConfiguration, CSS) tuple for WeasyPrint.

    Both objects must be passed to HTML.write_pdf/stylesheets so @font-face
    rules resolve correctly.
    """
    from weasyprint.text.fonts import FontConfiguration
    from weasyprint import CSS

    font_config = FontConfiguration()
    css_text = _build_font_face_css(font_config)
    if css_text:
        font_css = CSS(string=css_text, font_config=font_config)
    else:
        font_css = CSS(string="", font_config=font_config)
    return font_config, font_css


def ensure_fonts_dir() -> None:
    """Create the bundled fonts directory at runtime if it does not exist."""
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

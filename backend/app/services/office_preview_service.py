# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Office → PDF conversion service (2026-09-19).

Server-side rendering path for Office previews (reference: deepseek-harness
``dsh-office-to-pdf`` — LibreOffice + client PDF rendering). The Node kit is
not portable to this Python stack, so the equivalent is a headless
``soffice`` subprocess:

* content-version cache key ``sha256(path:mtime_ns:size:ext)`` — a changed
  source re-converts, repeated previews are served from disk;
* one conversion at a time (``Semaphore(1)``), per-run isolated
  ``UserInstallation`` profile so concurrent soffice instances cannot fight
  over the user profile lock;
* bounded cache (LRU by mtime) and hard timeout with process-group kill.

When no soffice binary is available the caller maps
:class:`OfficePreviewUnavailable`/``None`` to HTTP 501 and the frontend falls
back to its client-side renderers — office preview never becomes unavailable
just because the host lacks LibreOffice.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
import signal
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_OFFICE_EXT = frozenset({
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",
})

# Spreadsheet formats whose sheets may carry charts. LibreOffice's default
# paginated PDF export clips charts at page boundaries (user report
# 2026-09-19: sales_dashboard.xlsx lost chart categories / split the pie).
#
# Two mechanisms, chosen per format:
# * .xlsx → print-setup patch (fitToPage) in a spool copy + plain ``pdf`` —
#   works on every LibreOffice version (verified on prod LO 7.3, which
#   silently ignores the JSON filter options introduced in 7.4);
# * .xls / .ods → ``SinglePageSheets`` JSON filter (LO >= 7.4) with the
#   paginated fallback below for tall sheets / ignored options.
_CALC_SINGLE_PAGE_EXT = frozenset({".xls", ".ods"})
_XLSX_EXT = ".xlsx"

# Heuristic guard: a sheet taller than this many rows is exported paginated
# (fit-to-one-page would shrink it to unreadable scale).
_XLSX_FIT_MAX_ROWS = 200

# Bump whenever the conversion pipeline changes so stale cached PDFs are
# not served under an unchanged source (v4 = xlsx print-setup patch;
# v5 = patch hardening: paired pageSetUpPr, sheetProtection prefix,
# well-formedness validation).
_CACHE_VERSION = "5"

# Single-page mode flattens a whole sheet onto one page. Beyond this page
# height (PDF points; A4 is ~842pt) the page is unreadably large and can even
# exceed the browser's max canvas dimensions, so such workbooks fall back to
# the paginated export (charts may clip, tables stay readable) — A4.9 I-2.
_SINGLE_PAGE_MAX_PT = 4000
_MEDIABOX_RE = re.compile(rb"/MediaBox\s*\[([^\]]+)\]")

_COMMON_SOFFICE_PATHS = (
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

_SEM = None


class OfficePreviewError(RuntimeError):
    """Base error for office preview conversion."""


class OfficePreviewUnavailable(OfficePreviewError):
    """No usable soffice binary is available."""


class OfficePreviewFailed(OfficePreviewError):
    """soffice was found but the conversion failed or timed out."""


def _conversion_semaphore() -> "asyncio.Semaphore":  # noqa: F821 - lazy import
    global _SEM
    if _SEM is None:
        import asyncio

        _SEM = asyncio.Semaphore(1)
    return _SEM


def is_supported(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_OFFICE_EXT


def _find_soffice(preferred: str) -> str | None:
    if not preferred:
        preferred = "soffice"
    if os.sep in preferred or (os.altsep and os.altsep in preferred):
        if os.path.isfile(preferred) and os.access(preferred, os.X_OK):
            return preferred
        return None
    found = shutil.which(preferred)
    if found:
        return found
    if preferred == "soffice":
        for candidate in _COMMON_SOFFICE_PATHS:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


async def ensure_soffice(preferred: str = "soffice") -> str | None:
    """Return an executable soffice path, or ``None`` when unavailable."""
    import asyncio

    return await asyncio.to_thread(_find_soffice, preferred)


def _is_valid_pdf(path: Path) -> bool:
    """Cache entries are only trusted after a magic-byte check (A4.9 minor:
    a poisoned shared-temp cache entry must not be served as a preview)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _cache_key(source_id: str, mtime_ns: int, size: int, ext: str) -> str:
    raw = f"v{_CACHE_VERSION}:{source_id}:{mtime_ns}:{size}:{ext.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def convert_target(ext: str) -> str:
    """LibreOffice ``--convert-to`` argument for a source extension.

    Calc single-page export requires LibreOffice >= 7.4 for JSON filter
    options; older builds ignore them (conversion still succeeds, paginated).
    Hidden sheets are included in single-page mode by LibreOffice design —
    acceptable here (the workspace preview shows the whole workbook).
    """
    if ext.lower() in _CALC_SINGLE_PAGE_EXT:
        return 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
    return "pdf"


_SHEET_REF_RE = re.compile(rb'<dimension[^>]*ref="([^"]+)"[^>]*/?>')
_SHEET_ROW_RE = re.compile(rb'<row[^>]* r="(\d+)"')
_CELL_ROW_RE = re.compile(rb'([A-Za-z]{1,3})\$?(\d+)$')


def _sheet_max_row(xml: bytes) -> int:
    """Best-effort max row of a worksheet XML (0 when undeterminable)."""
    match = _SHEET_REF_RE.search(xml)
    if match:
        ref = match.group(1).decode("ascii", errors="ignore").replace("$", "").strip("'\"")
        last = ref.split(":")[-1]
        cell = _CELL_ROW_RE.search(last.encode("ascii", errors="ignore"))
        if cell:
            return int(cell.group(2))
    rows = _SHEET_ROW_RE.findall(xml)
    return max((int(r) for r in rows), default=0)


def _xlsx_needs_pagination(src: Path) -> bool:
    """True when any sheet exceeds the fit-to-one-page row guard."""
    try:
        with zipfile.ZipFile(src) as archive:
            for name in archive.namelist():
                if re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                    if _sheet_max_row(archive.read(name)) > _XLSX_FIT_MAX_ROWS:
                        return True
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError):
        return False
    return False


def _patch_xlsx_print_setup(src: Path, workdir: Path | None = None) -> Path:
    """Copy an .xlsx spool with every sheet set to fit-to-one-page printing.

    Pure zip rewrite — charts/drawings/styles are copied byte-for-byte (an
    openpyxl round-trip would drop charts). Returns the patched path, or
    *src* unchanged when the file is not a patchable zip or already fits.
    """
    patched = (workdir or src.parent) / (src.stem + ".wt_fit.xlsx")
    try:
        changed = False
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if re.match(r"xl/worksheets/sheet\d+\.xml$", item.filename):
                    xml = data.decode("utf-8")
                    new_xml = _patch_sheet_fit_to_page(xml)
                    if new_xml != xml:
                        # never accept a rewrite that isn't well-formed XML —
                        # a corrupt sheet is silently dropped by LibreOffice
                        # while the wrong PDF gets cached (A4.9 fix8 I-1)
                        try:
                            ET.fromstring(new_xml)
                        except ET.ParseError:
                            patched.unlink(missing_ok=True)
                            return src
                        changed = True
                    data = new_xml.encode("utf-8")
                zout.writestr(item, data)
        if not changed:
            patched.unlink(missing_ok=True)
            return src
        return patched
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError):
        try:
            patched.unlink(missing_ok=True)
        except OSError:
            pass
        return src


def _patch_sheet_fit_to_page(xml: str) -> str:
    fit = '<pageSetUpPr fitToPage="1"/>'
    if "<pageSetUpPr" in xml:
        # replace the WHOLE element (self-closing or paired) — a partial
        # replace would orphan the closing tag and corrupt the XML (A4.9
        # fix8 Important-1)
        xml = re.sub(
            r"<pageSetUpPr\b[^>]*?(?:/>|>.*?</pageSetUpPr>)",
            fit,
            xml,
            count=1,
            flags=re.DOTALL,
        )
    else:
        self_close = re.search(r"<sheetPr\s*/>", xml)
        open_tag = re.search(r"<sheetPr(?:\s[^>]*)?>", xml)
        if self_close:
            xml = xml.replace(self_close.group(0), f"<sheetPr>{fit}</sheetPr>", 1)
        elif open_tag:
            # pageSetUpPr is last in CT_SheetPr — insert before </sheetPr>
            if "</sheetPr>" in xml:
                xml = xml.replace("</sheetPr>", fit + "</sheetPr>", 1)
            else:
                xml = xml.replace(open_tag.group(0), open_tag.group(0) + fit, 1)
        else:
            # `<sheetProtection ...>` must not be mistaken for sheetPr
            # (A4.9 fix8 Important-2): anchor on the real opening tag only
            xml = re.sub(
                r"(<worksheet\b[^>]*>)",
                r"\1<sheetPr>" + fit + "</sheetPr>",
                xml,
                count=1,
            )
    if "<pageSetup" in xml:
        match = re.search(r"<pageSetup\b[^>]*?/?>", xml)
        tag = match.group(0)
        new_tag = tag
        if 'fitToWidth="' in new_tag:
            new_tag = re.sub(r'fitToWidth="[^"]*"', 'fitToWidth="1"', new_tag)
        else:
            new_tag = new_tag[:-2] + ' fitToWidth="1"/>' if new_tag.endswith("/>") else new_tag[:-1] + ' fitToWidth="1">'
        if 'fitToHeight="' in new_tag:
            new_tag = re.sub(r'fitToHeight="[^"]*"', 'fitToHeight="1"', new_tag)
        else:
            new_tag = new_tag[:-2] + ' fitToHeight="1"/>' if new_tag.endswith("/>") else new_tag[:-1] + ' fitToHeight="1">'
        xml = xml.replace(tag, new_tag, 1)
    else:
        xml = xml.replace(
            "</worksheet>",
            '<pageSetup fitToWidth="1" fitToHeight="1"/></worksheet>',
        )
    return xml


def _pdf_page_sizes(raw: bytes) -> list[tuple[float, float]]:
    """(width, height) per page from raw PDF bytes; [] when unparseable."""
    sizes: list[tuple[float, float]] = []
    for match in _MEDIABOX_RE.finditer(raw):
        try:
            nums = [float(x) for x in match.group(1).split()]
        except ValueError:
            continue
        if len(nums) == 4:
            sizes.append((abs(nums[2] - nums[0]), abs(nums[3] - nums[1])))
    return sizes


def _needs_paginated_fallback(pdf_path: Path) -> bool:
    try:
        raw = pdf_path.read_bytes()
    except OSError:
        return False
    sizes = _pdf_page_sizes(raw)
    return bool(sizes) and max(height for _w, height in sizes) > _SINGLE_PAGE_MAX_PT


def _prune_stale_spools(cache_dir: Path, max_age_seconds: int = 3600) -> None:
    """Remove abandoned spool copies (crash between spool and unlink)."""
    import time

    cutoff = time.time() - max_age_seconds
    try:
        candidates = list(cache_dir.glob("wt_office_src_*"))
    except OSError:
        return
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _prune_cache(cache_dir: Path, max_cache_mb: int) -> None:
    max_bytes = max(0, int(max_cache_mb)) * 1024 * 1024
    if max_bytes <= 0:
        return
    try:
        files = [p for p in cache_dir.glob("*.pdf") if p.is_file()]
    except OSError:
        return
    total = sum(p.stat().st_size for p in files if p.exists())
    if total <= max_bytes:
        return
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        if total <= max_bytes:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue


def _run_soffice(soffice: str, src: Path, dest: Path, timeout: int) -> None:
    """Blocking conversion; runs in a worker thread."""
    if not (os.path.isfile(soffice) and os.access(soffice, os.X_OK)):
        resolved = shutil.which(soffice)
        if not resolved:
            raise OfficePreviewUnavailable(f"soffice not found: {soffice}")
        soffice = resolved

    def _convert(profile: Path, target: str, source: Path | None = None) -> Path:
        """One soffice run into a private outdir; returns the produced PDF."""
        outdir = profile.parent / "out"
        outdir.mkdir(exist_ok=True)
        for leftover in outdir.glob("*.pdf"):
            leftover.unlink()
        cmd = [
            soffice,
            f"-env:UserInstallation=file://{profile}",
            "--headless",
            "--norestore",
            "--convert-to",
            target,
            "--outdir",
            str(outdir),
            str(source if source is not None else src),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _out, err = proc.communicate(timeout=max(5, int(timeout)))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise OfficePreviewFailed(f"conversion timed out after {timeout}s")
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", errors="replace")[:300]
            raise OfficePreviewFailed(f"soffice exited {proc.returncode}: {detail}")
        pdfs = sorted(outdir.glob("*.pdf"))
        if not pdfs:
            detail = (err or b"").decode("utf-8", errors="replace")[:300]
            raise OfficePreviewFailed(f"no PDF produced: {detail}")
        return pdfs[0]

    with tempfile.TemporaryDirectory(prefix="wt_office_preview_") as tmp:
        profile = Path(tmp) / "profile"
        convert_src = src
        target = convert_target(src.suffix)
        if src.suffix.lower() == _XLSX_EXT and not _xlsx_needs_pagination(src):
            # Version-independent single-sheet pages: patch print setup, then
            # plain PDF export (works on LibreOffice 7.3 too — prod box).
            convert_src = _patch_xlsx_print_setup(src, Path(tmp))
            if convert_src != src:
                target = "pdf"
        produced = _convert(profile, target, convert_src)

        # Single-page export can flatten a huge sheet into one unrenderably
        # tall page (browser canvas limits → silent blank preview). Fall back
        # to the paginated export for those (A4.9 I-2).
        if target != "pdf" and _needs_paginated_fallback(produced):
            produced = _convert(Path(tmp) / "profile2", "pdf", src)

        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = dest.with_name(dest.name + ".tmp")
        shutil.move(str(produced), str(staging))
        os.replace(staging, dest)


async def convert_to_pdf(
    src: str | Path,
    *,
    soffice: str,
    cache_dir: str | Path,
    timeout: int = 60,
    max_cache_mb: int = 512,
    cache_identity: str | None = None,
    cache_stat: tuple[int, int] | None = None,
) -> Path:
    """Convert *src* to a cached PDF and return the PDF path.

    ``cache_identity``/``cache_stat`` let callers key the cache on the
    *original workspace source* (path + fstat mtime/size) when *src* is a
    random-named spool copy — otherwise every request would miss the cache.
    Raises :class:`OfficePreviewUnavailable` when soffice is missing and
    :class:`OfficePreviewFailed` when conversion fails or times out.
    """
    import asyncio

    src_path = Path(src)
    if not is_supported(src_path.name):
        raise OfficePreviewFailed(f"unsupported office extension: {src_path.suffix}")
    try:
        stat_result = src_path.stat()
    except OSError as exc:
        raise OfficePreviewFailed(f"cannot stat source: {exc}") from exc
    if not src_path.is_file():
        raise OfficePreviewFailed("source is not a regular file")

    cache_path = Path(cache_dir)
    try:
        cache_path.mkdir(parents=True, exist_ok=True)
        os.chmod(cache_path, 0o700)
    except OSError:
        pass
    identity = cache_identity or str(src_path.resolve())
    mtime_ns, size = cache_stat if cache_stat else (stat_result.st_mtime_ns, stat_result.st_size)
    dest = cache_path / f"{_cache_key(identity, mtime_ns, size, src_path.suffix)}.pdf"
    _prune_stale_spools(cache_path)
    if _is_valid_pdf(dest):
        return dest
    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            pass

    async with _conversion_semaphore():
        if _is_valid_pdf(dest):
            return dest
        await asyncio.to_thread(_run_soffice, soffice, src_path, dest, timeout)
        await asyncio.to_thread(_prune_cache, cache_path, max_cache_mb)
        return dest

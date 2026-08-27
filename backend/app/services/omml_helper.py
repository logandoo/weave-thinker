# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""OMML helper injected by the code-execution sandbox.

Written to `.exec_tmp/omml_helper.py` inside the user workspace before each
execute_code run. The agent imports it instead of re-implementing the
LaTeX -> MathML -> OMML (Word equation editor) conversion, which guarantees
the official Microsoft MML2OMML.XSL path (merged runs, no m:box) is always
used and the latex2mathml flat-nary normalization is always applied.

Usage (see docx_manipulation skill):
    import sys, os
    sys.path.insert(0, os.path.join(os.getcwd(), ".exec_tmp"))
    from omml_helper import add_latex_equation
    p = doc.add_paragraph("公式：")
    add_latex_equation(p, r"\\int_0^1 x^2\\,dx = \\frac{1}{3}")
"""
import os

from lxml import etree
from latex2mathml.converter import convert as latex_to_mathml

_XSL_PATH = os.environ.get("WAVETHINKER_MML2OMML_XSL", "")
_NARY_CHARS = set("∏∐∑∩∪∫∬∭∮∯∰∱∲∳⋀⋁⋂⋃")
_SCRIPT_TAGS = {"msub", "msup", "msubsup", "munder", "mover", "munderover"}

# Microsoft official MML2OMML.XSL (the same converter Word uses when pasting
# MathML): sha256 5558e69d8bd6534927c4176bd5d5032d0d4bdc17bdaab7de580ca41e996a609b,
# 155527 bytes, mirrored at github.com/CBIhalsen/md2word. Compiled once per
# sandbox process; each formula conversion then reuses the transform.
_XSLT_TRANSFORM = None


def _get_xslt_transform():
    global _XSLT_TRANSFORM
    if _XSLT_TRANSFORM is None and _XSL_PATH and os.path.isfile(_XSL_PATH):
        _XSLT_TRANSFORM = etree.XSLT(etree.parse(_XSL_PATH))
    return _XSLT_TRANSFORM


def latex_to_omml_element(latex: str):
    """Convert a LaTeX formula to a Word equation-editor (OMML) element.

    Preferred path: official Microsoft MML2OMML.XSL (via the
    WAVETHINKER_MML2OMML_XSL env var injected by the sandbox) — Word-native
    output with merged runs and no m:box spacing artifacts.
    Fallback: mathml2omml (kept only in case the XSL file is unavailable).
    """
    mml = _normalize_nary_mathml(latex_to_mathml(latex))
    transform = _get_xslt_transform()
    if transform is not None:
        return transform(etree.fromstring(mml.encode("utf-8"))).getroot()
    print("LATEX2OMML_FALLBACK_MATHML2OMML: XSL unavailable, mathml2omml fallback (may show spacing artifacts in Word)")
    import mathml2omml
    from docx.oxml import parse_xml
    omml_str = mathml2omml.convert(mml)
    ns = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'
    return parse_xml(f'<p {ns}>{omml_str}</p>')[0]


def add_latex_equation(paragraph, latex: str) -> bool:
    """Insert a LaTeX formula as a Word-native (OMML) equation into *paragraph*.

    Returns False on conversion failure — the caller MUST report the failure
    to the user and must NOT silently fall back to plain text.
    """
    try:
        paragraph._p.append(latex_to_omml_element(latex))
        return True
    except Exception as e:
        print(f"LATEX2OMML_FAILED: {e}")
        return False


def _normalize_nary_mathml(mml: str) -> str:
    """latex2mathml flattens nary-operator bodies (\\sum etc.) into sibling
    nodes; the official MML2OMML.XSL only accepts an mrow as the nary body
    (otherwise it emits an empty <m:e> which Word renders as a blank box).
    Wrap the flat siblings after each nary script into an mrow.

    Recursive (not tree.iter) so newly inserted mrows are always revisited —
    adjacent nary pairs (\\sum\\sum, \\int\\int) need the first nary's body
    wrapped, which is only possible after the second nary has already been
    processed, hence a fixpoint loop."""
    tree = etree.fromstring(mml.encode("utf-8"))
    ns = tree.nsmap.get(None, "http://www.w3.org/1998/Math/MathML")

    def t(name):
        return f"{{{ns}}}{name}"

    def is_nary_script(el):
        kids = list(el)
        return (el.tag in {t(s) for s in _SCRIPT_TAGS} and kids
                and kids[0].tag == t("mo")
                and (kids[0].text or "").strip() in _NARY_CHARS)

    def wrap_row(row):
        changed = False
        children = list(row)
        i = 0
        while i < len(children):
            c = children[i]
            if is_nary_script(c):
                j = i + 1
                while j < len(children) and not is_nary_script(children[j]) \
                        and children[j].tag not in {t("mrow"), t("mstyle")}:
                    j += 1
                if j > i + 1:
                    body = etree.Element(t("mrow"))
                    for k in range(i + 1, j):
                        row.remove(children[k])
                        body.append(children[k])
                    row.insert(i + 1, body)
                    children = list(row)
                    changed = True
            i += 1
        return changed

    def walk(el):
        for child in list(el):
            walk(child)
        if el.tag == t("mrow"):
            while wrap_row(el):
                pass  # fixpoint: wrapping may expose further nary bodies

    walk(tree)
    return etree.tostring(tree, encoding="unicode")

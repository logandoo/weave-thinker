# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import re as _re


def _split_merged_list_items(line: str) -> list[str]:
    """Split lines where multiple list items are merged without line breaks.

    Handles patterns like:
      - item one- item two- item three
      * item one* item two
      - item one - item two (space before dash)
    """
    # Strategy 1: Split on " - " pattern (space-dash-space), which is always a new item
    parts = _re.split(r'(?<=\S) (\- )(?=[A-Z\u4e00-\u9fff])', line)
    if len(parts) > 1:
        result = []
        for i, part in enumerate(parts):
            if i == 0:
                result.append(part)
            else:
                if part.startswith('- '):
                    result.append('\n' + part)
                else:
                    result.append(part)
        line = ''.join(result)

    # Strategy 2: Split on "- " after bold close + CJK text
    # e.g. "**70%**，预计2026年下半年发布- AMD" -> split before "- AMD"
    line = _re.sub(
        r'(\*\*\s*)(\- )(?=[A-Z\u4e00-\u9fff])',
        r'\1\n\2',
        line,
    )

    # Strategy 3: Split on "- " after CJK closing bracket/punctuation
    # e.g. "发布- AMD" -> split
    # But NOT "PCIe6.0-宣称" (hyphenated compound, no CJK before dash)
    # And NOT "41%~64.8%-精度" (dash between non-CJK and CJK within same thought)
    # Key heuristic: CJK char followed by dash+space/CJK where dash starts a new idea
    line = _re.sub(
        r'([\u4e00-\u9fff\]\)）】])(\- )(?=[A-Z\u4e00-\u9fff])',
        r'\1\n\2',
        line,
    )

    # Strategy 4: Split when bold text is followed by a dash that starts new content
    # e.g. "**2.41x** [1]\n\n**2.**" patterns don't apply here
    # But "**70%**，预计...发布-宣称..." where - starts a clearly new bullet
    # Only split if there's significant CJK text between bold and the next dash
    line = _re.sub(
        r'(\*\*[^*]+\*\*[^\n*]{4,}?)(\- )(?=[A-Z\u4e00-\u9fff])',
        r'\1\n\2',
        line,
    )

    if '\n' in line:
        return line.split('\n')
    return [line]


def sanitize_markdown(text: str) -> str:
    if not text or not text.strip():
        return text

    lines = text.split("\n")
    result: list[str] = []
    prev_row_empty = False

    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if not stripped:
            result.append("")
            prev_row_empty = True
            continue

        # Fix: ###text -> ### text  (space after # marker)
        stripped = _re.sub(r"^(#{1,6})([^ \t#])", r"\1 \2", stripped)

        # Fix: -text -> - text  (space after list dash marker, but not --- horizontal rule)
        if _re.match(r"^-(?!--)([^ \t])", stripped):
            stripped = _re.sub(r"^-(?!\s|-)", "- ", stripped)

        # Fix: *text -> * text  (space after list asterisk marker, but not **bold**)
        if _re.match(r"^\*(?!\*)([^ \t*])", stripped):
            stripped = _re.sub(r"^\*(?!\s|\*)", "* ", stripped)

        # Fix: merged list items on same line
        if _re.match(r"^[-*]\s", stripped):
            sub_lines = _split_merged_list_items(stripped)
            if len(sub_lines) > 1:
                for sub in sub_lines:
                    sub = sub.strip()
                    if not sub:
                        continue
                    # Ensure blank line before heading if needed
                    if _re.match(r"^(#{1,6})\s", sub):
                        if result and not prev_row_empty:
                            result.append("")
                    result.append(sub)
                    prev_row_empty = False
                continue

        # Fix: text**bold_end_of_line** -> text\n\n**bold_end_of_line**
        stripped = _re.sub(
            r"(\S)(\*\*[^*]+?\*\*)$",
            lambda m: f"{m.group(1)}\n\n{m.group(2)}",
            stripped,
        )

        # Fix: consecutive ** ** without separator
        stripped = _re.sub(r"\*\*\s*\*\*(?=\S)", "**\n\n**", stripped)

        # Handle headings
        if _re.match(r"^(#{1,6})\s", stripped):
            # Fix: heading merged with sub-heading on same line
            # e.g. "## 一、产品线动态总览###1.第五代 EPYC9005..."
            sub_heading_match = _re.match(
                r'^(#{1,6}\s+\S.*?)(#{2,6}\s*\S.*)',
                stripped,
            )
            if sub_heading_match:
                main_heading = sub_heading_match.group(1).rstrip()
                sub_line = sub_heading_match.group(2)
                # Fix space after # in sub-heading: "###1." -> "### 1."
                sub_line = _re.sub(r'^(#{1,6})([^ \t#])', r'\1 \2', sub_line)
                # Also split body text after em-dash in sub-heading
                sub_dash_match = _re.match(
                    r'^(#{1,6}\s+\S[^—\n]{5,}?)(——|—)(\S.*)',
                    sub_line,
                )
                if sub_dash_match:
                    sub_heading_fixed = sub_dash_match.group(1).rstrip()
                    sub_body = sub_dash_match.group(3)
                    if result and not prev_row_empty:
                        result.append("")
                    result.append(main_heading)
                    result.append("")
                    result.append(sub_heading_fixed)
                    result.append("")
                    result.append(sub_body)
                    prev_row_empty = False
                    continue
                if result and not prev_row_empty:
                    result.append("")
                result.append(main_heading)
                result.append("")
                result.append(sub_line)
                prev_row_empty = False
                continue

            # Fix: heading merged with list item on same line
            # e.g. "## 三、市场与竞争态势- **市场份额**"
            heading_list_match = _re.match(
                r'^(#{1,6}\s+\S.*?)([-*]\s+\*\*\S.*)',
                stripped,
            )
            if heading_list_match:
                if result and not prev_row_empty:
                    result.append("")
                result.append(heading_list_match.group(1).rstrip())
                result.append("")
                result.append(heading_list_match.group(2))
                prev_row_empty = False
                continue

            # Fix: heading merged with body text after em-dash or ——
            # e.g. "### 2.第六代 EPYC "Venice"——下一代旗舰AMD在2026年CES上..."
            heading_body_match = _re.match(
                r'^(#{1,6}\s+\S[^—\n]{5,}?)(——|—)(\S.*)',
                stripped,
            )
            if heading_body_match:
                if result and not prev_row_empty:
                    result.append("")
                result.append(heading_body_match.group(1).rstrip())
                result.append("")
                result.append(heading_body_match.group(3))
                prev_row_empty = False
                continue

            # Detect heading merged with table: ### title|col1|col2|...
            pipe_idx = stripped.find("|")
            if pipe_idx > 0:
                after_pipe = stripped[pipe_idx:].strip()
                pipe_count = after_pipe.count("|")
                if pipe_count >= 2:
                    heading_part = stripped[:pipe_idx].rstrip()
                    if result and not prev_row_empty:
                        result.append("")
                    result.append(heading_part)
                    result.append("")
                    result.append(after_pipe)
                    prev_row_empty = False
                    continue

            # Fix: ### heading_text**bold... -> separate
            stripped = _re.sub(
                r"^(#{1,6}\s[^#*]*?[^*\s])(\*\*[^*\s])",
                r"\1\n\n\2",
                stripped,
            )
            # Fix: ### heading_text> blockquote -> separate
            stripped = _re.sub(
                r"^(#{1,6}\s[^#>\n]*?[^>\s])(>[>\s]*\S)",
                r"\1\n\n\2",
                stripped,
            )
            # Fix: ### heading followed immediately by body text on same line
            # e.g. "## AMD EPYC处理器近一周信息汇总报告报告文件已保存为："
            # Only split when there's a clear sentence boundary marker (：:)
            # after a reasonably long heading (15+ chars)
            if len(stripped) > 20 and _re.search(r'[：:]', stripped):
                split_match = _re.match(
                    r'^(#{1,6}\s+[^\n]{10,}?)[：:]\s*([^\n]+)$',
                    stripped,
                )
                if split_match:
                    heading_part = split_match.group(1).rstrip()
                    body_part = split_match.group(2).lstrip()
                    if body_part:
                        if result and not prev_row_empty:
                            result.append("")
                        result.append(heading_part)
                        result.append("")
                        result.append(body_part)
                        prev_row_empty = False
                        continue

            if result and not prev_row_empty:
                result.append("")
            result.append(stripped)
            prev_row_empty = False
            continue

        # Handle table rows
        is_table_sep = bool(_re.match(r"^\|[\s\-:|]+\|$", stripped))
        is_table_row = bool(_re.match(r"^\|", stripped))

        if is_table_row:
            prev_line_is_table = (
                bool(result and _re.match(r"^\|", result[-1]))
                if result
                else False
            )
            if not is_table_sep and not prev_line_is_table and result and result[-1] != "":
                result.append("")
            result.append(stripped)
            prev_row_empty = False
            continue

        result.append(stripped)
        prev_row_empty = False

    return "\n".join(result)

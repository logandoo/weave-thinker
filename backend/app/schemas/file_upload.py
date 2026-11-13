# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel


class FileParseResult(BaseModel):
    success: bool
    markdown: str | None = None
    error: str | None = None
    file_type: str | None = None
    filename: str = ""
    file_path: str | None = None
    rel_path: str | None = None
    # 任务作用域（§10）：Task 文件域内 agent 可直接解析的路径
    # （staging 附件为交接后的 inputs/<name>）；legacy 上传为 None。
    display_path: str | None = None
    size: int = 0


class FileUploadResponse(BaseModel):
    results: list[FileParseResult]
    notebook_id: str | None = None
    notebook_name: str | None = None

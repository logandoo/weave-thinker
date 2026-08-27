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
    size: int = 0


class FileUploadResponse(BaseModel):
    results: list[FileParseResult]
    notebook_id: str | None = None
    notebook_name: str | None = None

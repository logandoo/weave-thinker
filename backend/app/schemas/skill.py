# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel
from typing import Optional, List


class SkillBase(BaseModel):
    name: str
    description: Optional[str] = None
    content: str
    is_active: bool = True


class SkillCreate(SkillBase):
    pass


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    is_active: Optional[bool] = None


class SkillResponse(SkillBase):
    id: str
    user_id: str = ""
    source: str = "user"
    category: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    class Config:
        from_attributes = True


class SkillFileResponse(BaseModel):
    id: str
    skill_id: str
    file_path: str
    file_type: str
    is_executable: bool
    created_at: str

    class Config:
        from_attributes = True


class ExecutableWarning(BaseModel):
    file_path: str
    file_type: str
    is_dangerous: bool
    warning_message: str


class SkillUploadWarning(BaseModel):
    filename: str
    has_executables: bool
    executable_count: int
    warnings: List[ExecutableWarning]
    dangerous_count: int

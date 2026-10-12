# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from typing import Optional

from pydantic import BaseModel


class UiPreferencesResponse(BaseModel):
    skin_id: str
    # W18 G7：执行环境默认值（None=云端；device:<id>=客户端设备）。
    # 合并语义：PUT 只覆盖提交的键，不整对象覆写。
    default_execution_target: Optional[str] = None


class UiPreferencesUpdate(BaseModel):
    # W18 G7：全部可选——只覆盖提交的键（skin_id=None 表示本次不改皮肤）
    skin_id: Optional[str] = None
    default_execution_target: Optional[str] = None

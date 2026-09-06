# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel, field_validator


def _validate_alias(value: str, *, allow_empty: bool) -> str:
    """model_alias 合法性：必须存在于 model_gateway 注册表（未知别名 422）。
    Internal purpose aliases (vlm 等) are infrastructure endpoints, not
    chat models — rejected for assistants (A4.9 W2-M3: hiding them from
    /api/models is dropdown-deep only)."""
    if not value:
        if allow_empty:
            return value
        raise ValueError("model_alias 不能为空（合法取值见 GET /api/models）")
    from app.model_gateway.registry import get_model_registry, _INTERNAL_PURPOSE_ALIASES
    if value in _INTERNAL_PURPOSE_ALIASES or value.startswith("deathmatch."):
        raise ValueError(f"model alias {value!r} 是内部用途端点，不能作为助手聊天模型")
    if not get_model_registry().has(value):
        raise ValueError(f"unknown model alias: {value!r}（合法取值见 GET /api/models）")
    return value


class AssistantBase(BaseModel):
    name: str
    system_prompt: str = ""
    # 模型选择 = 逻辑别名（模型配置解耦 2026-08-30：供应商 url/key/模型名/参数
    # 统一由后端 config_model.toml 管理，不再经助手接口暴露）。
    model_alias: str = "deepseek"
    subtask_model_alias: str = ""  # "" = 跟随主模型

    @field_validator("model_alias")
    @classmethod
    def _check_model_alias(cls, v: str) -> str:
        # 基类宽容（Response 继承后需如实序列化 legacy NULL 行为 ""）；
        # Create 路径的严格性由 AssistantCreate 的覆写校验保证。
        return _validate_alias(v, allow_empty=True)

    @field_validator("subtask_model_alias")
    @classmethod
    def _check_subtask_alias(cls, v: str) -> str:
        return _validate_alias(v, allow_empty=True)


class AssistantCreate(AssistantBase):
    @field_validator("model_alias")
    @classmethod
    def _check_create_alias(cls, v: str) -> str:
        # 空 = 未选择（存 NULL，服务端按 default/legacy 解析）；未知别名仍 422。
        # 前端不持有任何供应商缺省名——默认值归一在服务端（A4.9 wave-6 统一性）。
        return _validate_alias(v, allow_empty=True)


class AssistantUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    model_alias: str | None = None
    subtask_model_alias: str | None = None

    @field_validator("model_alias")
    @classmethod
    def _check_model_alias(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # "" 允许：清除别名（回退 legacy/默认解析，存 NULL）
        return _validate_alias(v, allow_empty=True)

    @field_validator("subtask_model_alias")
    @classmethod
    def _check_subtask_alias(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_alias(v, allow_empty=True)


class AssistantResponse(AssistantBase):
    # legacy 行（旧 custom 配置，无别名）如实报告为 ""，不冒名默认别名
    # （A4.9 复审 Important-4：否则首次保存会静默覆盖旧 custom 端点）。
    model_alias: str = ""
    id: str
    user_id: str
    created_at: str
    updated_at: str

    @field_validator("model_alias")
    @classmethod
    def _check_model_alias(cls, v: str) -> str:
        # A4.9 R3 Minor③修复：与基类同名覆写以完全取代继承校验——Response
        # 只做宽容序列化：DB 里的陈旧别名（注册表已删）原样报告，列表端点
        # 绝不因脏数据 500；严格性由 Create/Update 承担，运行期解析
        # （endpoint_for_assistant）对未知别名自带 main 回落。
        return v or ""

    @field_validator("subtask_model_alias")
    @classmethod
    def _check_subtask_alias(cls, v: str) -> str:
        # 同上（A4.9 wave-3 复审 Important）：subtask 陈旧别名同样宽容序列化。
        return v or ""

    class Config:
        from_attributes = True

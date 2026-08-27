# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel


class UiPreferencesResponse(BaseModel):
    skin_id: str


class UiPreferencesUpdate(BaseModel):
    skin_id: str

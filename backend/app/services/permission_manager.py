# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# MULTI-WORKER NOTE: _permission_requests is a process-local dictionary.
# In multi-worker deployments, approve requests to one worker fail on
# another worker (request_id lookup returns None, times out silently after 120s).
# For multi-worker, migrate permission state to Redis or the database.

@dataclass
class PermissionRequest:
    request_id: str
    conversation_id: str
    tool_name: str
    description: str
    details: dict = field(default_factory=dict)
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _response: Optional[bool] = None

    async def wait(self, timeout: float = 120.0) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return self._response is True
        except asyncio.TimeoutError:
            return False

    def respond(self, approved: bool):
        self._response = approved
        self._event.set()


_permission_requests: Dict[str, PermissionRequest] = {}


async def request_permission(
    conversation_id: str,
    tool_name: str,
    description: str,
    details: Optional[dict] = None,
    timeout: float = 120.0,
    request_id: Optional[str] = None,
) -> bool:
    req_id = request_id or uuid.uuid4().hex[:12]
    req = PermissionRequest(
        request_id=req_id,
        conversation_id=conversation_id,
        tool_name=tool_name,
        description=description,
        details=details or {},
    )
    _permission_requests[req_id] = req
    try:
        return await req.wait(timeout=timeout)
    finally:
        _permission_requests.pop(req_id, None)


def get_pending_request(request_id: str) -> Optional[PermissionRequest]:
    return _permission_requests.get(request_id)


def respond_to_request(request_id: str, approved: bool) -> bool:
    req = _permission_requests.get(request_id)
    if req is None:
        return False
    req.respond(approved)
    return True


def get_pending_requests_for_conversation(conversation_id: str) -> list:
    return [
        r for r in _permission_requests.values()
        if r.conversation_id == conversation_id
    ]

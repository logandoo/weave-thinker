# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

from app.tools.registry import registry

logger = logging.getLogger(__name__)

_BRIDGE_TEMPLATE = '''\
import json as _json
import socket as _socket
import os as _os


class _ToolsProxy:
    def __init__(self, socket_path):
        self._socket_path = socket_path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(**kwargs):
            req = _json.dumps({{"tool": name, "args": kwargs}})
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(120)
            try:
                sock.connect(self._socket_path)
                sock.sendall(req.encode("utf-8") + b"\\n")
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                resp = _json.loads(data.decode("utf-8"))
                if "error" in resp and "result" not in resp:
                    raise RuntimeError(f"Tool '{{name}}' failed: {{resp['error']}}")
                result_str = resp.get("result", "")
                try:
                    return _json.loads(result_str) if isinstance(result_str, str) else result_str
                except (_json.JSONDecodeError, TypeError):
                    return result_str
            finally:
                sock.close()

        return _call


tools = _ToolsProxy(_os.environ.get("PTC_SOCKET_PATH", ""))
'''


class PTCBridge:
    def __init__(
        self,
        allowed_tools: List[str],
        sandbox_dir: str,
        dispatch_kwargs: Optional[Dict[str, Any]] = None,
    ):
        self.allowed_tools: Set[str] = set(allowed_tools)
        self.sandbox_dir = sandbox_dir
        self.socket_path = os.path.join(sandbox_dir, ".ptc.sock")
        self.bridge_module_path = os.path.join(sandbox_dir, "_ptc_bridge.py")
        self._dispatch_kwargs = dispatch_kwargs or {}
        self._server: Optional[asyncio.Server] = None
        self._call_count = 0

    async def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=self.socket_path,
        )
        self._write_bridge_module()
        logger.debug("PTC bridge started on %s", self.socket_path)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            ptc_timeout = config.agent_ptc.get("socket_timeout_seconds", 120.0)
            data = await asyncio.wait_for(reader.read(2_000_000), timeout=ptc_timeout)
            if not data:
                return

            request = json.loads(data.decode("utf-8"))
            tool_name = request.get("tool", "")
            tool_args = request.get("args", {})

            if tool_name not in self.allowed_tools:
                response = json.dumps({"error": f"Tool '{tool_name}' not in PTC allowed list"})
            else:
                try:
                    self._call_count += 1
                    result_str = await registry.dispatch(
                        tool_name, tool_args, **self._dispatch_kwargs,
                    )
                    response = json.dumps({"result": result_str})
                except Exception as e:
                    logger.exception("PTC dispatch failed for %s", tool_name)
                    response = json.dumps({"error": str(e)})

            writer.write(response.encode("utf-8"))
            await writer.drain()
        except asyncio.TimeoutError:
            logger.warning("PTC connection timed out")
            try:
                writer.write(json.dumps({"error": "Connection timed out"}).encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
        except Exception as e:
            logger.exception("PTC bridge connection error")
            try:
                writer.write(json.dumps({"error": str(e)}).encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _write_bridge_module(self):
        with open(self.bridge_module_path, "w", encoding="utf-8") as f:
            f.write(_BRIDGE_TEMPLATE)

    @property
    def extra_env(self) -> Dict[str, str]:
        return {"PTC_SOCKET_PATH": self.socket_path}

    @property
    def call_count(self) -> int:
        return self._call_count

    async def stop(self):
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for path in (self.socket_path, self.bridge_module_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        logger.debug("PTC bridge stopped (total calls: %d)", self._call_count)

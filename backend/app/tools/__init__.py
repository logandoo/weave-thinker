# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from app.tools.registry import ToolRegistry, ToolEntry, tool_error, registry as _registry

_imported = False


def _discover_tools():
    global _imported
    if _imported:
        return
    _imported = True

    import importlib
    import logging
    from pathlib import Path

    logger = logging.getLogger(__name__)
    tools_dir = Path(__file__).resolve().parent

    for path in sorted(tools_dir.glob("*.py")):
        if path.name in ("__init__.py", "registry.py"):
            continue
        mod_name = f"app.tools.{path.stem}"
        try:
            importlib.import_module(mod_name)
            logger.debug("Discovered tools from %s", mod_name)
        except Exception as e:
            logger.warning("Could not import tool module %s: %s", mod_name, e)


_discover_tools()

# Load MCP servers from config
try:
    from app.tools.mcp_client import load_mcp_servers_from_config
    load_mcp_servers_from_config()
except Exception:
    pass

registry = _registry
__all__ = ["registry", "ToolRegistry", "ToolEntry", "tool_error"]

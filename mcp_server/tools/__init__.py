"""MCP tool package.

tools.toml is the single source of truth: each [[tool]] entry carries the
tool's metadata and the dotted path of its handler function. Importing this
package resolves every handler — a bad tool name or handler path fails the
import, so a broken definition can't reach a running server.
"""

import importlib
import tomllib
from pathlib import Path
from typing import Any

from mcp.types import Tool, ToolAnnotations

from .common import ToolHandler

_TOOLS_TOML = Path(__file__).resolve().parent / "tools.toml"

with open(_TOOLS_TOML, "rb") as f:
    _specs: list[dict[str, Any]] = tomllib.load(f)["tool"]


def _resolve_handler(dotted: str):
    module_name, _, func_name = dotted.rpartition(".")
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, func_name)


TOOL_HANDLERS: dict[str, ToolHandler] = {}

for _spec in _specs:
    TOOL_HANDLERS[_spec["name"]] = _resolve_handler(_spec["handler"])


def load_tools() -> list[Tool]:
    """Build Tool objects from the definitions in tools.toml."""
    return [
        Tool(
            name=spec["name"],
            description=spec["description"],
            inputSchema=spec["input_schema"],
            annotations=ToolAnnotations(**spec["annotations"]) if "annotations" in spec else None,
        )
        for spec in _specs
    ]
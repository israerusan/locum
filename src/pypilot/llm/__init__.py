"""First-class LLM control of the desktop.

* :mod:`~pypilot.llm.observe` -- set-of-marks observations (numbered elements + annotated screenshot).
* :mod:`~pypilot.llm.tools` -- schema-generating :class:`ToolRegistry` and the :class:`DesktopToolkit`.
* :mod:`~pypilot.llm.computer_use` -- executes Anthropic computer-use toolset actions.
* :mod:`~pypilot.llm.agent` -- the :class:`Agent` loop (Claude Messages API, or any compatible client).
* :mod:`~pypilot.llm.safety` -- :class:`Policy` guardrails, confirmations, dry-run and :class:`AuditLog`.
* :mod:`~pypilot.llm.mcp_server` -- a dependency-free MCP server over stdio.

Only pillow/numpy are needed to import this package; the ``anthropic`` SDK is
imported lazily when an :class:`Agent` creates its default client.
"""

from __future__ import annotations

from .agent import (
    Agent,
    AgentResult,
    AgentStep,
    LLMClient,
    ScriptedClient,
    computer_block,
    scripted_response,
    text_block,
    thinking_block,
    tool_use_block,
)
from .computer_use import ComputerUseAdapter, map_key, parse_key_combo
from .mcp_server import MCPServer, serve
from .observe import Observation, ObservedElement, fit_image_size, observe
from .safety import CARD_NUMBER, DEFAULT_CONFIRM, DEFAULT_CONFIRM_KEYS, AuditLog, Policy, console_confirm
from .tools import DesktopToolkit, ToolRegistry, ToolResult, ToolSpec

__all__ = [
    "Agent",
    "AgentResult",
    "AgentStep",
    "AuditLog",
    "CARD_NUMBER",
    "ComputerUseAdapter",
    "DEFAULT_CONFIRM",
    "DEFAULT_CONFIRM_KEYS",
    "DesktopToolkit",
    "LLMClient",
    "MCPServer",
    "Observation",
    "ObservedElement",
    "Policy",
    "ScriptedClient",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "computer_block",
    "console_confirm",
    "fit_image_size",
    "map_key",
    "observe",
    "parse_key_combo",
    "scripted_response",
    "serve",
    "text_block",
    "thinking_block",
    "tool_use_block",
]

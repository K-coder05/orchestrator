"""Agent adapter registry. Adding an agent means registering a factory here;
core code never names a specific agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import AgentAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter

AdapterFactory = Callable[[dict[str, Any]], AgentAdapter]

REGISTRY: dict[str, AdapterFactory] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
}


def get_adapter(name: str, settings: dict[str, Any] | None = None) -> AgentAdapter:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise ValueError(f"no adapter for agent {name!r}; known: {sorted(REGISTRY)}") from None
    return factory(settings or {})

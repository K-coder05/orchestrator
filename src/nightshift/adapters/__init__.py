"""Agent adapter registry. Adding an agent means registering a factory here;
core code never names a specific agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import AgentAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter, GeminiApiAdapter

AdapterFactory = Callable[[dict[str, Any]], AgentAdapter]

REGISTRY: dict[str, AdapterFactory] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "gemini-api": GeminiApiAdapter,
}


def is_paid(name: str) -> bool:
    """Whether agent ``name`` is a paid path (§1.5): spend, not usage Windows."""
    return bool(getattr(REGISTRY.get(name), "paid", False))


def paid_path(agent: str) -> str | None:
    """The paid path that stands in for subscription agent ``agent`` (§1.5)."""
    return next((name for name, f in REGISTRY.items()
                 if getattr(f, "paid", False) and getattr(f, "subscription", None) == agent),
                None)


def lane_of(name: str) -> str:
    """The queue runner lane agent ``name`` runs in (§5 item 5): its
    adapter's ``lane``, else a lane of its own."""
    return getattr(REGISTRY.get(name), "lane", None) or name


def load_api_key(adapter: AgentAdapter, secrets: Any, *, required: bool = False) -> None:
    """Give ``adapter`` the API key it signs in with (the secret named by its
    ``api_key_secret``, if any); loading it registers it for redaction. With
    ``required`` a missing key raises ``ConfigError``; otherwise the adapter
    refuses to build a command without it."""
    if adapter.api_key_secret:
        adapter.api_key = (secrets.require(adapter.api_key_secret) if required
                           else secrets.get(adapter.api_key_secret))


def get_adapter(name: str, settings: dict[str, Any] | None = None) -> AgentAdapter:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise ValueError(f"no adapter for agent {name!r}; known: {sorted(REGISTRY)}") from None
    return factory(settings or {})

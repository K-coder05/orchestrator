"""The host seam (ADR 0001).

Every OS-specific call goes through a ``Host``. ``MacHost`` is the only real
implementation; ``FakeHost`` keeps core logic testable on any OS.
"""

from __future__ import annotations

import subprocess
from typing import Protocol


class Host(Protocol):
    def keychain_get(self, service: str, account: str) -> str | None:
        """Return a generic-password secret from the Keychain, or None."""
        ...


class MacHost:
    """Real macOS host. Uses the ``security`` CLI for Keychain reads."""

    def keychain_get(self, service: str, account: str) -> str | None:
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        value = proc.stdout.rstrip("\n")
        return value or None


class FakeHost:
    """In-memory host for tests."""

    def __init__(self, keychain: dict[tuple[str, str], str] | None = None):
        self.keychain = dict(keychain or {})
        self.keychain_reads: list[tuple[str, str]] = []

    def keychain_get(self, service: str, account: str) -> str | None:
        self.keychain_reads.append((service, account))
        return self.keychain.get((service, account))

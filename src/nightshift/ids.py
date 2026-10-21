"""Task IDs and slugs (§1.3)."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Container

BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_LENGTH = 4


def random_id(length: int = ID_LENGTH) -> str:
    return "".join(secrets.choice(BASE36) for _ in range(length))


def new_task_id(
    taken: Container[str] | Callable[[str], bool],
    *,
    generator: Callable[[], str] = random_id,
    max_tries: int = 1000,
) -> str:
    """Return a 4-char lowercase base36 id not in ``taken``.

    ``taken`` is either a container of used ids or a predicate returning True
    when an id is already used.
    """
    is_taken = taken if callable(taken) else (lambda i: i in taken)
    for _ in range(max_tries):
        candidate = generator()
        if not is_valid_id(candidate):
            raise ValueError(f"generator produced an invalid id: {candidate!r}")
        if not is_taken(candidate):
            return candidate
    raise RuntimeError("could not find a free Task id")


_ID_RE = re.compile(r"^[0-9a-z]{4}$")


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value))


def slugify(text: str, *, max_words: int = 6, max_length: int = 40) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:max_words]
    slug = "-".join(words)
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
        # Avoid cutting a word in half when a hyphen is reasonably close.
        if "-" in slug and len(slug) - slug.rfind("-") < 8:
            slug = slug[: slug.rfind("-")]
    return slug or "task"


def title_from_prompt(prompt: str, max_length: int = 80) -> str:
    line = " ".join(prompt.split())
    if len(line) <= max_length:
        return line
    return line[: max_length - 1].rstrip() + "…"

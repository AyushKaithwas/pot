"""The extension point.

A skill is a module in `pot.skills` that exports a module-level `SKILL`. Adding a capability
means adding one file: the registry finds it, its tools land in the realtime session, and its
prompt fragment is appended to the persona. Nothing else needs editing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from agents import Tool

from pot.config import Settings
from pot.runtime import Runtime


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: Sequence[Tool] = field(default_factory=tuple)
    prompt: str = ""
    setup: Callable[[Settings], None] | None = None
    # Background work that should live as long as the voice session: a subprocess, a listener.
    # Anything the skill parks on runtime.resource is closed with the runtime.
    start: Callable[[Runtime], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a skill needs a name")

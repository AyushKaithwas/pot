"""Shared services that skill tools reach for.

Tools are plain module-level functions, so they get their dependencies from here rather than
through arguments. Skills that need something long-lived (a device connection pool, an HTTP
client) park it in `resource`, so core never has to know what skills exist; anything with an
async `close` is shut down with the runtime. `announce` is how a background job (a fired timer)
gets Pot to speak: the voice session installs a callback, and the CLI just prints.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pot.config import Settings
from pot.config import settings as load_cached_settings
from pot.console import console
from pot.scheduler import Scheduler
from pot.usage import UsageMeter

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class Runtime:
    settings: Settings
    scheduler: Scheduler = field(default_factory=Scheduler)
    _resources: dict[str, Any] = field(default_factory=dict, repr=False)
    _announce: Callable[[str], None] | None = None
    _usage: UsageMeter | None = field(default=None, repr=False)
    _speech_listeners: list[Callable[[bool], None]] = field(default_factory=list, repr=False)
    _speaking: bool = False

    @property
    def usage(self) -> UsageMeter:
        """Running token and cost tally, shared by the voice session and any skill that spends."""
        if self._usage is None:
            self._usage = UsageMeter(self.settings.costs.models)
        return self._usage

    def resource(self, key: str, factory: Callable[[Runtime], T]) -> T:
        """Get or create a long-lived resource owned by this runtime."""
        if key not in self._resources:
            self._resources[key] = factory(self)
        return self._resources[key]

    def on_speech(self, callback: Callable[[bool], None]) -> None:
        """Subscribe to "Pot started/stopped talking".

        The music skill uses this to duck the volume. The voice session publishes it without
        knowing who is listening, so core never has to import a skill.
        """
        self._speech_listeners.append(callback)

    def set_speaking(self, speaking: bool) -> None:
        if speaking == self._speaking:
            return
        self._speaking = speaking
        for listener in list(self._speech_listeners):
            try:
                listener(speaking)
            except Exception:
                log.debug("a speech listener failed", exc_info=True)

    @property
    def speaking(self) -> bool:
        return self._speaking

    def on_announce(self, callback: Callable[[str], None] | None) -> None:
        self._announce = callback

    def announce(self, text: str) -> None:
        """Say something the user did not directly ask for, e.g. a timer going off."""
        if self._announce is None:
            console.print(f"[magenta]pot[/magenta] {text}")
            return
        self._announce(text)

    async def close(self) -> None:
        self.scheduler.cancel_all()
        for key, value in list(self._resources.items()):
            close = getattr(value, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                log.debug("closing resource %s failed", key, exc_info=True)
        self._resources.clear()


_runtime: Runtime | None = None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(load_cached_settings())
    return _runtime


def set_runtime(value: Runtime | None) -> None:
    global _runtime
    _runtime = value

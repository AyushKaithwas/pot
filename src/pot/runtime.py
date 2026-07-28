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
    _listen_listeners: list[Callable[[bool], None]] = field(default_factory=list, repr=False)
    _ring_listeners: list[Callable[[bool], None]] = field(default_factory=list, repr=False)
    # Set by a skill that has just started something long-running, so the session stops listening
    # after this reply rather than waiting out the idle timeout.
    _end_conversation: bool = False
    _speaking: bool = False
    _listening: bool = False
    _ringing: bool = False
    # The session owns the speaker, so it parks these here for skills that need them: the time
    # skill rings through it, and music is mixed through it so Pot can hear over its own playback.
    ringer: Any | None = None
    speaker: Any | None = None

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

    def on_listen(self, callback: Callable[[bool], None]) -> None:
        """Subscribe to "Pot started/stopped listening for a request".

        Music ducks for this too: a speaker at full volume is the main reason a wake word gets
        heard but the sentence after it doesn't.
        """
        self._listen_listeners.append(callback)

    def on_ring(self, callback: Callable[[bool], None]) -> None:
        """Subscribe to "an alarm is ringing". Music ducks for this too."""
        self._ring_listeners.append(callback)

    def end_conversation(self) -> None:
        """Ask for the exchange to end once Pot has finished this reply.

        For skills that leave something running. Starting a track is usually the end of what the
        user wanted, so holding the mic open afterwards streams the room to a paid API for no
        reason. Say the wake word again, the way an Echo works.
        """
        self._end_conversation = True

    def take_conversation_end(self) -> bool:
        """Whether the conversation was asked to end, clearing the request."""
        ended, self._end_conversation = self._end_conversation, False
        return ended

    def set_speaking(self, speaking: bool) -> None:
        self._publish(self._speech_listeners, "_speaking", speaking)

    def set_ringing(self, ringing: bool) -> None:
        self._publish(self._ring_listeners, "_ringing", ringing)

    def set_listening(self, listening: bool) -> None:
        self._publish(self._listen_listeners, "_listening", listening)

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def alarm_ringing(self) -> bool:
        return self._ringing

    def _publish(self, listeners: list[Callable[[bool], None]], attribute: str, value: bool) -> None:
        if getattr(self, attribute) == value:
            return
        setattr(self, attribute, value)
        for listener in list(listeners):
            try:
                listener(value)
            except Exception:
                log.debug("a %s listener failed", attribute.lstrip("_"), exc_info=True)

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

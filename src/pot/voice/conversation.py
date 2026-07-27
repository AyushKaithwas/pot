"""When a wake-word conversation is over.

After the wake word fires, the mic stays open so follow-ups don't need the word again. Something
has to decide when the exchange has ended, or the device streams the room to a paid API until it
reboots.

This is done locally on purpose. The Realtime API looks like it offers exactly this through
turn_detection.idle_timeout_ms, but it rejects the parameter as unknown, and because a rejected
session update discards the whole configuration, the failure shows up as a model with no tools
rather than as an error about a timeout.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class ConversationWindow:
    """A deadline that anything active pushes back."""

    def __init__(self, seconds: float, *, clock=time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._deadline: float | None = None

    @property
    def open(self) -> bool:
        return self._deadline is not None

    def start(self) -> None:
        self._deadline = self._clock() + self.seconds

    def touch(self) -> None:
        """Someone said something, Pot answered, or a tool ran: not idle after all."""
        if self._deadline is not None:
            self._deadline = self._clock() + self.seconds

    def stop(self) -> None:
        self._deadline = None

    def expired(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    def remaining(self) -> float:
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - self._clock())

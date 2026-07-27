"""Watching the clock, ringing, and putting music back afterwards.

The store owns what's pending and the ringer owns the noise; this owns the deciding. It ticks every
second rather than sleeping until the next alarm, because a device suspends, the clock jumps, and a
sleeping task can't notice either.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from pot.alarms import Alarm, AlarmStore, Kind, Repeat, next_datetime
from pot.audio.ring import Ringer
from pot.config import Settings, state_dir
from pot.runtime import Runtime, runtime

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
DEFAULT_SNOOZE_MINUTES = 9


class TimeService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: AlarmStore | None = None,
        ringer: Ringer | None = None,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or AlarmStore(state_dir() / "alarms.json")
        self.ringer = ringer
        self._announce = announce
        self._watcher: asyncio.Task | None = None
        self._ringing: Alarm | None = None

    # -- setting things ------------------------------------------------------------------

    def add_timer(self, seconds: int, label: str = "") -> Alarm:
        return self.store.add(Kind.TIMER, datetime.now() + timedelta(seconds=seconds), label=label)

    def add_alarm(
        self,
        at: time,
        *,
        on: date | None = None,
        label: str = "",
        repeat: Repeat = Repeat.ONCE,
    ) -> Alarm:
        return self.store.add(Kind.ALARM, next_datetime(at, on), label=label, repeat=repeat, at_time=at)

    def add_reminder(self, when: datetime, text: str) -> Alarm:
        return self.store.add(Kind.REMINDER, when, label=text)

    def cancel(self, alarm_id: str) -> Alarm | None:
        if self._ringing is not None and self._ringing.id == alarm_id:
            self.dismiss()
        return self.store.remove(alarm_id)

    def cancel_all(self) -> list[Alarm]:
        self.dismiss()
        return self.store.clear()

    def pending(self) -> list[Alarm]:
        return self.store.sorted()

    # -- while it's ringing -------------------------------------------------------------

    @property
    def ringing(self) -> Alarm | None:
        return self._ringing

    def dismiss(self) -> Alarm | None:
        """Stop the noise. The alarm itself is already settled by the time it rings."""
        was, self._ringing = self._ringing, None
        if self.ringer is not None:
            self.ringer.stop()
        if was is not None:
            self._set_quiet(False)
        return was

    def snooze(self, minutes: int = DEFAULT_SNOOZE_MINUTES) -> Alarm | None:
        alarm = self.dismiss()
        if alarm is None:
            return None
        # A recurring alarm has already been moved to its next occurrence, so the snooze goes in
        # as a fresh one-off; otherwise tomorrow's 7am would become tomorrow's 7:09.
        if alarm.recurring:
            return self.store.add(alarm.kind, datetime.now() + timedelta(minutes=minutes), label=alarm.label)
        return self.store.snooze(alarm, minutes)

    # -- the loop -----------------------------------------------------------------------

    async def start(self) -> None:
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch(), name="alarms")

    async def close(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if self.ringer is not None:
            self.ringer.stop()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self._check(datetime.now())
            except Exception:
                log.exception("the alarm check failed")

    def _check(self, now: datetime) -> None:
        for alarm in self.store.due(now):
            self._fire(alarm, now)

    def _fire(self, alarm: Alarm, now: datetime) -> None:
        # Settle first: if anything below throws, the alarm has still moved on and won't spin.
        self.store.settle(alarm, now)
        if self._ringing is not None:
            return  # already making noise; one at a time is enough

        self._ringing = alarm
        name = alarm.label or alarm.kind.value
        log.info("%s %s fired", alarm.kind.value, name)
        if self.ringer is not None:
            self.ringer.start(name)
        self._set_quiet(True)
        if self._announce is not None:
            self._announce(_spoken(alarm))

    def _set_quiet(self, quiet: bool) -> None:
        """Duck music while ringing: an alarm competing with a song is no use."""
        try:
            current = runtime()
        except Exception:
            return
        current.set_ringing(quiet)


def _spoken(alarm: Alarm) -> str:
    name = alarm.label.strip()
    if alarm.kind is Kind.REMINDER:
        return f"Reminder: {name}" if name else "You asked me to remind you about something."
    if alarm.kind is Kind.TIMER:
        return f"The {name} timer is up." if name else "Your timer is up."
    return f"Alarm: {name}." if name else "Your alarm is going off."


def get_service() -> TimeService:
    """The process-wide time service, tied to the current runtime's lifetime."""
    return runtime().resource("time", _create)


def _create(rt: Runtime) -> TimeService:
    return TimeService(rt.settings, announce=rt.announce)

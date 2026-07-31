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

    def add_timer(self, seconds: int, label: str = "", then: str = "", ring: bool = True) -> Alarm:
        when = datetime.now() + timedelta(seconds=seconds)
        return self.store.add(Kind.TIMER, when, label=label, then=then, ring=ring)

    def add_alarm(
        self,
        at: time,
        *,
        on: date | None = None,
        label: str = "",
        repeat: Repeat = Repeat.ONCE,
        then: str = "",
        ring: bool = True,
    ) -> Alarm:
        return self.store.add(
            Kind.ALARM,
            next_datetime(at, on),
            label=label,
            repeat=repeat,
            at_time=at,
            then=then,
            ring=ring,
        )

    def add_reminder(self, when: datetime, text: str, then: str = "", ring: bool = True) -> Alarm:
        return self.store.add(Kind.REMINDER, when, label=text, then=then, ring=ring)

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
        if alarm.ring and self._ringing is not None:
            # One ring at a time, but the second alarm is not settled: it stays due, and fires the
            # moment the current one is dismissed. Settling it here removed it from the store
            # without it ever making a sound, which is the one thing an alarm must never do.
            return

        # Settle first: if anything below throws, the alarm has still moved on and won't spin.
        self.store.settle(alarm, now)
        name = alarm.label or alarm.kind.value
        log.info("%s %s fired", alarm.kind.value, name)

        if not alarm.ring:
            # Nobody asked to be alerted, so there is nothing to ring and nothing to stop: the job
            # is the whole point. Staying quiet also keeps music unducked, in case the job is music.
            self._say(_spoken(alarm))
            return
        self._ringing = alarm
        if self.ringer is not None:
            # If it rings itself out unanswered, the dismissal has to happen anyway: otherwise
            # "something is ringing" outlives the sound, holding the mic open and music ducked
            # until a restart.
            self.ringer.start(name, on_gave_up=self.dismiss)
        self._set_quiet(True)
        self._say(_spoken(alarm))

    def _say(self, text: str) -> None:
        if self._announce is not None:
            self._announce(text)

    def _set_quiet(self, quiet: bool) -> None:
        """Duck music while ringing: an alarm competing with a song is no use."""
        try:
            current = runtime()
        except Exception:
            return
        current.set_ringing(quiet)


def _spoken(alarm: Alarm) -> str:
    name = alarm.label.strip()
    if alarm.then and not alarm.ring:
        return f"The {name} you set is due. Do this now: {alarm.then}"
    if alarm.then:
        # It keeps ringing until the user says so, which is what they asked for by calling it an
        # alarm. Silencing it on their behalf is how "stop the alarm" gets answered with
        # "nothing is ringing". The exception is a job that makes its own noise: music is ducked
        # while anything is ringing, so the two would fight and the job would lose.
        return (
            f"The {name} is ringing. Do this as well, now: {alarm.then}. "
            "If that makes any sound of its own, call stop_alarm first."
        )
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

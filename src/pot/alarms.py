"""Timers, alarms and reminders that outlive the process.

An alarm you can't rely on is worse than no alarm, and Pot restarts: a power cut, a `systemctl
restart`, a crash. So these live in a file in the state directory rather than in memory, and are
reloaded on start. Anything already overdue while Pot was down fires immediately, because waking
up late is better than not waking up.

Times are stored as local wall-clock plus an absolute timestamp. Recurring alarms are recomputed
from the wall-clock time, so "7am on weekdays" stays 7am across a daylight-saving change rather
than drifting by an hour.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)

# Ordered so "id 3" is short to say out loud.
ID_ALPHABET = "123456789"


class Repeat(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKENDS = "weekends"
    WEEKLY = "weekly"


class Kind(StrEnum):
    TIMER = "timer"
    ALARM = "alarm"
    REMINDER = "reminder"


WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class Alarm:
    id: str
    kind: Kind
    due_at: datetime
    label: str = ""
    repeat: Repeat = Repeat.ONCE
    # Set for repeating alarms so the time of day survives being rescheduled.
    at_time: time | None = None
    snoozed_from: datetime | None = None
    # What Pot should do when this fires, in the user's own words ("play Wavy by Karan Aujla").
    # Kept as text rather than a resolved tool call so any skill can be the thing that happens.
    then: str = ""
    # Whether this is meant to get your attention. An alarm is asked for as an alarm, so it rings
    # until stopped; "turn the lights on in ten seconds" is just a job with a time on it.
    ring: bool = True

    @property
    def recurring(self) -> bool:
        return self.repeat is not Repeat.ONCE

    def remaining(self, now: datetime | None = None) -> timedelta:
        return max(timedelta(0), self.due_at - (now or datetime.now()))

    def describe(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        name = self.label or self.kind.value
        if self.kind is Kind.TIMER:
            return f"{self.id}: {name}, {_spoken_gap(self.remaining(now))} left"
        when = self.due_at.strftime("%H:%M")
        day = ""
        if self.due_at.date() != now.date():
            day = (
                " tomorrow"
                if self.due_at.date() == now.date() + timedelta(days=1)
                else (f" on {self.due_at.strftime('%A')}")
            )
        recurrence = "" if not self.recurring else f", {_spoken_repeat(self.repeat)}"
        return f"{self.id}: {name} at {when}{day}{recurrence}"

    def next_occurrence(self, after: datetime) -> datetime | None:
        """When this should fire again, or None if it's finished."""
        if not self.recurring or self.at_time is None:
            return None
        candidate = datetime.combine(after.date(), self.at_time)
        for _ in range(8):  # a week plus one, enough to find the next allowed day
            if candidate > after and _allowed(self.repeat, candidate.date(), self.due_at.date()):
                return candidate
            candidate = datetime.combine(candidate.date() + timedelta(days=1), self.at_time)
        return None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "due_at": self.due_at.isoformat(),
            "label": self.label,
            "repeat": self.repeat.value,
            "at_time": self.at_time.isoformat() if self.at_time else None,
            "then": self.then,
            "ring": self.ring,
        }

    @classmethod
    def from_json(cls, data: dict) -> Alarm:
        at_time = data.get("at_time")
        return cls(
            id=data["id"],
            kind=Kind(data["kind"]),
            due_at=datetime.fromisoformat(data["due_at"]),
            label=data.get("label", ""),
            repeat=Repeat(data.get("repeat", "once")),
            at_time=time.fromisoformat(at_time) if at_time else None,
            then=data.get("then", ""),
            ring=bool(data.get("ring", True)),
        )


def _allowed(repeat: Repeat, day: date, first: date) -> bool:
    if repeat is Repeat.DAILY:
        return True
    if repeat is Repeat.WEEKDAYS:
        return day.weekday() < 5
    if repeat is Repeat.WEEKENDS:
        return day.weekday() >= 5
    if repeat is Repeat.WEEKLY:
        return day.weekday() == first.weekday()
    return False


def _spoken_gap(gap: timedelta) -> str:
    total = int(gap.total_seconds())
    if total >= 3600:
        return f"{total // 3600}h {total % 3600 // 60}m"
    if total >= 60:
        return f"{total // 60}m {total % 60}s"
    return f"{total}s"


def _spoken_repeat(repeat: Repeat) -> str:
    return {
        Repeat.DAILY: "every day",
        Repeat.WEEKDAYS: "on weekdays",
        Repeat.WEEKENDS: "at weekends",
        Repeat.WEEKLY: "every week",
    }.get(repeat, "once")


@dataclass
class AlarmStore:
    """The set of pending timers and alarms, saved after every change.

    Two processes can hold this file at once: the running assistant, and `pot alarms cancel` from
    a shell. Reads check the file's mtime and reload when someone else wrote it, so a CLI
    cancellation reaches the daemon within a tick instead of being resurrected by its stale
    memory. Writes are last-writer-wins, which for a device with one household is livable; real
    concurrent writing would need a lock file.
    """

    path: Path
    alarms: dict[str, Alarm] = field(default_factory=dict)
    _mtime: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._mtime = None
            return
        try:
            raw = json.loads(self.path.read_text())
            self._mtime = self.path.stat().st_mtime_ns
        except (json.JSONDecodeError, OSError):
            log.warning("could not read alarms at %s; starting empty", self.path)
            return
        for entry in raw.get("alarms", []):
            try:
                alarm = Alarm.from_json(entry)
            except (KeyError, ValueError):
                log.warning("skipping an unreadable alarm: %r", entry)
                continue
            self.alarms[alarm.id] = alarm

    def _refresh(self) -> None:
        """Pick up changes another process made to the file."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            if self.alarms:
                # Someone cleared the file out from under us; firing from memory would resurrect
                # alarms the user just cancelled.
                self.alarms.clear()
            self._mtime = None
            return
        if mtime != self._mtime:
            self.alarms.clear()
            self.load()

    def save(self) -> None:
        """Write the whole store, atomically.

        A plain overwrite truncates first, so a power cut mid-write leaves invalid JSON and the
        next start treats it as an empty store: every alarm gone. Writing beside the file and
        renaming over it means the store is always either the old state or the new one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Straight from memory, deliberately not via sorted(): a save is the authoritative state
        # being written out, and refreshing against the file it is about to replace once wiped
        # the very alarms being saved.
        ordered = sorted(self.alarms.values(), key=lambda alarm: alarm.due_at)
        payload = {"alarms": [alarm.to_json() for alarm in ordered]}
        scratch = self.path.with_suffix(".json.tmp")
        with open(scratch, "w") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    def sorted(self) -> list[Alarm]:
        self._refresh()
        return sorted(self.alarms.values(), key=lambda alarm: alarm.due_at)

    def add(
        self,
        kind: Kind,
        due_at: datetime,
        *,
        label: str = "",
        repeat: Repeat = Repeat.ONCE,
        at_time: time | None = None,
        then: str = "",
        ring: bool = True,
    ) -> Alarm:
        alarm = Alarm(
            id=self._next_id(),
            kind=kind,
            due_at=due_at,
            label=label,
            repeat=repeat,
            at_time=at_time if repeat is not Repeat.ONCE else None,
            then=then,
            ring=ring,
        )
        self.alarms[alarm.id] = alarm
        self.save()
        return alarm

    def remove(self, alarm_id: str) -> Alarm | None:
        alarm = self.alarms.pop(alarm_id, None)
        if alarm is not None:
            self.save()
        return alarm

    def clear(self) -> list[Alarm]:
        gone = self.sorted()
        self.alarms.clear()
        self.save()
        return gone

    def due(self, now: datetime | None = None) -> list[Alarm]:
        now = now or datetime.now()
        return [alarm for alarm in self.sorted() if alarm.due_at <= now]

    def settle(self, alarm: Alarm, now: datetime | None = None) -> Alarm | None:
        """Called once an alarm has fired: reschedule it if it repeats, else drop it."""
        now = now or datetime.now()
        following = alarm.next_occurrence(now)
        if following is None:
            self.remove(alarm.id)
            return None
        rescheduled = replace(alarm, due_at=following, snoozed_from=None)
        self.alarms[alarm.id] = rescheduled
        self.save()
        return rescheduled

    def snooze(self, alarm: Alarm, minutes: int, now: datetime | None = None) -> Alarm:
        """Push an alarm out without losing its recurrence."""
        now = now or datetime.now()
        snoozed = replace(
            alarm,
            due_at=now + timedelta(minutes=minutes),
            snoozed_from=alarm.snoozed_from or alarm.due_at,
        )
        self.alarms[alarm.id] = snoozed
        self.save()
        return snoozed

    def _next_id(self) -> str:
        for character in ID_ALPHABET:
            if character not in self.alarms:
                return character
        # More than nine pending is unusual; fall back to numbers that still read out loud.
        index = 10
        while str(index) in self.alarms:
            index += 1
        return str(index)


def next_datetime(at: time, on: date | None = None, now: datetime | None = None) -> datetime:
    """The next moment matching a wall-clock time, today if it hasn't passed, else tomorrow."""
    now = now or datetime.now()
    if on is not None:
        return datetime.combine(on, at)
    candidate = datetime.combine(now.date(), at)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def describe_all(alarms: Iterable[Alarm], now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [alarm.describe(now) for alarm in alarms]
    return "\n".join(lines) if lines else "Nothing is set."

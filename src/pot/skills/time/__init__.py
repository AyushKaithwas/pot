"""Timers, alarms and reminders.

The model handles the language ("quarter past seven", "in ten minutes", "every weekday") and these
tools take the resolved values, so there's no date parsing here to get subtly wrong.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from agents import function_tool

from pot.alarms import Repeat, describe_all
from pot.runtime import Runtime
from pot.skills.base import Skill
from pot.skills.time.service import DEFAULT_SNOOZE_MINUTES, get_service

log = logging.getLogger(__name__)

PROMPT = """
Time: timers count down, alarms happen at a clock time, reminders say words back.
- You convert the language, the tools take numbers: "ten minutes" is 600 seconds, "quarter past
  seven" is "19:15" if that's the next one due, "every weekday" is repeat="weekdays".
- Check current_time first whenever the request depends on today's date or the current hour.
- Alarms ring until stopped. When one is ringing, "stop" means stop_alarm and "snooze" means
  snooze; use them rather than talking over it.
- Say the time back so a mishearing is obvious ("alarm set for 7am"), then stop.
""".strip()


@function_tool
def set_timer(seconds: int, label: str = "") -> str:
    """Start a countdown that rings when it finishes.

    Args:
        seconds: Total length in whole seconds; convert minutes and hours yourself.
        label: Short name, e.g. "pasta". Optional.
    """
    if seconds <= 0:
        return "A timer needs a positive number of seconds."
    alarm = get_service().add_timer(seconds, label.strip())
    return f"Timer {alarm.id} set: {alarm.describe()}"


@function_tool
def set_alarm(at: str, repeat: str = "once", label: str = "", on: str = "") -> str:
    """Set an alarm for a clock time. It rings until stopped or snoozed.

    Args:
        at: 24-hour time, e.g. "07:30". The next matching time is used.
        repeat: once, daily, weekdays, weekends or weekly.
        label: Short name, e.g. "gym". Optional.
        on: Optional ISO date ("2026-08-01") for a specific day rather than the next one.
    """
    try:
        at_time = time.fromisoformat(at.strip())
    except ValueError:
        return f"I need a 24-hour time like 07:30, not {at!r}."
    try:
        repeat_rule = Repeat(repeat.strip().lower() or "once")
    except ValueError:
        allowed = ", ".join(rule.value for rule in Repeat)
        return f"repeat must be one of {allowed}."
    day: date | None = None
    if on.strip():
        try:
            day = date.fromisoformat(on.strip())
        except ValueError:
            return f"I need an ISO date like 2026-08-01, not {on!r}."

    alarm = get_service().add_alarm(at_time, on=day, label=label.strip(), repeat=repeat_rule)
    return f"Alarm {alarm.id} set: {alarm.describe()}"


@function_tool
def remind_me(text: str, seconds: int = 0, at: str = "") -> str:
    """Say something back later, either after a delay or at a clock time.

    Args:
        text: What to say when it fires.
        seconds: Delay in whole seconds. Use this or `at`, not both.
        at: 24-hour time, e.g. "18:00", for the next time that comes round.
    """
    if not text.strip():
        return "A reminder needs something to say."
    service = get_service()
    if at.strip():
        try:
            at_time = time.fromisoformat(at.strip())
        except ValueError:
            return f"I need a 24-hour time like 18:00, not {at!r}."
        from pot.alarms import next_datetime

        alarm = service.add_reminder(next_datetime(at_time), text.strip())
    elif seconds > 0:
        alarm = service.add_reminder(datetime.now() + timedelta(seconds=seconds), text.strip())
    else:
        return "Tell me when: a delay in seconds, or a time like 18:00."
    return f"Reminder {alarm.id} set: {alarm.describe()}"


@function_tool
def list_alarms() -> str:
    """Every timer, alarm and reminder that hasn't fired yet."""
    return describe_all(get_service().pending())


@function_tool
def cancel_alarm(which: str) -> str:
    """Cancel one timer, alarm or reminder by id, or "all" for everything.

    Args:
        which: The id from list_alarms, e.g. "2", or "all".
    """
    service = get_service()
    if which.strip().lower() == "all":
        gone = service.cancel_all()
        return f"Cancelled {len(gone)}." if gone else "Nothing was set."
    alarm = service.cancel(which.strip())
    return f"Cancelled {alarm.label or alarm.kind.value}." if alarm else f"Nothing with id {which}."


@function_tool
def stop_alarm() -> str:
    """Stop whatever is ringing right now."""
    alarm = get_service().dismiss()
    return "Stopped." if alarm else "Nothing is ringing."


@function_tool
def snooze(minutes: int = DEFAULT_SNOOZE_MINUTES) -> str:
    """Silence what's ringing and bring it back shortly.

    Args:
        minutes: How long to wait before it rings again.
    """
    alarm = get_service().snooze(max(1, minutes))
    if alarm is None:
        return "Nothing is ringing."
    return f"Snoozed, back at {alarm.due_at.strftime('%H:%M')}."


async def _start(runtime: Runtime) -> None:
    """Start watching the clock, and ring anything that came due while Pot was down."""
    service = get_service()
    service.ringer = runtime.ringer
    await service.start()


SKILL = Skill(
    name="time",
    description="Timers, alarms and reminders, which survive a restart",
    tools=(set_timer, set_alarm, remind_me, list_alarms, cancel_alarm, stop_alarm, snooze),
    prompt=PROMPT,
    start=_start,
)

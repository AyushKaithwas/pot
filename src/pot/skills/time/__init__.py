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

SECONDS_PER = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
}

PROMPT = """
Time: timers count down, alarms happen at a clock time, reminders say words back.
- Pass the number and the unit the user actually said and let the tool do the arithmetic:
  "ten seconds" is duration=10 unit="seconds", "ten minutes" is duration=10 unit="minutes".
- You still convert the wording the tools have no field for: "quarter past seven" is "19:15" if
  that's the next one due, "every weekday" is repeat="weekdays".
- Never swap the unit for a larger one: "twenty seconds" is duration=20 unit="seconds", never any
  number of minutes. If you didn't catch the number or the unit, ask instead of guessing.
- Read the length back from what the tool returned, not from what you meant to set. If they
  disagree the tool is right, and saying the wrong one is how a ten second timer becomes ten
  minutes with nobody noticing.
- "when it goes off, play X" is the `then` argument, in the user's words. Pot does it when the
  time comes, so promise it only when you've actually passed it. There's no other way to schedule
  an action, and a `then` runs whether or not the alarm also rings.
- `ring` follows what they asked for. Called it an alarm, a timer or a reminder? It rings, and it
  keeps ringing until they stop it. Just a job with a time on it ("turn the lights on in ten
  seconds", "lights off at eleven")? ring=false, so nothing chimes and there's nothing to stop.
  Also ring=false when the `then` is itself the alert, like waking them up to music.
- Check current_time first whenever the request depends on today's date or the current hour.
- Alarms ring until stopped. When one is ringing, "stop" means stop_alarm and "snooze" means
  snooze; use them rather than talking over it.
- Say the time back so a mishearing is obvious ("alarm set for 7am"), then stop.
""".strip()


def _to_seconds(duration: int, unit: str) -> int | None:
    """A count and its unit, which is what the user said, rather than a number to be multiplied.

    Asking the model for seconds looks simpler and reads back wrong: "ten seconds" and "ten
    minutes" both arrive as 10, and whichever example the prompt gives is the one it copies.
    """
    singular = unit.strip().lower().removesuffix("s") or unit.strip().lower()
    multiplier = SECONDS_PER.get(singular)
    return None if multiplier is None else duration * multiplier


@function_tool
def set_timer(
    duration: int, unit: str = "seconds", label: str = "", then: str = "", ring: bool = True
) -> str:
    """Start a countdown. Rings when it finishes unless told not to.

    Args:
        duration: How many of `unit` to count down. For "ten minutes" this is 10, not 600.
        unit: seconds, minutes or hours. Say what the user said and let this do the arithmetic.
        label: Short name, e.g. "pasta". Optional.
        then: What to do when it fires, e.g. "play Wavy by Karan Aujla". Optional.
        ring: False for a job with a time on it that shouldn't chime, e.g. "lights on in a minute".
    """
    seconds = _to_seconds(duration, unit)
    if seconds is None:
        return f"I don't know the unit {unit!r}. Use seconds, minutes or hours."
    if seconds <= 0:
        return "A timer needs a positive length."
    alarm = get_service().add_timer(seconds, label.strip(), then.strip(), ring)
    return f"Timer {alarm.id} set: {alarm.describe()}. Say that length back exactly."


@function_tool
def set_alarm(
    at: str,
    repeat: str = "once",
    label: str = "",
    on: str = "",
    then: str = "",
    ring: bool = True,
) -> str:
    """Set an alarm for a clock time. It rings until stopped or snoozed.

    Args:
        at: 24-hour time, e.g. "07:30". The next matching time is used.
        repeat: once, daily, weekdays, weekends or weekly.
        label: Short name, e.g. "gym". Optional.
        on: Optional ISO date ("2026-08-01") for a specific day rather than the next one.
        then: What to do when it fires, e.g. "play the news". Optional.
        ring: False for a job with a time on it that shouldn't chime, e.g. "lights off at eleven".
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

    alarm = get_service().add_alarm(
        at_time, on=day, label=label.strip(), repeat=repeat_rule, then=then.strip(), ring=ring
    )
    return f"Alarm {alarm.id} set: {alarm.describe()}"


@function_tool
def remind_me(text: str, delay: int = 0, unit: str = "seconds", at: str = "") -> str:
    """Say something back later, either after a delay or at a clock time.

    Args:
        text: What to say when it fires.
        delay: How many of `unit` to wait. Use this or `at`, not both.
        unit: seconds, minutes or hours. Say what the user said, don't convert.
        at: 24-hour time, e.g. "18:00", for the next time that comes round.
    """
    if not text.strip():
        return "A reminder needs something to say."
    seconds = _to_seconds(delay, unit)
    if seconds is None:
        return f"I don't know the unit {unit!r}. Use seconds, minutes or hours."
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
        return "Tell me when: a delay, or a time like 18:00."
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

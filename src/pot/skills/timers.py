"""Timers and reminders. Fires speak on their own via runtime().announce."""

from __future__ import annotations

from agents import function_tool

from pot.runtime import runtime
from pot.skills.base import Skill

PROMPT = """
Timers: set_timer for plain countdowns, remind_me when the user wants specific words read back.
Both take whole seconds, so convert "ten minutes" yourself. You'll be prompted when one fires.
""".strip()


@function_tool
def set_timer(seconds: int, label: str = "timer") -> str:
    """Start a countdown and announce it when it finishes.

    Args:
        seconds: How long to wait, in whole seconds.
        label: Short name for the timer, e.g. "pasta".
    """
    if seconds <= 0:
        return "A timer needs a positive number of seconds."

    async def fire() -> None:
        runtime().announce(f"The {label} timer is up.")

    job = runtime().scheduler.schedule(seconds, label, fire)
    return f"Timer {job.id} set: {label} in {seconds}s."


@function_tool
def remind_me(seconds: int, text: str) -> str:
    """Say something back to the user after a delay.

    Args:
        seconds: How long to wait, in whole seconds.
        text: What to remind the user about.
    """
    if seconds <= 0:
        return "A reminder needs a positive number of seconds."

    async def fire() -> None:
        runtime().announce(f"Reminder: {text}")

    job = runtime().scheduler.schedule(seconds, text, fire)
    return f"Reminder {job.id} set for {seconds}s from now."


@function_tool
def list_timers() -> str:
    """List timers, reminders and delayed actions that are still pending."""
    jobs = runtime().scheduler.jobs()
    if not jobs:
        return "Nothing is pending."
    return "\n".join(job.describe() for job in jobs)


@function_tool
def cancel_timer(timer_id: str) -> str:
    """Cancel one pending timer, reminder or delayed action by its id, or "all" for everything.

    Args:
        timer_id: The id shown by list_timers, e.g. "t1", or "all".
    """
    scheduler = runtime().scheduler
    if timer_id.strip().lower() == "all":
        cancelled = scheduler.cancel_all()
        return f"Cancelled {len(cancelled)} pending item(s)." if cancelled else "Nothing was pending."
    job = scheduler.cancel(timer_id.strip())
    return f"Cancelled {job.label}." if job else f"No pending item called {timer_id}."


SKILL = Skill(
    name="timers",
    description="Countdown timers, reminders, and cancelling pending actions",
    tools=(set_timer, remind_me, list_timers, cancel_timer),
    prompt=PROMPT,
)

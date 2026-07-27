"""Small built-ins: the clock, and Pot describing its own capabilities."""

from __future__ import annotations

from datetime import datetime

from agents import function_tool

from pot.runtime import runtime
from pot.skills.base import Skill

PROMPT = """
Use current_time for anything about the date, the time, or the day of the week.
Use capabilities when asked what you can do, so you describe what is actually loaded.
""".strip()


@function_tool
def current_time() -> str:
    """The current local date and time."""
    now = datetime.now().astimezone()
    return now.strftime("%A %d %B %Y, %H:%M %Z")


@function_tool
def capabilities() -> str:
    """List the skills that are currently loaded."""
    from pot.skills import load

    skills = load(runtime().settings)
    return "\n".join(f"{skill.name}: {skill.description}" for skill in skills)


SKILL = Skill(
    name="basics",
    description="Date and time, and what Pot can currently do",
    tools=(current_time, capabilities),
    prompt=PROMPT,
)

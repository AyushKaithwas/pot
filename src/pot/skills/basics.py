"""Small built-ins: the clock, and Pot describing its own capabilities."""

from __future__ import annotations

from datetime import datetime

from agents import function_tool

from pot.runtime import runtime
from pot.skills.base import Skill

PROMPT = """
Use current_time for anything about the date, the time, or the day of the week.
Use capabilities when asked what you can do, so you describe what is actually loaded.
Use stop_listening when they're finished with you: "stop", "that's all", "never mind", "thanks,
that's it", "go to sleep". Don't reach for it when something is ringing or playing, though:
those have their own stops, and this one only ends the conversation. Acknowledge in a word or
say nothing; they'll hear the tone that means you've gone quiet.
""".strip()


@function_tool
def current_time() -> str:
    """The current local date and time."""
    now = datetime.now().astimezone()
    return now.strftime("%A %d %B %Y, %H:%M %Z")


def _stop_listening() -> str:
    """End the exchange now instead of waiting out the idle timeout.

    Without this, going quiet takes conversation_idle_seconds of silence, during which the mic
    stays open and the room keeps being streamed to a paid API. Saying "that's all" should be
    enough, the way hanging up ends a phone call.
    """
    rt = runtime()
    if rt.settings.audio.mode == "always_on":
        # There is no gate to close in always-on, so pretending otherwise would be a silent no-op.
        return "This device is set to always-on, so I keep listening. Nothing to stop."
    rt.end_conversation()
    return "Going quiet."


@function_tool
def stop_listening() -> str:
    """Stop listening and wait for the wake word again. For "stop", "that's all", "never mind"."""
    return _stop_listening()


@function_tool
def capabilities() -> str:
    """List the skills that are currently loaded."""
    from pot.skills import load

    skills = load(runtime().settings)
    return "\n".join(f"{skill.name}: {skill.description}" for skill in skills)


SKILL = Skill(
    name="basics",
    description="Date and time, and what Pot can currently do",
    tools=(current_time, capabilities, stop_listening),
    prompt=PROMPT,
)

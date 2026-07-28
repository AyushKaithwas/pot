"""Lights skill: one tool call can hit several lights and several attributes at once."""

from __future__ import annotations

import logging

from agents import function_tool

from pot.runtime import runtime
from pot.skills.base import Skill
from pot.skills.lights.provider import LightChange, LightError
from pot.skills.lights.service import get_service

log = logging.getLogger(__name__)

PROMPT = """
Lights: you can switch them, dim them, shift them warm or cool, and set colours.
- Batch everything you can into a single set_lights call: it accepts several targets and
  several attributes at once, and `after_seconds` defers the whole change.
- brightness_pct is 1-100 and color_temp_pct is 0-100, where 0 is warmest and 100 coolest. There
  is nothing above 100: "full brightness" is 100. Use power off rather than a brightness of 0.
- A light shows either a colour or a white temperature, never both. Send `color` with
  `brightness_pct` to dim a colour; don't send `color_temp_pct` alongside a colour.
- `color` is a 6-digit hex value, so translate spoken colours yourself ("red" -> ff0000).
- "White" is not a colour on these lights, it's the white scale: send color_temp_pct, not
  color=ffffff. Plain white is color_temp_pct=50, warm white low, cool white high.
- The tool reports the state the light is actually in afterwards. Trust it over your own
  expectation, and don't claim a change the result doesn't show.
- Never ask which light when only one is configured; just act.
- Confirm in a few words ("done", "on at 30 percent"). Never read out device ids.
""".strip()


@function_tool
async def list_lights() -> str:
    """List the lights that can be controlled, with their current state."""
    service = get_service()
    if not service.devices:
        return "No lights are configured yet."
    outcomes = await service.read(list(service.devices.values()))
    return "\n".join(o.describe() for o in outcomes)


@function_tool
async def set_lights(
    targets: list[str],
    power: bool | None = None,
    brightness_pct: int | None = None,
    color_temp_pct: int | None = None,
    color: str | None = None,
    after_seconds: int = 0,
) -> str:
    """Change one or more lights.

    Args:
        targets: Light names, rooms, or "all". Several are allowed.
        power: True to switch on, False to switch off, omit to leave as is.
        brightness_pct: Brightness from 1 to 100.
        color_temp_pct: 0 is the warmest white, 100 the coolest.
        color: Optional 6-digit hex colour, only on lights that support colour.
        after_seconds: Delay the change by this many seconds. 0 applies it now.
    """
    service = get_service()
    change, note = LightChange(power, brightness_pct, color_temp_pct, color).resolved()
    if change.is_empty():
        return "Nothing to change: no power, brightness, temperature or colour was given."

    try:
        devices = service.resolve_many(targets)
    except LightError as exc:
        return str(exc)
    if not devices:
        return "No matching lights."

    if after_seconds > 0:
        names = ", ".join(d.spoken_name for d in devices)
        label = f"{change.describe()} on {names}"

        async def later() -> None:
            outcomes = await service.apply(devices, change)
            runtime().announce(f"Done: {label}. {'; '.join(o.describe() for o in outcomes)}")

        job = runtime().scheduler.schedule(after_seconds, label, later)
        return _with_note(f"Scheduled ({job.id}): {label} in {after_seconds} seconds.", note)

    outcomes = await service.apply(devices, change)
    return _with_note("\n".join(o.describe() for o in outcomes), note)


@function_tool
async def apply_scene(scene: str) -> str:
    """Apply a named lighting scene from the configuration, e.g. "night" or "bright"."""
    try:
        outcomes = await get_service().apply_scene(scene)
    except LightError as exc:
        return str(exc)
    return "\n".join(o.describe() for o in outcomes)


def _with_note(text: str, note: str | None) -> str:
    return f"{text} ({note})" if note else text


SKILL = Skill(
    name="lights",
    description="Switch, dim and tune the smart lights, now or on a delay",
    tools=(list_lights, set_lights, apply_scene),
    prompt=PROMPT,
)

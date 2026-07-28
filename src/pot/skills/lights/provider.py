"""Provider-agnostic light control types.

`LightChange` is what a caller wants; `LightState` is what came back. Providers are
interchangeable (LAN, Tuya cloud, later Home Assistant) and `ProviderChain` walks them in
order so a LAN timeout silently falls through to the cloud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from pot.config import LightDevice

log = logging.getLogger(__name__)

# A "colour" this close to grey is white, and white on these lights is a work mode rather than a
# hue. Warm and cool whites sit under the line too, which is what we want: they belong on the
# temperature scale. Warm white as a hex lands around 0.12 and a pale pink around 0.18, so this
# sits between them, with anything you'd actually name as a colour far above.
WHITE_SATURATION = 0.15
# Where "white" with no temperature given lands: neither warm nor cool.
NEUTRAL_WHITE_PCT = 50
# These lights have no zero brightness: off is off, and the dimmest they go is 1%.
MIN_BRIGHTNESS_PCT = 1


class LightError(RuntimeError):
    """Any failure to control or read a light."""


class LightUnavailable(LightError):
    """The device could not be reached by any provider."""


class LightUnsupported(LightError):
    """The device does not have the capability that was asked for."""


def _clamp(percent: int | None, low: int = 0) -> int | None:
    return None if percent is None else max(low, min(100, percent))


def _out_of_range(percent: int | None, low: int = 0) -> bool:
    return percent is not None and not low <= percent <= 100


def _channels(color: str) -> tuple[int, int, int] | None:
    text = color.strip().lstrip("#")
    if len(text) < 6:
        return None
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None
    return red, green, blue


def _is_white(color: str) -> bool:
    """Whether a hex colour is really just white, saturation being what makes a colour a colour."""
    channels = _channels(color)
    if channels is None or max(channels) == 0:
        return False
    return (max(channels) - min(channels)) / max(channels) <= WHITE_SATURATION


def _white_temperature(color: str) -> int:
    """Where on the warm-to-cool scale a white-ish hex belongs.

    "Warm white" arrives as a hex with more red than blue, and answering every white with a
    neutral one would throw that away.
    """
    channels = _channels(color)
    if channels is None or max(channels) == 0:
        return NEUTRAL_WHITE_PCT
    red, _, blue = channels
    tilt = (blue - red) / max(channels) / WHITE_SATURATION
    return round(min(100, max(0, NEUTRAL_WHITE_PCT * (1 + tilt))))


@dataclass(frozen=True)
class LightChange:
    power: bool | None = None
    brightness_pct: int | None = None
    color_temp_pct: int | None = None
    color: str | None = None

    def is_empty(self) -> bool:
        return all(
            value is None for value in (self.power, self.brightness_pct, self.color_temp_pct, self.color)
        )

    def with_implied_power(self) -> LightChange:
        """Setting brightness or colour on a light that's off implies turning it on."""
        if self.power is None and not self.is_empty():
            return LightChange(True, self.brightness_pct, self.color_temp_pct, self.color)
        return self

    def resolved(self) -> tuple[LightChange, str | None]:
        """A light is either showing a colour or a white temperature, never both.

        Asking for both would apply one and silently undo the other, so drop the temperature and
        say so rather than reporting a state the light isn't in.
        """
        if _out_of_range(self.brightness_pct, MIN_BRIGHTNESS_PCT) or _out_of_range(self.color_temp_pct):
            # Clamp at the edge, so what was asked for is always something the light can hold.
            # Left alone, an impossible request stays in the change and gets reported back as if
            # it happened: "200 percent" for a light sitting at its maximum.
            clamped = replace(
                self,
                brightness_pct=_clamp(self.brightness_pct, MIN_BRIGHTNESS_PCT),
                color_temp_pct=_clamp(self.color_temp_pct),
            )
            return clamped.resolved()[0], "that was outside 1 to 100, so it went to the nearest end"
        if self.color and _is_white(self.color):
            # "White" as a hex colour leaves the bulb in colour mode with the saturation at zero,
            # which reads as dim grey, not white, and any brightness change after it stays stuck
            # there. On these lights white is the white work mode, which is the temperature scale.
            temperature = (
                self.color_temp_pct if self.color_temp_pct is not None else _white_temperature(self.color)
            )
            return replace(self, color=None, color_temp_pct=temperature), None
        if self.color and self.color_temp_pct is not None:
            note = "colour and warm/cool can't both apply, so the temperature was ignored"
            return replace(self, color_temp_pct=None), note
        return self, None

    def describe(self) -> str:
        parts = []
        if self.power is not None:
            parts.append("on" if self.power else "off")
        if self.brightness_pct is not None:
            parts.append(f"{self.brightness_pct}% brightness")
        if self.color_temp_pct is not None:
            parts.append(f"{self.color_temp_pct}% warm-to-cool")
        if self.color:
            parts.append(f"colour {self.color}")
        return ", ".join(parts) or "no change"


@dataclass(frozen=True)
class LightState:
    power: bool | None = None
    brightness_pct: int | None = None
    color_temp_pct: int | None = None
    color: str | None = None
    mode: str = ""
    via: str = ""

    def describe(self) -> str:
        if self.power is False:
            return "off"
        parts = ["on" if self.power else "unknown"]
        if self.brightness_pct is not None:
            parts.append(f"{self.brightness_pct}%")
        if self.color_temp_pct is not None:
            parts.append(f"temp {self.color_temp_pct}%")
        if self.color:
            parts.append(f"colour #{self.color}")
        if self.mode and self.mode != "white":
            parts.append(f"{self.mode} mode")
        return ", ".join(parts)


@runtime_checkable
class LightProvider(Protocol):
    name: str

    async def apply(self, device: LightDevice, change: LightChange) -> LightState: ...

    async def read(self, device: LightDevice) -> LightState: ...

    async def close(self) -> None: ...


class ProviderChain:
    """Tries each provider in order, reporting which one served the call."""

    def __init__(self, providers: list[LightProvider]) -> None:
        self.providers = providers

    async def apply(self, device: LightDevice, change: LightChange) -> LightState:
        return await self._attempt("apply", device, change)

    async def read(self, device: LightDevice) -> LightState:
        return await self._attempt("read", device)

    async def close(self) -> None:
        for provider in self.providers:
            try:
                await provider.close()
            except Exception:
                log.debug("provider %s failed to close", provider.name, exc_info=True)

    async def _attempt(self, op: str, device: LightDevice, *args: object) -> LightState:
        if not self.providers:
            raise LightUnavailable("no light providers are configured")
        failures = []
        for provider in self.providers:
            try:
                return await getattr(provider, op)(device, *args)
            except LightUnsupported:
                raise
            except Exception as exc:
                log.warning("%s via %s failed for %s: %s", op, provider.name, device.id, exc)
                failures.append(f"{provider.name}: {exc}")
        raise LightUnavailable(f"{device.spoken_name} unreachable ({'; '.join(failures)})")

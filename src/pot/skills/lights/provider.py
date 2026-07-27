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


class LightError(RuntimeError):
    """Any failure to control or read a light."""


class LightUnavailable(LightError):
    """The device could not be reached by any provider."""


class LightUnsupported(LightError):
    """The device does not have the capability that was asked for."""


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

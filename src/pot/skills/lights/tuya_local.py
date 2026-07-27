"""LAN control of Tuya devices via tinytuya.

tinytuya is synchronous and one connection per device is not thread-safe, so every call runs
in a worker thread behind a per-device lock. Addresses come from UDP discovery, so a device
that changed IP recovers on the retry instead of failing for good.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging
from dataclasses import replace

import tinytuya

from pot.config import LightDevice
from pot.skills.lights import discovery
from pot.skills.lights.provider import (
    LightChange,
    LightError,
    LightState,
    LightUnsupported,
)

log = logging.getLogger(__name__)

CONNECTION_TIMEOUT_SECONDS = 4


class TuyaLocalProvider:
    name = "local"

    def __init__(self) -> None:
        self._bulbs: dict[str, tinytuya.BulbDevice] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def apply(self, device: LightDevice, change: LightChange) -> LightState:
        return await self._with_bulb(device, lambda bulb: _apply_sync(bulb, change))

    async def read(self, device: LightDevice) -> LightState:
        return await self._with_bulb(device, _read_sync)

    async def close(self) -> None:
        bulbs, self._bulbs = self._bulbs, {}
        for bulb in bulbs.values():
            await asyncio.to_thread(bulb.close)

    async def _with_bulb(self, device: LightDevice, work) -> LightState:
        if not device.local_key:
            raise LightError(
                f"no local key for {device.spoken_name}; run `pot lights keys` after linking "
                "the Tuya cloud project"
            )
        async with self._locks.setdefault(device.gwid, asyncio.Lock()):
            for attempt in (1, 2):
                bulb = await self._bulb(device, refresh=attempt == 2)
                try:
                    state = await asyncio.to_thread(work, bulb)
                except Exception as exc:
                    log.debug("local attempt %s for %s failed: %s", attempt, device.id, exc)
                    self._drop(device)
                    if attempt == 2:
                        raise
                    continue
                return replace(state, via=self.name)
        raise LightError("unreachable")  # pragma: no cover - loop always returns or raises

    async def _bulb(self, device: LightDevice, *, refresh: bool) -> tinytuya.BulbDevice:
        if not refresh and device.gwid in self._bulbs:
            return self._bulbs[device.gwid]
        found = await discovery.find(device.gwid, refresh=refresh)
        if found is None:
            raise LightError(f"{device.spoken_name} is not answering on the network")
        bulb = tinytuya.BulbDevice(
            dev_id=device.gwid,
            address=found.ip,
            local_key=device.local_key,
            version=device.version or found.version,
            persist=True,
            connection_timeout=CONNECTION_TIMEOUT_SECONDS,
        )
        self._bulbs[device.gwid] = bulb
        return bulb

    def _drop(self, device: LightDevice) -> None:
        bulb = self._bulbs.pop(device.gwid, None)
        if bulb is not None:
            try:
                bulb.close()
            except Exception:
                log.debug("closing %s failed", device.id, exc_info=True)
        discovery.forget(device.gwid)


def _apply_sync(bulb: tinytuya.BulbDevice, change: LightChange) -> LightState:
    change = change.with_implied_power()

    if change.power is False:
        _check(bulb.turn_off())
        return LightState(power=False)

    if change.power:
        _check(bulb.turn_on())

    if change.color:
        _set_colour(bulb, change.color, change.brightness_pct)
    elif change.color_temp_pct is not None:
        # A white temperature only exists in white mode, so this one does switch.
        if not bulb.bulb_has_capability(bulb.BULB_FEATURE_COLOURTEMP):
            raise LightUnsupported("this light has a fixed colour temperature")
        _check(bulb.set_mode(bulb.DPS_MODE_WHITE))
        if change.brightness_pct is not None:
            _check(bulb.set_brightness_percentage(_clamp_brightness(change.brightness_pct)))
        _check(bulb.set_colourtemp_percentage(_clamp_pct(change.color_temp_pct)))
    elif change.brightness_pct is not None:
        _set_brightness(bulb, change.brightness_pct)

    return _read_sync(bulb, fresh=True)


def _set_colour(bulb: tinytuya.BulbDevice, color: str, brightness_pct: int | None) -> None:
    """Set hue and level in one command.

    Brightness has to ride inside the colour: tinytuya's set_brightness picks between white and
    colour mode from its cached state, which right after a colour change still says white, so a
    separate brightness call would flip the light to white and undo the colour.
    """
    if not bulb.bulb_has_capability(bulb.BULB_FEATURE_COLOUR):
        raise LightUnsupported("this light cannot do colours, only warm-to-cool white")
    hue, saturation, value = _hex_to_hsv(color)
    if brightness_pct is not None:
        value = _clamp_brightness(brightness_pct) / 100
    _check(bulb.set_hsv(hue, saturation, value))


def _set_brightness(bulb: tinytuya.BulbDevice, brightness_pct: int) -> None:
    """Dim without changing what the light is showing.

    "Dim it" must not turn a red light white, so a light already showing a colour keeps its hue
    and only drops the level. The read is deliberately fresh: the current mode decides which
    path to take, and tinytuya's cached mode is often a change behind.
    """
    current = _read_sync(bulb, fresh=True)
    if current.mode == bulb.DPS_MODE_COLOUR and current.color:
        hue, saturation, _ = _hex_to_hsv(current.color)
        _check(bulb.set_hsv(hue, saturation, _clamp_brightness(brightness_pct) / 100))
        return
    _check(bulb.set_brightness_percentage(_clamp_brightness(brightness_pct)))


def _read_sync(bulb: tinytuya.BulbDevice, *, fresh: bool = False) -> LightState:
    """tinytuya knows each bulb's own value ranges, so let it do the percentage maths.

    After a change, the persistent connection's cached status can still hold the old work mode,
    so re-query the device rather than reporting a state that never existed.
    """
    if fresh:
        _check(bulb.status())
    state = _check(bulb.state())
    power = state.get("is_on", state.get("switch"))
    mode = state.get("mode") or ""
    brightness = _percentage(bulb.get_brightness_percentage, state)
    color_temp = _percentage(bulb.get_colourtemp_percentage, state)

    color = None
    if mode == bulb.DPS_MODE_COLOUR:
        # In colour mode the visible level is the HSV value, not the white brightness dp. The
        # colour is reported at full value so it stays recognisable ("ff0000" for red at 20%)
        # rather than being darkened twice over by the brightness that's already reported.
        hsv = bulb.colour_hsv(state=state)
        if isinstance(hsv, tuple):
            hue, saturation, value = hsv
            brightness = max(0, min(100, round(value * 100)))
            color = _hsv_to_hex(hue, saturation)
        color_temp = None

    return LightState(
        power=bool(power) if power is not None else None,
        brightness_pct=brightness,
        color_temp_pct=color_temp,
        color=color,
        mode=mode,
    )


def _percentage(getter, state: dict) -> int | None:
    try:
        value = getter(state=state)
    except Exception:  # capability absent or value missing
        return None
    return None if value is None else max(0, min(100, round(value)))


def _clamp_pct(value: int) -> int:
    return max(0, min(100, int(value)))


def _clamp_brightness(value: int) -> int:
    """0% would just be off, and Tuya rejects it, so the floor is 1."""
    return max(1, min(100, int(value)))


def _hsv_to_hex(hue: float, saturation: float) -> str:
    channels = colorsys.hsv_to_rgb(hue, saturation, 1.0)
    return "".join(f"{max(0, min(255, round(channel * 255))):02x}" for channel in channels)


def _hex_to_hsv(color: str) -> tuple[float, float, float]:
    red, green, blue = _hex_to_rgb(color)
    return colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    text = color.strip().lstrip("#")
    if len(text) != 6:
        raise LightError(f"colour must be a 6-digit hex value, got {color!r}")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError as exc:
        raise LightError(f"colour must be a 6-digit hex value, got {color!r}") from exc


def _check(result: object) -> dict:
    if isinstance(result, dict) and "Error" in result:
        raise LightError(str(result.get("Error")))
    return result if isinstance(result, dict) else {}

"""Tuya cloud control, used when the LAN path fails.

Slower than LAN (round trip to Tuya's data centre) and useless without internet, so it sits
second in the provider chain. Codes differ between device generations, hence the candidate
lists: newer devices use the *_v2 codes, older ones don't.
"""

from __future__ import annotations

import asyncio
import logging

import tinytuya

from pot.config import LightDevice, Settings
from pot.skills.lights.provider import LightChange, LightError, LightState

log = logging.getLogger(__name__)

SWITCH_CODES = ("switch_led", "switch_led_1", "led_switch")
BRIGHTNESS_CODES = ("bright_value_v2", "bright_value", "bright_value_1")
TEMP_CODES = ("temp_value_v2", "temp_value")
CODE_MAX = 1000


class TuyaCloudProvider:
    name = "cloud"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cloud: tinytuya.Cloud | None = None
        self._codes_seen: dict[str, set[str]] = {}

    def _client(self) -> tinytuya.Cloud:
        if self._cloud is None:
            if not self._settings.has_tuya_cloud:
                raise LightError("TUYA_API_KEY / TUYA_API_SECRET are not set")
            self._cloud = tinytuya.Cloud(
                apiRegion=self._settings.tuya_api_region,
                apiKey=self._settings.tuya_api_key,
                apiSecret=self._settings.tuya_api_secret,
            )
        return self._cloud

    async def apply(self, device: LightDevice, change: LightChange) -> LightState:
        change = change.with_implied_power()
        available = await self._codes(device)
        commands: list[dict] = []

        if change.power is not None:
            commands.append({"code": _pick(SWITCH_CODES, available), "value": change.power})
        if change.power is not False:
            if change.brightness_pct is not None:
                commands.append(
                    {
                        "code": _pick(BRIGHTNESS_CODES, available),
                        "value": _scale(change.brightness_pct),
                    }
                )
            if change.color_temp_pct is not None:
                commands.append(
                    {"code": _pick(TEMP_CODES, available), "value": _scale(change.color_temp_pct)}
                )
        if change.color:
            raise LightError("colour changes are only supported over the local connection")

        if commands:
            result = await asyncio.to_thread(self._client().sendcommand, device.gwid, {"commands": commands})
            _check(result)
        return await self.read(device)

    async def read(self, device: LightDevice) -> LightState:
        result = await asyncio.to_thread(self._client().getstatus, device.gwid)
        payload = _check(result)
        values = {item["code"]: item["value"] for item in payload if isinstance(item, dict)}
        power = _lookup(SWITCH_CODES, values)
        brightness = _lookup(BRIGHTNESS_CODES, values)
        temp = _lookup(TEMP_CODES, values)
        return LightState(
            power=bool(power) if power is not None else None,
            brightness_pct=_unscale(brightness),
            color_temp_pct=_unscale(temp),
            via=self.name,
        )

    async def close(self) -> None:
        self._cloud = None

    async def _codes(self, device: LightDevice) -> set[str]:
        """Which dp codes this device actually exposes, read once per process."""
        if device.gwid not in self._codes_seen:
            result = await asyncio.to_thread(self._client().getstatus, device.gwid)
            payload = _check(result)
            self._codes_seen[device.gwid] = {
                item["code"] for item in payload if isinstance(item, dict) and "code" in item
            }
        return self._codes_seen[device.gwid]


def _pick(candidates: tuple[str, ...], available: set[str]) -> str:
    for code in candidates:
        if code in available:
            return code
    return candidates[0]


def _lookup(candidates: tuple[str, ...], values: dict) -> object | None:
    for code in candidates:
        if code in values:
            return values[code]
    return None


def _scale(percent: int) -> int:
    return max(10, min(CODE_MAX, round(max(1, min(100, percent)) / 100 * CODE_MAX)))


def _unscale(raw: object | None) -> int | None:
    if not isinstance(raw, int):
        return None
    return max(0, min(100, round(raw / CODE_MAX * 100)))


def _check(result: object) -> list:
    """Tuya returns {'success': bool, 'result': ...} or {'Error': ...} from tinytuya itself."""
    if isinstance(result, dict):
        if "Error" in result:
            raise LightError(str(result["Error"]))
        if result.get("success") is False:
            raise LightError(str(result.get("msg") or result))
        payload = result.get("result")
        if isinstance(payload, list):
            return payload
        return []
    raise LightError(f"unexpected cloud response: {result!r}")

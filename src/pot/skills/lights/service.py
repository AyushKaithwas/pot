"""Turning spoken names into device actions.

The voice model says "the tube light" or "bedroom" or "both"; this resolves that to devices,
applies changes concurrently, and reports per-device outcomes so a partial failure is still
useful to the model.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pot.config import LightDevice, Settings, normalize
from pot.runtime import runtime
from pot.skills.lights.provider import LightChange, LightError, LightState, ProviderChain
from pot.skills.lights.tuya_cloud import TuyaCloudProvider
from pot.skills.lights.tuya_local import TuyaLocalProvider

log = logging.getLogger(__name__)

EVERYTHING = {"all", "all lights", "everything", "every light", "lights", "both", "both lights"}


class UnknownLight(LightError):
    pass


@dataclass(frozen=True)
class Outcome:
    device: LightDevice
    state: LightState | None = None
    error: str | None = None

    def describe(self) -> str:
        if self.error:
            return f"{self.device.spoken_name}: failed ({self.error})"
        assert self.state is not None
        via = f" via {self.state.via}" if self.state.via else ""
        return f"{self.device.spoken_name}: {self.state.describe()}{via}"


class LightService:
    def __init__(self, settings: Settings, chain: ProviderChain | None = None) -> None:
        self.settings = settings
        self.devices = settings.lights.devices
        self.chain = chain or _default_chain(settings)

    def resolve(self, target: str) -> list[LightDevice]:
        wanted = normalize(target)
        if not wanted:
            raise UnknownLight("no light was named")
        if wanted in EVERYTHING:
            return list(self.devices.values())

        # Compare without spaces too: speech-to-text hands over "tubelight" for "tube light".
        squashed = wanted.replace(" ", "")
        exact = [
            d
            for d in self.devices.values()
            if wanted in d.match_terms() or squashed in {t.replace(" ", "") for t in d.match_terms()}
        ]
        if exact:
            return exact
        partial = [
            d
            for d in self.devices.values()
            if any(squashed in term.replace(" ", "") for term in d.match_terms())
        ]
        if partial:
            return partial
        known = ", ".join(d.spoken_name for d in self.devices.values()) or "none configured"
        raise UnknownLight(f"no light matches {target!r} (known lights: {known})")

    def resolve_many(self, targets: list[str]) -> list[LightDevice]:
        found: dict[str, LightDevice] = {}
        for target in targets:
            for device in self.resolve(target):
                found[device.id] = device
        return list(found.values())

    async def apply(self, devices: list[LightDevice], change: LightChange) -> list[Outcome]:
        return await self._gather(devices, lambda d: self.chain.apply(d, change))

    async def read(self, devices: list[LightDevice]) -> list[Outcome]:
        return await self._gather(devices, self.chain.read)

    async def apply_scene(self, scene: str) -> list[Outcome]:
        wanted = normalize(scene)
        scenes = {normalize(name): name for name in self.settings.lights.scenes}
        if wanted not in scenes:
            known = ", ".join(self.settings.lights.scenes) or "none configured"
            raise UnknownLight(f"no scene called {scene!r} (known scenes: {known})")
        steps = self.settings.lights.scenes[scenes[wanted]]

        async def run(device_id: str, change: LightChange) -> list[Outcome]:
            return await self.apply(self.resolve(device_id), change)

        results = await asyncio.gather(
            *(run(device_id, LightChange(**spec.model_dump())) for device_id, spec in steps.items()),
            return_exceptions=True,
        )
        outcomes: list[Outcome] = []
        for (device_id, _), result in zip(steps.items(), results, strict=True):
            if isinstance(result, BaseException):
                device = self.devices.get(device_id)
                if device is None:
                    log.warning("scene %s references unknown device %s", scene, device_id)
                    continue
                outcomes.append(Outcome(device, error=str(result)))
            else:
                outcomes.extend(result)
        return outcomes

    async def close(self) -> None:
        await self.chain.close()

    async def _gather(self, devices: list[LightDevice], op) -> list[Outcome]:
        results = await asyncio.gather(*(op(d) for d in devices), return_exceptions=True)
        return [
            Outcome(device, error=str(result))
            if isinstance(result, BaseException)
            else Outcome(device, state=result)
            for device, result in zip(devices, results, strict=True)
        ]


def get_service() -> LightService:
    """The process-wide light service, tied to the current runtime's lifetime."""
    return runtime().resource("lights", lambda rt: LightService(rt.settings))


def _default_chain(settings: Settings) -> ProviderChain:
    providers = [TuyaLocalProvider()]
    if settings.has_tuya_cloud:
        providers.append(TuyaCloudProvider(settings))
    return ProviderChain(providers)

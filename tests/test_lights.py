from __future__ import annotations

import pytest

from pot.config import LightChangeSpec, LightsConfig, Settings
from pot.skills.lights.provider import (
    LightChange,
    LightState,
    LightUnavailable,
    LightUnsupported,
    ProviderChain,
)
from pot.skills.lights.service import LightService, UnknownLight


class FakeProvider:
    def __init__(self, name: str, *, fail: bool = False, unsupported: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.unsupported = unsupported
        self.applied: list[tuple[str, LightChange]] = []

    async def apply(self, device, change):
        if self.unsupported:
            raise LightUnsupported("no colour on this one")
        if self.fail:
            raise RuntimeError("timeout")
        self.applied.append((device.id, change))
        return LightState(power=change.power, brightness_pct=change.brightness_pct, via=self.name)

    async def read(self, device):
        if self.fail:
            raise RuntimeError("timeout")
        return LightState(power=True, brightness_pct=42, via=self.name)

    async def close(self):
        return None


def service_with(settings: Settings, *providers) -> LightService:
    return LightService(settings, ProviderChain(list(providers)))


def test_resolve_by_name_alias_and_room(settings: Settings):
    service = service_with(settings, FakeProvider("fake"))
    assert [d.id for d in service.resolve("Beta 1")] == ["beta1"]
    assert [d.id for d in service.resolve("tube light")] == ["beta1"]
    assert [d.id for d in service.resolve("bedroom")] == ["beta1"]
    assert {d.id for d in service.resolve("all")} == {"beta1", "beta2"}


def test_resolve_unknown_light_lists_known_ones(settings: Settings):
    service = service_with(settings, FakeProvider("fake"))
    with pytest.raises(UnknownLight, match="Beta 1"):
        service.resolve("kitchen spotlight")


def test_resolve_many_deduplicates(settings: Settings):
    service = service_with(settings, FakeProvider("fake"))
    assert [d.id for d in service.resolve_many(["beta1", "tube", "bedroom"])] == ["beta1"]


async def test_chain_falls_back_to_the_next_provider(settings: Settings):
    local, cloud = FakeProvider("local", fail=True), FakeProvider("cloud")
    service = service_with(settings, local, cloud)
    outcomes = await service.apply(service.resolve("beta1"), LightChange(power=True))
    assert outcomes[0].state.via == "cloud"
    assert cloud.applied == [("beta1", LightChange(power=True))]


async def test_chain_raises_when_every_provider_fails(settings: Settings):
    service = service_with(settings, FakeProvider("local", fail=True), FakeProvider("cloud", fail=True))
    outcomes = await service.apply(service.resolve("beta1"), LightChange(power=True))
    assert outcomes[0].error is not None
    assert "unreachable" in outcomes[0].error


async def test_unsupported_capability_is_not_retried_on_the_next_provider(settings: Settings):
    cloud = FakeProvider("cloud")
    chain = ProviderChain([FakeProvider("local", unsupported=True), cloud])
    with pytest.raises(LightUnsupported):
        await chain.apply(settings.lights.devices["beta1"], LightChange(color="ff0000"))
    assert cloud.applied == []


async def test_partial_failure_still_reports_the_working_light(settings: Settings):
    class Picky(FakeProvider):
        async def apply(self, device, change):
            if device.id == "beta2":
                raise RuntimeError("nope")
            return await super().apply(device, change)

    service = service_with(settings, Picky("local"))
    outcomes = await service.apply(service.resolve("all"), LightChange(power=False))
    assert {o.device.id: bool(o.error) for o in outcomes} == {"beta1": False, "beta2": True}


async def test_scene_applies_each_configured_device(settings: Settings):
    settings = settings.model_copy(
        update={
            "lights": LightsConfig(
                devices=settings.lights.devices,
                scenes={"night": {"beta1": LightChangeSpec(power=True, brightness_pct=10)}},
            )
        }
    )
    provider = FakeProvider("local")
    outcomes = await service_with(settings, provider).apply_scene("Night")
    assert provider.applied == [("beta1", LightChange(power=True, brightness_pct=10))]
    assert outcomes[0].state.via == "local"


async def test_unknown_scene_is_rejected(settings: Settings):
    with pytest.raises(UnknownLight, match="no scene"):
        await service_with(settings, FakeProvider("local")).apply_scene("disco")


def test_brightness_implies_power_on():
    assert LightChange(brightness_pct=30).with_implied_power().power is True
    assert LightChange(power=False).with_implied_power().power is False
    assert LightChange().with_implied_power().power is None


async def test_empty_chain_reports_no_providers(settings: Settings):
    with pytest.raises(LightUnavailable):
        await ProviderChain([]).apply(settings.lights.devices["beta1"], LightChange(power=True))

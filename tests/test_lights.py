from __future__ import annotations

import pytest

from pot.config import LightChangeSpec, LightsConfig, Settings
from pot.skills.lights.provider import (
    NEUTRAL_WHITE_PCT,
    LightChange,
    LightState,
    LightUnavailable,
    LightUnsupported,
    ProviderChain,
)
from pot.skills.lights.service import LightService, UnknownLight
from pot.skills.lights.tuya_cloud import _agrees, _confirmed


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


def test_resolve_survives_speech_glued_words(settings: Settings):
    """The realtime model hands over "tubelight" when the user said "tube light"."""
    service = service_with(settings, FakeProvider("fake"))
    assert [d.id for d in service.resolve("tubelight")] == ["beta1"]
    assert [d.id for d in service.resolve("bedroomlight")] == ["beta1"]


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


def test_white_is_a_temperature_not_a_colour():
    """Sent as a hex colour it leaves the bulb in colour mode at zero saturation: dim grey.

    This is what made "white at 1 percent" come out as 1 percent of nothing.
    """
    change, note = LightChange(color="ffffff", brightness_pct=1).resolved()
    assert change.color is None
    assert change.color_temp_pct == NEUTRAL_WHITE_PCT
    assert change.brightness_pct == 1, "the brightness still has to survive"
    assert note is None, "it did what was asked, so there's nothing to explain"


def test_a_stated_white_temperature_wins_over_plain_white():
    change, _ = LightChange(color="ffffff", color_temp_pct=10).resolved()
    assert (change.color, change.color_temp_pct) == (None, 10)


def test_near_white_counts_as_white():
    """ "Warm white" arrives as a barely tinted hex, and it belongs on the same scale."""
    change, _ = LightChange(color="fffbf0").resolved()
    assert change.color is None


def test_a_real_colour_is_left_alone():
    for colour in ("ff0000", "00ff00", "0000ff", "ff8800"):
        change, _ = LightChange(color=colour, brightness_pct=50).resolved()
        assert change.color == colour, f"{colour} is a colour, not white"
        assert change.color_temp_pct is None


def test_a_pale_colour_is_still_a_colour():
    """The line has to sit above tinted white and below anything you'd name as a colour."""
    change, _ = LightChange(color="ffd0d0").resolved()
    assert change.color == "ffd0d0", "pale pink is a colour, not white"


def test_warm_white_stays_warm():
    """ "Warm white" as a hex carries its warmth in the tint; neutralising it would lose that."""
    warm, _ = LightChange(color="fff4e0").resolved()
    cool, _ = LightChange(color="e0f0ff").resolved()
    plain, _ = LightChange(color="ffffff").resolved()

    assert warm.color_temp_pct < plain.color_temp_pct < cool.color_temp_pct
    assert plain.color_temp_pct == NEUTRAL_WHITE_PCT


def test_the_cloud_reports_what_it_set_not_a_status_that_lags():
    """The bug: "brightness to a hundred" answered with "on at 1 percent", the previous value.

    Tuya's cloud status is eventually consistent, so the read straight after a command still holds
    the old values, and Pot is told to trust the tool over its own expectation.
    """
    stale = LightState(power=True, brightness_pct=1, color_temp_pct=50, via="cloud")
    asked = LightChange(power=True, brightness_pct=100)

    confirmed = _confirmed(stale, asked)
    assert confirmed.brightness_pct == 100, "it was just told to be 100"
    assert confirmed.color_temp_pct == 50, "nothing was said about temperature, so keep the device's"
    assert confirmed.via == "cloud"


def test_an_unchanged_field_is_never_invented():
    stale = LightState(power=True, brightness_pct=40, via="cloud")
    confirmed = _confirmed(stale, LightChange(color_temp_pct=20))
    assert confirmed.brightness_pct == 40
    assert confirmed.power is True


def test_a_status_that_already_agrees_needs_no_second_look():
    state = LightState(power=True, brightness_pct=100, color_temp_pct=50, via="cloud")
    assert _agrees(state, LightChange(power=True, brightness_pct=100))


def test_a_lagging_status_is_spotted():
    """1% right after being told 100% is the cloud lagging, not the light disobeying."""
    stale = LightState(power=True, brightness_pct=1, color_temp_pct=50, via="cloud")
    assert not _agrees(stale, LightChange(power=True, brightness_pct=100))


def test_fields_nobody_asked_about_do_not_count_as_disagreement():
    state = LightState(power=True, brightness_pct=40, color_temp_pct=80, via="cloud")
    assert _agrees(state, LightChange(power=True))


def test_an_impossible_brightness_cannot_be_reported_as_fact():
    """The bug: asked for 200, the light went to its maximum, and Pot said "200 percent".

    Nothing clamped at the edge, so the out-of-range request stayed in the change and the
    confirmation echoed it back as though the light were sitting at 200.
    """
    change, note = LightChange(power=True, brightness_pct=200).resolved()
    assert change.brightness_pct == 100
    assert note, "silently changing what was asked for is how the wrong number gets spoken"

    device = LightState(power=True, brightness_pct=100, color_temp_pct=50, via="cloud")
    assert _agrees(device, change), "the light is at 100 and 100 is what was asked for"
    assert _confirmed(device, change).brightness_pct == 100


def test_brightness_below_the_dimmest_setting_lands_on_it():
    change, note = LightChange(brightness_pct=0).resolved()
    assert change.brightness_pct == 1, "these lights have no zero; off is off"
    assert note


def test_an_out_of_range_temperature_is_clamped_too():
    change, note = LightChange(color_temp_pct=140).resolved()
    assert (change.color_temp_pct, bool(note)) == (100, True)


def test_a_normal_request_is_untouched_and_unremarked():
    change, note = LightChange(power=True, brightness_pct=10, color_temp_pct=0).resolved()
    assert (change.brightness_pct, change.color_temp_pct, note) == (10, 0, None)


def test_a_deferred_change_lands_in_the_persistent_store(tmp_path):
    """ "Lights off in ten minutes" used to be an in-memory task, gone with the process. As a
    silent timer it survives a restart, shows in list_alarms, and cancels by voice."""
    from pot.alarms import AlarmStore
    from pot.config import LightDevice
    from pot.runtime import Runtime, set_runtime
    from pot.skills.lights import _defer
    from pot.skills.time.service import TimeService

    rt = Runtime(Settings())
    set_runtime(rt)
    service = rt.resource(
        "time", lambda r: TimeService(r.settings, store=AlarmStore(tmp_path / "alarms.json"))
    )
    device = LightDevice(id="beta1", gwid="x", name="Beta 1")

    spoken = _defer([device], LightChange(power=True, brightness_pct=30), after_seconds=600)

    stored = AlarmStore(tmp_path / "alarms.json").sorted()
    assert len(stored) == 1, "a fresh read of the file proves it was persisted, not just scheduled"
    assert stored[0].ring is False, "a deferred change is a job with a time on it, not an alarm"
    assert "set_lights" in stored[0].then and "Beta 1" in stored[0].then
    assert "30" in spoken or "brightness" in spoken
    assert service.pending(), "and the live service sees it too"

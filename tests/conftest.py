from __future__ import annotations

import pytest

from pot.config import LightDevice, LightsConfig, Settings
from pot.runtime import Runtime, set_runtime


@pytest.fixture
def beta1() -> LightDevice:
    return LightDevice(
        id="beta1",
        gwid="bf00000000000000beta1",
        name="Beta 1",
        room="bedroom",
        kind="batten",
        aliases=["tube light", "tube"],
        local_key="fake-key",
        version=3.5,
    )


@pytest.fixture
def beta2() -> LightDevice:
    return LightDevice(id="beta2", gwid="bf00000000000000beta2", name="Beta 2", kind="batten")


@pytest.fixture
def settings(beta1: LightDevice, beta2: LightDevice) -> Settings:
    return Settings(
        openai_api_key="test",
        lights=LightsConfig(devices={"beta1": beta1, "beta2": beta2}),
    )


@pytest.fixture(autouse=True)
def clean_runtime():
    yield
    set_runtime(None)


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    value = Runtime(settings)
    set_runtime(value)
    return value

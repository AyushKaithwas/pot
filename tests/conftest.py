from __future__ import annotations

import os
import tempfile

# Point config, secrets and state at a throwaway directory before anything reads them. Settings
# resolves its env file when the class is defined, so this has to happen before that import: a
# test run that picks up a real ~/.config/pot/env behaves differently on every machine, and puts
# somebody's API key into pytest's failure output.
_SANDBOX = tempfile.mkdtemp(prefix="pot-tests-")
os.environ["POT_CONFIG"] = f"{_SANDBOX}/pot.toml"
os.environ["POT_ENV"] = f"{_SANDBOX}/env"
os.environ["POT_STATE_DIR"] = f"{_SANDBOX}/state"

import pytest  # noqa: E402

from pot.config import LightDevice, LightsConfig, Settings  # noqa: E402
from pot.runtime import Runtime, set_runtime  # noqa: E402


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

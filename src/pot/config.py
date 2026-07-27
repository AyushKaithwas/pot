"""Configuration: secrets from .env, everything else from config/pot.toml.

Device local keys live in config/devices.json (written by `pot lights keys`) and are merged
onto the device entries by gwId, so pot.toml stays safe to commit.
"""

from __future__ import annotations

import json
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pot.usage import Rates

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "pot.toml"
DEVICES_FILE = CONFIG_DIR / "devices.json"


class AssistantConfig(BaseModel):
    name: str = "Pot"
    extra_instructions: str = ""


class VoiceConfig(BaseModel):
    model: str = "gpt-realtime-2.1-mini"
    voice: str = "ash"
    # Transcribing your own speech is billed per minute and only feeds the terminal transcript,
    # so it's off by default. Pot's own side is transcribed for free either way.
    transcribe_input: bool = False
    transcription_model: str = "gpt-4o-mini-transcribe"
    turn_detection: Literal["semantic_vad", "server_vad"] = "semantic_vad"
    reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    # Audio output is the most expensive thing here, so cap a runaway monologue.
    max_output_tokens: int = 800


class AudioConfig(BaseModel):
    mode: Literal["push_to_talk", "always_on"] = "push_to_talk"
    # Reopen the mic by itself once Pot stops talking, so a conversation needs one keypress
    # rather than one per turn. Turn off if the speakers make Pot interrupt itself.
    auto_reopen: bool = True
    sample_rate: int = 24000
    input_device: str = ""
    output_device: str = ""


class SkillsConfig(BaseModel):
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class ResearchConfig(BaseModel):
    """Most spoken questions are simple lookups, so the default model is a cheap one."""

    model: str = "gpt-5.4-nano"
    deep_model: str = "gpt-5.6"
    search_context_size: Literal["low", "medium", "high"] = "low"
    deep_search_context_size: Literal["low", "medium", "high"] = "medium"


class CostConfig(BaseModel):
    show_per_turn: bool = True
    # Dollars per million tokens, keyed by model id. Update when OpenAI's prices change.
    models: dict[str, Rates] = Field(default_factory=dict)


class LightChangeSpec(BaseModel):
    """One device's target state inside a scene."""

    power: bool | None = None
    brightness_pct: int | None = None
    color_temp_pct: int | None = None
    color: str | None = None


class LightDevice(BaseModel):
    id: str
    gwid: str
    name: str
    room: str = ""
    kind: str = "light"
    aliases: list[str] = Field(default_factory=list)
    local_key: str | None = None
    version: float | None = None

    @property
    def spoken_name(self) -> str:
        return self.name or self.id

    def match_terms(self) -> set[str]:
        terms = {self.id, self.name, *self.aliases}
        if self.room:
            terms |= {self.room, f"{self.room} light", f"{self.room} lights"}
        return {normalize(t) for t in terms if t}


class LightsConfig(BaseModel):
    devices: dict[str, LightDevice] = Field(default_factory=dict)
    scenes: dict[str, dict[str, LightChangeSpec]] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = ""
    tuya_api_key: str = ""
    tuya_api_secret: str = ""
    tuya_api_region: str = "in"

    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    lights: LightsConfig = Field(default_factory=LightsConfig)

    @property
    def has_tuya_cloud(self) -> bool:
        return bool(self.tuya_api_key and self.tuya_api_secret)


def normalize(text: str) -> str:
    """Fold spoken variants together: lowercase, punctuation dropped, whitespace collapsed."""
    kept = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace())
    return " ".join(kept.split())


def load_settings(config_file: Path | None = None, devices_file: Path | None = None) -> Settings:
    config_file = config_file or CONFIG_FILE
    devices_file = devices_file or DEVICES_FILE

    data: dict = {}
    if config_file.exists():
        data = tomllib.loads(config_file.read_text())

    lights_raw = dict(data.pop("lights", {}))
    device_entries = lights_raw.pop("devices", {})
    scenes = lights_raw.pop("scenes", {})
    credentials = _load_credentials(devices_file)

    devices: dict[str, LightDevice] = {}
    for key, entry in device_entries.items():
        cred = credentials.get(entry.get("gwid", ""), {})
        devices[key] = LightDevice(
            id=key,
            local_key=cred.get("local_key"),
            version=cred.get("version"),
            **entry,
        )

    return Settings(**data, lights=LightsConfig(devices=devices, scenes=scenes))


def _load_credentials(devices_file: Path) -> dict[str, dict]:
    """Read tinytuya-style devices.json into {gwid: {local_key, version}}."""
    if not devices_file.exists():
        return {}
    try:
        raw = json.loads(devices_file.read_text())
    except json.JSONDecodeError:
        return {}
    entries = raw.get("devices", raw) if isinstance(raw, dict) else raw
    out: dict[str, dict] = {}
    for item in entries:
        gwid = item.get("id") or item.get("gwid")
        if not gwid:
            continue
        version = item.get("version") or item.get("ver")
        out[gwid] = {
            "local_key": item.get("key") or item.get("local_key"),
            "version": float(version) if version else None,
        }
    return out


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()

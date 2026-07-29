"""Configuration, layered so an installed copy owns none of its own settings.

Your settings live in ~/.config/pot/pot.toml and are merged over the defaults shipped with the
source, so an update never overwrites them and the repo carries nobody's device ids. Secrets come
from ~/.config/pot/env (or a .env beside the source, for development). Device keys and tokens go
to ~/.local/state/pot, which is machine-specific and never in a backup of the config.

POT_CONFIG, POT_ENV and POT_STATE_DIR override the locations, which is what the tests use.
"""

from __future__ import annotations

import json
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pot.usage import Rates

REPO_ROOT = Path(__file__).resolve().parents[2]
# Shipped defaults. Editing this file is fine for development; an install shouldn't have to.
DEFAULTS_FILE = REPO_ROOT / "config" / "pot.toml"


def _xdg(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / fallback)


def user_config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "pot"


def state_dir() -> Path:
    return _xdg("POT_STATE_DIR", ".local/state/pot")


def config_file() -> Path:
    return Path(os.environ["POT_CONFIG"]) if os.environ.get("POT_CONFIG") else user_config_dir() / "pot.toml"


def env_file() -> Path:
    """Secrets. The user config dir wins; a repo .env keeps development convenient."""
    if os.environ.get("POT_ENV"):
        return Path(os.environ["POT_ENV"])
    candidate = user_config_dir() / "env"
    return candidate if candidate.exists() else REPO_ROOT / ".env"


def devices_file() -> Path:
    """Tuya local keys, written by `pot lights keys`. Machine state, not config."""
    return state_dir() / "devices.json"


class AssistantConfig(BaseModel):
    name: str = "Pot"
    extra_instructions: str = ""


class VoiceConfig(BaseModel):
    model: str = "gpt-realtime-2.1-mini"
    voice: str = "ash"
    # Slightly quicker than natural: a home assistant's answers are short, and waiting through
    # an unhurried reading of "the light is on" gets old.
    speed: float = Field(default=1.15, ge=0.25, le=1.5)
    # Transcribing your own speech is billed per minute and only feeds the terminal transcript,
    # so it's off by default. Pot's own side is transcribed for free either way.
    transcribe_input: bool = False
    transcription_model: str = "gpt-4o-mini-transcribe"
    turn_detection: Literal["semantic_vad", "server_vad"] = "semantic_vad"
    reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    # Audio output is the most expensive thing here, so cap a runaway monologue.
    max_output_tokens: int = 800
    # True keeps Pot answerable while a slow tool runs, at the price of reporting each result
    # separately (more words, ~30% more cost on multi-tool turns). False gives one tighter reply
    # but leaves Pot deaf until the tool lands, which on a 30s search is a long time.
    async_tool_calls: bool = True


class WakeConfig(BaseModel):
    """Local wake word. Costs nothing at rest: audio only reaches OpenAI once this fires."""

    # Stock model names (hey_mycroft, alexa, hey_jarvis, hey_rhasspy) or a path to an .onnx model
    # trained on your own voice. Any listed trigger firing wakes Pot, and each costs about 1 ms
    # per 80 ms of audio, so listing several is a cheap way to be heard more often.
    #
    # The stock models are trained on synthetic speech and differ a lot on real voices. Measured
    # across three voices at three volumes: hey_mycroft fired 9/9 (median 1.000), alexa 9/9
    # (0.931), hey_jarvis only 6/9 with 0.14 on one voice. `pot audio benchmark` measures them
    # against your voice, which is the only comparison that counts.
    models: list[str] = Field(default_factory=list)
    threshold: float = Field(default=0.5, ge=0.05, le=0.99)
    # Detection lands a few hundred ms after the word finishes, by which point you're already
    # saying the request. Replaying this much recent audio stops the first syllables being lost.
    # Detection completes as the wake word ends, so this only has to cover the detector being a
    # fraction late. Any longer and it replays the wake word itself, which then gets transcribed
    # as a turn of its own and answered ("Mycroft." -> "Your MacBook Pro is available").
    preroll_seconds: float = Field(default=0.2, ge=0.0, le=3.0)
    # How long you can stay quiet before Pot stops listening and waits for the word again.
    # Timed locally: the API rejects turn_detection.idle_timeout_ms.
    conversation_idle_seconds: int = Field(default=15, ge=2, le=120)

    @field_validator("models", mode="before")
    @classmethod
    def _accept_a_single_name(cls, value: object) -> object:
        """`models = "alexa"` is the obvious thing to write, so accept it."""
        return [value] if isinstance(value, str) else value

    @property
    def spoken(self) -> str:
        """How to tell someone what to say."""
        return " or ".join(name.replace("_", " ") for name in self.models) or "the wake word"


class AudioConfig(BaseModel):
    mode: Literal["push_to_talk", "always_on", "wake_word"] = "push_to_talk"
    # Reopen the mic by itself once Pot stops talking, so a conversation needs one keypress
    # rather than one per turn. Turn off if the speakers make Pot interrupt itself.
    auto_reopen: bool = True
    # Earcons for listening, working and errors. The only status channel on a device with no
    # screen, so they stay on by default.
    cues: bool = True
    cue_volume: float = 0.2
    # Subtract Pot's own playback from what the wake word detector hears, using what actually left
    # the speaker as the reference. Only affects the detector's copy, never what Pot sends to the
    # model. This covers music too, as long as it's playing through Pot's own Spotify speaker;
    # music from another app or device has no reference and can't be removed.
    echo_suppression: bool = True
    sample_rate: int = 24000
    # How much of Pot's voice to gather before playing any of it. The default suits a wired or
    # quiet network; raise it wherever the round trip is uneven, since a jitter spike longer than
    # this empties the buffer mid-word and is heard as a click. Measured on a Pi over Wi-Fi:
    # 7ms best, 147ms worst, which needs roughly 300.
    playback_buffer_ms: int = Field(default=120, ge=20, le=1000)
    input_device: str = ""
    output_device: str = ""
    wake: WakeConfig = Field(default_factory=WakeConfig)

    @property
    def uses_wake_word(self) -> bool:
        return self.mode == "wake_word"

    @property
    def mic_open_at_start(self) -> bool:
        """Only always-on starts sending audio immediately; the others wait for a trigger."""
        return self.mode == "always_on"


class SkillsConfig(BaseModel):
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class ResearchConfig(BaseModel):
    """Most spoken questions are simple lookups, so the default model is a cheap one."""

    model: str = "gpt-5.4-nano"
    deep_model: str = "gpt-5.6"
    search_context_size: Literal["low", "medium", "high"] = "low"
    deep_search_context_size: Literal["low", "medium", "high"] = "medium"


class SpotifyConfig(BaseModel):
    # Each user registers their own Spotify app: development-mode apps are capped at five
    # authenticated users, so a single shared Pot app could never serve everyone.
    client_id: str = ""
    redirect_port: int = 8765

    def token_path(self) -> Path:
        return state_dir() / "spotify-token.json"


class SpeakerConfig(BaseModel):
    """Pot as a Spotify Connect device, via librespot. Off unless asked for."""

    enabled: bool = False
    name: str = "Pot"
    binary: str = "librespot"
    bitrate: int = 160

    def cache_path(self) -> Path:
        return state_dir() / "librespot"


class MusicConfig(BaseModel):
    provider: str = "spotify"
    preferred_device: str = ""
    duck_to_percent: int = 20
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)


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


class ServiceConfig(BaseModel):
    """Where model access comes from.

    Empty means straight to OpenAI with your own key, which is what an install from source does and
    what it will keep doing. Pointing this at a relay that holds the keys is the only change needed
    for a hosted setup: the models are reached the same way either way, so nothing above this cares
    which it is. The token is a secret, so it comes from the environment rather than from here.
    """

    # Where to reach an OpenAI-compatible API, e.g. "https://api.example.com/v1".
    base_url: str = ""
    # The realtime websocket, when it isn't derived from base_url.
    realtime_url: str = ""

    @property
    def hosted(self) -> bool:
        return bool(self.base_url or self.realtime_url)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        # So a field with an environment alias can still be set by its own name, which is what
        # every caller in the tests does.
        populate_by_name=True,
    )

    openai_api_key: str = ""
    # Stands in for the OpenAI key when Pot is pointed at a service instead.
    service_token: str = Field(default="", validation_alias="POT_SERVICE_TOKEN")
    tuya_api_key: str = ""
    tuya_api_secret: str = ""
    tuya_api_region: str = "in"

    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    lights: LightsConfig = Field(default_factory=LightsConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)

    @property
    def has_tuya_cloud(self) -> bool:
        return bool(self.tuya_api_key and self.tuya_api_secret)

    @property
    def model_token(self) -> str:
        """Whatever authorises a model request: your own OpenAI key, or a service token."""
        return self.service_token if self.service.hosted else self.openai_api_key

    @property
    def has_model_access(self) -> bool:
        return bool(self.model_token)

    def realtime_config(self) -> dict[str, object]:
        """Connection settings for the realtime session.

        The key is passed explicitly because pydantic-settings reads .env without exporting it, so
        the SDK's own environment lookup would find nothing.
        """
        config: dict[str, object] = {"api_key": self.model_token}
        url = self.service.realtime_url or _websocket_url(self.service.base_url)
        if url:
            config["url"] = url
        return config

    def openai_client_args(self) -> dict[str, object]:
        """Constructor arguments for the plain OpenAI client, used by the research skill."""
        args: dict[str, object] = {"api_key": self.model_token or None}
        if self.service.base_url:
            args["base_url"] = self.service.base_url
        return args


def _websocket_url(base_url: str) -> str:
    """The realtime endpoint implied by an HTTP base url, so only one has to be configured."""
    if not base_url:
        return ""
    scheme, _, rest = base_url.partition("://")
    websocket = {"http": "ws", "https": "wss"}.get(scheme, scheme)
    return f"{websocket}://{rest.rstrip('/')}/realtime"


def normalize(text: str) -> str:
    """Fold spoken variants together: lowercase, punctuation dropped, whitespace collapsed."""
    kept = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace())
    return " ".join(kept.split())


def deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins, but nested tables merge rather than replace.

    Without this, setting one thing under [lights] in your own config would silently drop every
    device and scene that came from the defaults.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def read_config(defaults: Path | None = None, user: Path | None = None) -> dict[str, Any]:
    defaults = DEFAULTS_FILE if defaults is None else defaults
    user = config_file() if user is None else user
    data: dict[str, Any] = tomllib.loads(defaults.read_text()) if defaults.exists() else {}
    if user.exists() and user != defaults:
        data = deep_merge(data, tomllib.loads(user.read_text()))
    return data


def load_settings(config_path: Path | None = None, keys_path: Path | None = None) -> Settings:
    data = read_config(user=config_path)
    keys_path = devices_file() if keys_path is None else keys_path

    lights_raw = dict(data.pop("lights", {}))
    device_entries = lights_raw.pop("devices", {})
    scenes = lights_raw.pop("scenes", {})
    credentials = _load_credentials(keys_path)

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


def _load_credentials(path: Path) -> dict[str, dict]:
    """Read tinytuya-style devices.json into {gwid: {local_key, version}}."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
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

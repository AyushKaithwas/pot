"""Spotify as a music provider.

The interesting part is picking where to play, which is what makes this feel like an Echo
rather than a remote control: Pot's own speaker first, then whatever the user asked for, then
whatever is already awake.

Every public method translates Spotify's errors into MusicError/MusicUnavailable, so callers
(the skill, the CLI) never see a raw SpotifyError, and state complaints like "Restriction
violated" become sentences instead of stack traces.
"""

from __future__ import annotations

import functools
import logging

from pot.config import Settings, normalize
from pot.skills.music.provider import MusicError, MusicUnavailable, Speaker
from pot.skills.music.spotify.auth import SpotifyAuth, SpotifyAuthError, TokenStore
from pot.skills.music.spotify.client import (
    Device,
    NoActiveDevice,
    Playable,
    PremiumRequired,
    RestrictionViolated,
    SpotifyClient,
    SpotifyError,
)
from pot.skills.music.spotify.speaker import ManagedSpeaker

log = logging.getLogger(__name__)

DEFAULT_KIND = "track"
TRACK_QUEUE_SIZE = 5

NOTHING_TO_PLAY_ON = (
    "no Spotify device is available. Open Spotify on your phone or computer, or switch on Pot's own speaker"
)


def _translated(fn):
    """Turn Spotify's failure modes into the provider-agnostic ones."""

    @functools.wraps(fn)
    async def inner(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (MusicError, MusicUnavailable):
            raise
        except SpotifyAuthError as exc:
            raise MusicUnavailable(str(exc)) from exc
        except PremiumRequired as exc:
            raise MusicUnavailable(str(exc)) from exc
        except NoActiveDevice as exc:
            raise MusicUnavailable(NOTHING_TO_PLAY_ON) from exc
        except SpotifyError as exc:
            raise MusicError(str(exc)) from exc

    return inner


class SpotifyProvider:
    name = "spotify"

    def __init__(
        self,
        settings: Settings,
        *,
        client: SpotifyClient | None = None,
        speaker: ManagedSpeaker | None = None,
    ) -> None:
        self.settings = settings
        music = settings.music
        self._auth = SpotifyAuth(
            client_id=music.spotify.client_id,
            redirect_port=music.spotify.redirect_port,
            store=TokenStore(music.spotify.token_path()),
        )
        self._client = client or SpotifyClient(self._auth)
        self._speaker = speaker if speaker is not None else ManagedSpeaker(settings)

    @property
    def auth(self) -> SpotifyAuth:
        return self._auth

    @_translated
    async def speakers(self) -> list[Speaker]:
        pot_name = normalize(self.settings.music.speaker.name)
        return [_speaker(device, pot_name) for device in await self._devices()]

    @_translated
    async def play(self, query: str | None, kind: str, speaker: Speaker | None = None) -> str:
        target = speaker or await self.pick_speaker()
        if query is None:
            try:
                await self._client.play(device_id=target.id or None)
            except RestrictionViolated:
                return "it's already playing"
            return f"resumed on {target.name}"

        playables = await self._find(query, kind)
        await self._client.play(playables, device_id=target.id or None)
        return f"playing {playables[0].describe()} on {target.name}"

    @_translated
    async def pause(self) -> str:
        try:
            await self._client.pause()
        except RestrictionViolated:
            return "it was already paused"
        return "paused"

    async def resume(self) -> str:
        return await self.play(None, DEFAULT_KIND)

    @_translated
    async def skip(self, backwards: bool = False) -> str:
        try:
            if backwards:
                await self._client.previous_track()
            else:
                await self._client.next_track()
        except RestrictionViolated:
            return "there's nothing further back" if backwards else "nothing is queued after this"
        playing = await self._client.now_playing()
        return playing.describe() if playing else ("went back" if backwards else "skipped")

    @_translated
    async def set_volume(self, percent: int) -> None:
        await self._client.set_volume(percent)

    @_translated
    async def volume(self) -> int | None:
        playing = await self._client.now_playing()
        return playing.volume_percent if playing else None

    @_translated
    async def now_playing(self) -> str:
        playing = await self._client.now_playing()
        return playing.describe() if playing else "nothing is playing"

    async def pick_speaker(self) -> Speaker:
        """Pot's own speaker, then the configured favourite, then whatever is awake."""
        devices = await self._devices()
        pot_name = normalize(self.settings.music.speaker.name)
        speakers = [_speaker(device, pot_name) for device in devices]
        if not speakers:
            raise MusicUnavailable(NOTHING_TO_PLAY_ON)

        if self._speaker.running:
            for candidate in speakers:
                if candidate.is_pot:
                    return candidate

        preferred = normalize(self.settings.music.preferred_device)
        if preferred:
            for candidate in speakers:
                if normalize(candidate.name) == preferred:
                    return candidate

        for candidate in speakers:
            if candidate.is_active:
                return candidate
        return speakers[0]

    def resolve(self, speakers: list[Speaker], wanted: str) -> Speaker:
        target = normalize(wanted)
        for candidate in speakers:
            if normalize(candidate.name) == target:
                return candidate
        for candidate in speakers:
            if target in normalize(candidate.name):
                return candidate
        known = ", ".join(candidate.name for candidate in speakers) or "none"
        raise MusicError(f"no speaker called {wanted!r} (available: {known})")

    async def start_speaker(self) -> None:
        await self._speaker.start()

    async def close(self) -> None:
        await self._speaker.stop()
        await self._client.close()

    async def _devices(self) -> list[Device]:
        try:
            return await self._client.devices()
        except NoActiveDevice:
            return []

    async def _find(self, query: str, kind: str) -> list[Playable]:
        kind = (kind or DEFAULT_KIND).strip().lower()
        # For plain tracks, take a handful so "skip" has somewhere to go.
        limit = TRACK_QUEUE_SIZE if kind == DEFAULT_KIND else 1
        matches = await self._client.search(query, kind, limit=limit)
        if not matches:
            raise MusicError(f"nothing on Spotify matches {query!r}")
        return matches


def _speaker(device: Device, pot_name: str) -> Speaker:
    return Speaker(
        id=device.id,
        name=device.name,
        kind=device.kind,
        is_active=device.is_active,
        volume_percent=device.volume_percent,
        supports_volume=device.supports_volume,
        is_pot=bool(pot_name) and normalize(device.name) == pot_name,
    )

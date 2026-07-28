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
    clock,
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
        output: object | None = None,
    ) -> None:
        self.settings = settings
        music = settings.music
        self._auth = SpotifyAuth(
            client_id=music.spotify.client_id,
            redirect_port=music.spotify.redirect_port,
            store=TokenStore(music.spotify.token_path()),
        )
        self._client = client or SpotifyClient(self._auth)
        self._speaker = speaker if speaker is not None else ManagedSpeaker(settings, output)

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
    async def seek(self, seconds: int, relative: bool = True) -> str:
        """Jump within the track. Relative for "forward twenty seconds", absolute for "from 1:30"."""
        playing = await self._client.now_playing()
        if playing is None:
            raise MusicError("nothing is playing")
        target_ms = seconds * 1000
        if relative:
            target_ms += playing.position_ms or 0
        if playing.duration_ms is not None:
            # Past the end is a skip as far as Spotify is concerned, which is rarely what was meant.
            target_ms = min(target_ms, playing.duration_ms - 1000)
        target_ms = max(0, target_ms)
        await self._client.seek(target_ms)
        return f"at {clock(target_ms)} of {playing.title}"

    @_translated
    async def set_shuffle(self, on: bool) -> str:
        await self._client.set_shuffle(on)
        return "shuffling" if on else "playing in order"

    @_translated
    async def set_repeat(self, mode: str) -> str:
        await self._client.set_repeat(mode)
        return {
            "off": "repeat off",
            "track": "repeating this track",
            "context": "repeating the whole thing",
        }[mode]

    @_translated
    async def queue(self, query: str, kind: str = DEFAULT_KIND) -> str:
        playables = await self._find(query, kind)
        await self._client.add_to_queue(playables[0].uri)
        return f"queued {playables[0].describe()}"

    @_translated
    async def upcoming(self) -> str:
        items = await self._client.queue()
        if not items:
            return "nothing is queued after this"
        return "next up: " + "; ".join(item.describe() for item in items)

    @_translated
    async def recent(self) -> str:
        items = await self._client.recently_played()
        if not items:
            return "nothing has played recently"
        return "recently: " + "; ".join(item.describe() for item in items)

    @_translated
    async def save_current(self, remove: bool = False) -> str:
        playing = await self._client.now_playing()
        if playing is None or not playing.uri:
            raise MusicError("nothing is playing")
        track_id = playing.uri.rsplit(":", 1)[-1]
        if not remove and await self._client.saved(track_id):
            return f"{playing.title} is already in your library"
        await self._client.save(track_id, remove=remove)
        verb = "removed from" if remove else "saved to"
        return f"{playing.title} {verb} your library"

    @_translated
    async def add_current_to_playlist(self, playlist: str) -> str:
        playing = await self._client.now_playing()
        if playing is None or not playing.uri:
            raise MusicError("nothing is playing")
        wanted = normalize(playlist)
        matches = [p for p in await self._client.playlists() if wanted in normalize(p.name)]
        if not matches:
            raise MusicError(f"you have no playlist called {playlist!r}")
        await self._client.add_to_playlist(matches[0].uri.rsplit(":", 1)[-1], playing.uri)
        return f"added {playing.title} to {matches[0].name}"

    @_translated
    async def move(self, speaker: Speaker) -> str:
        await self._client.transfer(speaker.id, play=True)
        return f"moved to {speaker.name}"

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

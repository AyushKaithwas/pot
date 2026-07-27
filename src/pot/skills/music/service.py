"""Music operations, and the volume ducking that lets Pot talk over music.

Ducking is why this is a service rather than a bare provider: Pot and the music share a speaker,
so the assistant's own speech has to push the music down and put it back exactly where it was.
"""

from __future__ import annotations

import asyncio
import logging

from pot.config import Settings
from pot.runtime import Runtime, runtime
from pot.skills.music.provider import MusicError, MusicProvider, MusicUnavailable, Speaker
from pot.skills.music.spotify.provider import SpotifyProvider

log = logging.getLogger(__name__)


class MusicService:
    def __init__(self, settings: Settings, provider: MusicProvider | None = None) -> None:
        self.settings = settings
        self.provider = provider or _build_provider(settings)
        self._ducked_from: int | None = None
        self._duck_lock = asyncio.Lock()

    async def speakers(self) -> list[Speaker]:
        return await self.provider.speakers()

    async def play(self, query: str | None, kind: str = "track", speaker_name: str = "") -> str:
        speaker = None
        if speaker_name:
            speaker = self.provider.resolve(await self.speakers(), speaker_name)
        return await self.provider.play(query, kind, speaker)

    async def pause(self) -> str:
        return await self.provider.pause()

    async def resume(self) -> str:
        return await self.provider.resume()

    async def skip(self, backwards: bool = False) -> str:
        return await self.provider.skip(backwards)

    async def now_playing(self) -> str:
        return await self.provider.now_playing()

    async def set_volume(self, percent: int) -> str:
        percent = max(0, min(100, percent))
        await self.provider.set_volume(percent)
        # A manual volume change during ducking is the user's intent, not ours to undo.
        self._ducked_from = None
        return f"volume {percent}%"

    async def duck(self) -> None:
        """Drop the music while Pot speaks."""
        async with self._duck_lock:
            if self._ducked_from is not None:
                return
            target = self.settings.music.duck_to_percent
            try:
                current = await self.provider.volume()
                if current is None or current <= target:
                    return
                await self.provider.set_volume(target)
            except (MusicError, MusicUnavailable) as exc:
                log.debug("could not duck the music: %s", exc)
                return
            self._ducked_from = current

    async def unduck(self) -> None:
        """Put it back exactly where it was."""
        async with self._duck_lock:
            previous, self._ducked_from = self._ducked_from, None
            if previous is None:
                return
            try:
                await self.provider.set_volume(previous)
            except (MusicError, MusicUnavailable) as exc:
                log.debug("could not restore the music volume: %s", exc)

    async def start_speaker(self) -> None:
        starter = getattr(self.provider, "start_speaker", None)
        if starter is not None:
            await starter()

    async def close(self) -> None:
        await self.provider.close()


def get_service() -> MusicService:
    """The process-wide music service, tied to the current runtime's lifetime."""
    return runtime().resource("music", _create)


def _create(rt: Runtime) -> MusicService:
    service = MusicService(rt.settings)
    # Music ducks while Pot talks, while Pot listens for a request, and while an alarm rings; the
    # session broadcasts all three and skills subscribe, so core never imports a skill.
    reasons: set[str] = set()
    rt.on_speech(lambda active: _set_quiet(service, reasons, "speaking", active))
    rt.on_listen(lambda active: _set_quiet(service, reasons, "listening", active))
    rt.on_ring(lambda active: _set_quiet(service, reasons, "ringing", active))
    return service


def _set_quiet(service: MusicService, reasons: set[str], reason: str, active: bool) -> None:
    """Duck while any reason holds, and only restore once every one has cleared."""
    was_quiet = bool(reasons)
    reasons.add(reason) if active else reasons.discard(reason)
    if bool(reasons) == was_quiet:
        return
    _spawn(service.duck() if reasons else service.unduck())


def _spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


_pending: set[asyncio.Task] = set()


def _build_provider(settings: Settings) -> MusicProvider:
    if settings.music.provider != "spotify":
        raise MusicUnavailable(f"unknown music provider {settings.music.provider!r}")
    return SpotifyProvider(settings)

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
    def __init__(
        self,
        settings: Settings,
        provider: MusicProvider | None = None,
        *,
        output: object | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider or _build_provider(settings, output)
        self._ducked_from: int | None = None
        self._duck_lock = asyncio.Lock()

    async def speakers(self) -> list[Speaker]:
        return await self.provider.speakers()

    async def play(self, query: str | None, kind: str = "track", speaker_name: str = "") -> str:
        speaker = None
        if speaker_name:
            speaker = self.provider.resolve(await self.speakers(), speaker_name)
        return self._playing(await self.provider.play(query, kind, speaker))

    async def pause(self) -> str:
        return await self.provider.pause()

    async def resume(self) -> str:
        return self._playing(await self.provider.resume())

    async def skip(self, backwards: bool = False) -> str:
        return self._playing(await self.provider.skip(backwards))

    def _playing(self, spoken: str) -> str:
        """Music is running now, so the exchange is over unless the user starts another one.

        Getting a track going is usually all that was wanted, and an Echo goes back to its wake
        word here too. Pausing and volume are left alone: those tend to come in runs.
        """
        try:
            runtime().end_conversation()
        except Exception:
            # No runtime outside a session, e.g. `pot music play` from the shell.
            log.debug("no runtime to end the conversation on", exc_info=True)
        return spoken

    async def now_playing(self) -> str:
        return await self.provider.now_playing()

    async def seek(self, seconds: int, relative: bool = True) -> str:
        return await self.provider.seek(seconds, relative)

    async def set_shuffle(self, on: bool) -> str:
        return await self.provider.set_shuffle(on)

    async def set_repeat(self, mode: str) -> str:
        return await self.provider.set_repeat(mode)

    async def queue(self, query: str, kind: str = "track") -> str:
        return await self.provider.queue(query, kind)

    async def upcoming(self) -> str:
        return await self.provider.upcoming()

    async def recent(self) -> str:
        return await self.provider.recent()

    async def save_current(self, remove: bool = False) -> str:
        return await self.provider.save_current(remove)

    async def add_current_to_playlist(self, playlist: str) -> str:
        return await self.provider.add_current_to_playlist(playlist)

    async def move(self, speaker_name: str) -> str:
        speaker = self.provider.resolve(await self.speakers(), speaker_name)
        return self._playing(await self.provider.move(speaker))

    async def set_volume(self, percent: int) -> str:
        percent = max(0, min(100, percent))
        # Under the duck lock: interleaved with duck()/unduck(), a manual change could be applied
        # mid-duck and then "restored" away by the unduck that follows.
        async with self._duck_lock:
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
    service = MusicService(rt.settings, output=rt.speaker)
    # Music ducks while Pot talks, while Pot listens for a request, and while an alarm rings; the
    # session broadcasts all three and skills subscribe, so core never imports a skill.
    reasons: set[str] = set()

    def quiet(reason: str, active: bool) -> None:
        _set_quiet(service, reasons, reason, active)
        if rt.speaker is not None:
            # Music mixed through Pot ducks with a gain change rather than a Spotify round trip:
            # it takes effect on the next audio block and can't fail on the network.
            rt.speaker.duck_music(bool(reasons))

    rt.on_speech(lambda active: quiet("speaking", active))
    rt.on_listen(lambda active: quiet("listening", active))
    rt.on_ring(lambda active: quiet("ringing", active))
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


def _build_provider(settings: Settings, output: object | None = None) -> MusicProvider:
    if settings.music.provider != "spotify":
        raise MusicUnavailable(f"unknown music provider {settings.music.provider!r}")
    return SpotifyProvider(settings, output=output)

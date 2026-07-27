"""Provider-agnostic music types.

Same shape as the lights skill: a protocol plus small value types, so a second backend
(YouTube Music, a local library) slots in without the skill or the service changing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class MusicError(RuntimeError):
    """Any failure to control playback."""


class MusicUnavailable(MusicError):
    """Nowhere to play, or the provider isn't set up."""


@dataclass(frozen=True)
class Speaker:
    """Somewhere music can play. A Spotify Connect device, in practice."""

    id: str
    name: str
    kind: str = ""
    is_active: bool = False
    volume_percent: int | None = None
    supports_volume: bool = True
    is_pot: bool = False

    def describe(self) -> str:
        marks = []
        if self.is_pot:
            marks.append("this is Pot")
        if self.is_active:
            marks.append("active")
        suffix = f" ({', '.join(marks)})" if marks else ""
        return f"{self.name}{suffix}"


@runtime_checkable
class MusicProvider(Protocol):
    name: str

    async def speakers(self) -> list[Speaker]: ...

    async def play(self, query: str | None, kind: str, speaker: Speaker | None) -> str: ...

    async def pause(self) -> str: ...

    async def resume(self) -> str: ...

    async def skip(self, backwards: bool = False) -> str: ...

    async def set_volume(self, percent: int) -> None: ...

    async def volume(self) -> int | None: ...

    async def now_playing(self) -> str: ...

    async def close(self) -> None: ...

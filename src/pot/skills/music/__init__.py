"""Music skill: play, control and target speakers, currently through Spotify."""

from __future__ import annotations

import logging

from agents import function_tool

from pot.runtime import Runtime
from pot.skills.base import Skill
from pot.skills.music.provider import MusicError
from pot.skills.music.service import get_service

log = logging.getLogger(__name__)

PROMPT = """
Music: you can play, pause, skip and set the volume, and choose which speaker to use.
- play_music takes a natural query ("brown eyed girl", "the new Kendrick album", "my discover
  weekly") plus what kind of thing it is. Guess the kind from how they said it.
- "play something" or a mood ("some jazz", "something calm") is a fine query; pass it through
  rather than asking which track.
- Music volume is separate from your own speaking volume, and it dips automatically while you
  talk, so don't mention that or offer to do it.
- Name the track and stop there. If nothing can play, say what to do about it once.
""".strip()


@function_tool
async def play_music(query: str, kind: str = "track", speaker: str = "") -> str:
    """Play music.

    Args:
        query: What to play: a song, album, artist, playlist or mood.
        kind: One of track, album, artist, playlist. Use track when unsure.
        speaker: Optional speaker name; leave empty for the usual one.
    """
    try:
        return await get_service().play(query, kind, speaker)
    except MusicError as exc:
        return str(exc)


@function_tool
async def pause_music() -> str:
    """Pause whatever is playing."""
    try:
        return await get_service().pause()
    except MusicError as exc:
        return str(exc)


@function_tool
async def resume_music() -> str:
    """Resume playback after a pause."""
    try:
        return await get_service().resume()
    except MusicError as exc:
        return str(exc)


@function_tool
async def skip_track(backwards: bool = False) -> str:
    """Skip to the next track, or back to the previous one.

    Args:
        backwards: True to go to the previous track instead of the next.
    """
    try:
        return await get_service().skip(backwards)
    except MusicError as exc:
        return str(exc)


@function_tool
async def set_music_volume(percent: int) -> str:
    """Set the music volume, 0 to 100. This is separate from Pot's speaking volume.

    Args:
        percent: Target volume from 0 to 100.
    """
    try:
        return await get_service().set_volume(percent)
    except MusicError as exc:
        return str(exc)


@function_tool
async def now_playing() -> str:
    """What is playing right now, and where."""
    try:
        return await get_service().now_playing()
    except MusicError as exc:
        return str(exc)


@function_tool
async def list_speakers() -> str:
    """List the speakers music can play on."""
    try:
        speakers = await get_service().speakers()
    except MusicError as exc:
        return str(exc)
    if not speakers:
        return "No speakers are available right now."
    return "\n".join(speaker.describe() for speaker in speakers)


async def _start(runtime: Runtime) -> None:
    """Bring up Pot's own Spotify speaker, when it's switched on."""
    if not runtime.settings.music.speaker.enabled:
        return
    await get_service().start_speaker()


SKILL = Skill(
    name="music",
    description="Play and control music on any of your speakers",
    tools=(
        play_music,
        pause_music,
        resume_music,
        skip_track,
        set_music_volume,
        now_playing,
        list_speakers,
    ),
    prompt=PROMPT,
    start=_start,
)

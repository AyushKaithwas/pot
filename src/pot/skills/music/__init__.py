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
Music: play, pause, skip, seek, shuffle, repeat, queue, save, and choose which speaker.
- play_music takes a natural query ("brown eyed girl", "the new Kendrick album", "my discover
  weekly") plus what kind of thing it is. Guess the kind from how they said it. Podcasts are
  kind="episode" for one, kind="show" for a series.
- "play something" or a mood ("some jazz", "something calm") is a fine query; pass it through
  rather than asking which track.
- "play X next" is queue_music, not play_music: queueing doesn't interrupt what's on.
- "forward thirty seconds" is seek_music(30); "back a bit" is a negative number; "from the start"
  is seek_music(0, from_start=True). Don't offer to skip the track instead.
- "I like this" or "save this" is save_song. "What was that song earlier" is recently_played.
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
async def seek_music(seconds: int, from_start: bool = False) -> str:
    """Move within the current track.

    Args:
        seconds: How far. Negative goes backwards, e.g. -30 for "back thirty seconds".
        from_start: True to treat `seconds` as a position from the beginning of the track
            ("play it from two minutes in" is seconds=120, from_start=True).
    """
    try:
        return await get_service().seek(seconds, relative=not from_start)
    except MusicError as exc:
        return str(exc)


@function_tool
async def shuffle_music(on: bool = True) -> str:
    """Turn shuffle on or off.

    Args:
        on: False to play in order again.
    """
    try:
        return await get_service().set_shuffle(on)
    except MusicError as exc:
        return str(exc)


@function_tool
async def repeat_music(mode: str = "context") -> str:
    """Set repeat: "off", "track" for this one on a loop, "context" for the album or playlist.

    Args:
        mode: off, track or context.
    """
    try:
        return await get_service().set_repeat(mode.strip().lower())
    except MusicError as exc:
        return str(exc)


@function_tool
async def queue_music(query: str, kind: str = "track") -> str:
    """Add something to play after what's on now, without interrupting it.

    Args:
        query: What to queue.
        kind: track, album, artist, playlist, episode or show.
    """
    try:
        return await get_service().queue(query, kind)
    except MusicError as exc:
        return str(exc)


@function_tool
async def whats_next() -> str:
    """What's queued to play after the current track."""
    try:
        return await get_service().upcoming()
    except MusicError as exc:
        return str(exc)


@function_tool
async def recently_played() -> str:
    """What has played recently, for "what was that song?"."""
    try:
        return await get_service().recent()
    except MusicError as exc:
        return str(exc)


@function_tool
async def save_song(remove: bool = False) -> str:
    """Save what's playing to the user's library, or take it out again.

    Args:
        remove: True to remove it instead of saving it.
    """
    try:
        return await get_service().save_current(remove)
    except MusicError as exc:
        return str(exc)


@function_tool
async def add_to_playlist(playlist: str) -> str:
    """Add what's playing to one of the user's own playlists.

    Args:
        playlist: The playlist's name, matched loosely against their own playlists.
    """
    try:
        return await get_service().add_current_to_playlist(playlist)
    except MusicError as exc:
        return str(exc)


@function_tool
async def move_music(speaker: str) -> str:
    """Move playback to another speaker, carrying on where it left off.

    Args:
        speaker: The speaker's name, from list_speakers.
    """
    try:
        return await get_service().move(speaker)
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
        seek_music,
        shuffle_music,
        repeat_music,
        queue_music,
        whats_next,
        recently_played,
        save_song,
        add_to_playlist,
        move_music,
        set_music_volume,
        now_playing,
        list_speakers,
    ),
    prompt=PROMPT,
    start=_start,
)

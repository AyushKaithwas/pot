"""The voice loop opens the mic and speaker with `async with`, so both must be async.

This is here because a sync-only Speaker crashed `pot talk` at startup while every other
test still passed: nothing else in the suite enters those context managers.
"""

from __future__ import annotations

import asyncio
import inspect

import numpy as np
import pytest

from pot.audio.capture import Microphone, MicrophoneGate
from pot.audio.playback import DUCKED_GAIN, Speaker, _mix


@pytest.mark.parametrize("cls", [Speaker, Microphone])
def test_audio_objects_are_async_context_managers(cls):
    assert inspect.iscoroutinefunction(cls.__aenter__)
    assert inspect.iscoroutinefunction(cls.__aexit__)


async def test_speaker_plays_and_flushes():
    speaker = Speaker(sample_rate=24000)
    async with speaker:
        speaker.write(b"\x00\x00" * 2400)
        assert speaker.is_playing
        speaker.flush()
        assert not speaker.is_playing


def test_gate_starts_closed_for_push_to_talk_and_toggles():
    gate = MicrophoneGate()
    assert not gate.is_open
    assert gate.toggle() is True
    assert gate.is_open
    gate.close()
    assert not gate.is_open


def test_gate_can_start_open_for_always_on():
    assert MicrophoneGate(open_at_start=True).is_open


async def test_playback_waits_for_a_little_audio_before_starting():
    """Playing the first burst instantly is what leaves the device starved on the second one."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=100)
    async with speaker:
        speaker.write(b"\x00\x00" * 240)  # 10ms, well under the pre-buffer
        # Whatever rate the device runs at, that 10ms is queued once the resampler lets go of it.
        speaker.finish()
        assert speaker.is_playing, "it's queued"
        await asyncio.sleep(0.15)
        assert speaker.underruns == 0, "a short write must not be reported as starvation"


async def test_a_full_buffer_plays_without_underruns():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=60)
    async with speaker:
        speaker.write(b"\x00\x00" * 24000)  # a full second
        await asyncio.sleep(0.4)
    assert speaker.underruns == 0


def test_flush_disarms_playback_so_the_next_burst_rebuffers():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=60)
    speaker.write(b"\x00\x00" * 2400)
    speaker.flush()
    assert not speaker.is_playing


async def test_the_end_of_a_reply_is_not_reported_as_starvation():
    """The count is a diagnostic the user is told to act on, so it has to mean something."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=60)
    async with speaker:
        speaker.write(b"\x00\x00" * 4800)  # 200ms
        speaker.finish()
        while speaker.is_playing:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    assert speaker.underruns == 0


def test_the_gate_can_be_deafened_briefly():
    now = 0.0
    gate = MicrophoneGate(open_at_start=True, clock=lambda: now)
    gate.mute_for(0.5)

    now = 0.4
    assert gate.is_muted, "the cue is still sounding"
    now = 1.0
    assert not gate.is_muted, "it has finished, so listen again"
    assert gate.is_open, "muting is temporary deafness, not the gate closing"


def test_muting_never_shortens_an_existing_mute():
    now = 0.0
    gate = MicrophoneGate(clock=lambda: now)
    gate.mute_for(1.0)
    gate.mute_for(0.1)
    now = 0.5
    assert gate.is_muted, "a second, shorter cue must not cut the first one's silence short"


def test_a_gate_is_not_muted_by_default():
    assert not MicrophoneGate().is_muted


def _loud(samples: int, value: int = 8000) -> bytes:
    return np.full(samples, value, dtype=np.int16).tobytes()


async def test_music_plays_under_pots_voice_rather_than_after_it():
    """Queueing music behind speech would delay every reply by the length of the track."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        # Comfortably longer than the resamplers' own latency, so both channels have emerged.
        speaker.write(_loud(4800, 1000))
        speaker.write_music(_loud(4800, 2000))
        await asyncio.sleep(0.3)
        played = np.frombuffer(speaker.drain_reference(), dtype=np.int16)

    assert played.size, "something has to have reached the device"
    assert played.max() > 800, "Pot's voice has to reach the device with music underneath it"


def test_ducking_lowers_music_without_touching_pots_voice():
    voice, music = _loud(240, 1000), _loud(240, 8000)
    wanted = len(voice)

    full = np.frombuffer(_mix(voice, music, wanted, 1.0), dtype=np.int16)
    ducked = np.frombuffer(_mix(voice, music, wanted, DUCKED_GAIN), dtype=np.int16)

    assert ducked.max() < full.max() / 2, f"ducked peak {ducked.max()} vs {full.max()}"
    assert ducked.max() > 1000, "Pot's own voice is not what gets ducked"


def test_ducking_all_the_way_down_leaves_only_pots_voice():
    voice = _loud(240, 1000)
    assert np.frombuffer(_mix(voice, _loud(240, 8000), len(voice), 0.0), dtype=np.int16).max() == 1000


def test_a_short_channel_does_not_truncate_the_block():
    """Music runs out at the end of a track and voice whenever Pot stops; the block is still full."""
    assert len(_mix(_loud(100), _loud(40), 480, 1.0)) == 480


def test_mixing_clips_rather_than_wrapping():
    """Wrapping two loud channels around int16 is the difference between loud and destroyed."""
    loudest = np.frombuffer(_mix(_loud(240, 30000), _loud(240, 30000), 480, 1.0), dtype=np.int16)
    assert loudest.max() == 32767


async def test_music_is_not_mistaken_for_pot_still_talking():
    """is_playing gates cues and the reopening of the mic, so music must not hold either open."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        speaker.write_music(_loud(24000))
        assert not speaker.is_playing


def test_an_interruption_stops_pot_but_not_the_music():
    """Barge-in silences Pot, not the room: talking over a reply shouldn't kill the track."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=600)
    speaker.write(_loud(4800))
    speaker.write_music(_loud(4800))
    speaker.flush()

    assert not speaker.is_playing
    assert speaker.music_queued > 0


def test_stopping_music_leaves_pot_able_to_speak():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    speaker.write(_loud(4800))
    speaker.write_music(_loud(4800))
    speaker.stop_music()

    assert speaker.music_queued == 0
    assert speaker.is_playing


async def test_a_music_backlog_is_dropped_rather_than_allowed_to_drift():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        for _ in range(20):
            speaker.write_music(_loud(24000))  # 20 seconds, far past the cap
        assert speaker.music_dropped > 0, "otherwise playback falls further behind the stream"


async def test_music_is_kept_out_of_the_echo_reference():
    """Suppression subtracts per band, and against continuous music that removes the speaker too.

    Measured: with music in the reference the wake word went from firing at equal loudness to never
    firing. Until there's an adaptive canceller, the reference stays Pot's own voice.
    """
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        speaker.write_music(_loud(4800, 8000))
        await asyncio.sleep(0.25)
        # Not bit-exact zero: the reference is resampled, and that rounds at the last bit.
        assert np.abs(np.frombuffer(speaker.drain_reference(), dtype=np.int16)).max() < 10


async def test_pots_own_voice_is_still_in_the_reference():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        speaker.write(_loud(4800))
        await asyncio.sleep(0.25)
        assert np.abs(np.frombuffer(speaker.drain_reference(), dtype=np.int16)).max() > 0


async def test_a_short_complete_sound_plays_even_behind_a_big_buffer():
    """The bug this catches: raising the buffer to 300ms silenced every earcon.

    A cue is ~200ms and arrives complete. The pre-buffer is there to ride out audio still on its
    way from the network, so waiting for more than exists means it never plays at all.
    """
    speaker = Speaker(sample_rate=24000, prebuffer_ms=300)
    async with speaker:
        speaker.write(_loud(4800, 6000))  # 200ms, under the 300ms pre-buffer
        speaker.finish()  # nothing more is coming
        await asyncio.sleep(0.3)
        played = np.abs(np.frombuffer(speaker.drain_reference(), dtype=np.int16)).max()

    assert played > 1000, f"peak {played}: the cue never reached the speaker"


async def test_a_partial_stream_still_waits_for_the_buffer():
    """Streaming audio must still pre-buffer, or the jitter fix is undone."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=300)
    async with speaker:
        speaker.write(_loud(2400, 6000))  # 100ms and more is expected
        await asyncio.sleep(0.2)
        # Under 10 rather than zero: the reference is resampled, and that rounds at the last bit.
        assert np.abs(np.frombuffer(speaker.drain_reference(), dtype=np.int16)).max() < 10


async def test_a_chime_rings_over_speech_instead_of_waiting_for_it():
    """An alarm that queues behind a sentence isn't ringing at the time it was set for."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        speaker.write(_loud(4800, 1000))
        speaker.write_chime(_loud(4800, 8000))
        await asyncio.sleep(0.3)
        heard = np.abs(np.frombuffer(speaker.drain_reference(), dtype=np.int16))

    assert heard.max() > 6000, "the chime has to be audible immediately, not after the sentence"


async def test_stopping_the_chime_leaves_speech_playing():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    speaker.write(_loud(4800))
    speaker.write_chime(_loud(4800))
    speaker.stop_chime()
    assert speaker.is_playing, "dismissing an alarm must not silence what Pot was saying"


async def test_the_chime_is_not_ducked_with_the_music():
    """DUCKED_GAIN applies while anything rings; ducking the ring itself would defeat it."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=0)
    async with speaker:
        speaker.duck_music(True)
        speaker.write_chime(_loud(4800, 8000))
        await asyncio.sleep(0.3)
        heard = np.abs(np.frombuffer(speaker.drain_reference(), dtype=np.int16))
    assert heard.max() > 6000, f"peak {heard.max()}: the chime must ride at full level"

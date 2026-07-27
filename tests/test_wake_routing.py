"""Where microphone audio goes, which is the same thing as what Pot costs.

The rule: every chunk reaches the local wake word, and nothing reaches OpenAI until it fires.
A regression here doesn't break behaviour, it silently streams a room to a paid API all day.
"""

from __future__ import annotations

from pot.audio.capture import MicrophoneGate
from pot.config import AudioConfig, Settings, WakeConfig
from pot.runtime import Runtime
from pot.voice.agent import build_run_config
from pot.voice.session import _how_to_talk, _pump_microphone, _start_listening, _stop_listening


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_audio(self, chunk: bytes, *, commit: bool = False) -> None:
        self.sent.append(chunk)


class FakeMicrophone:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def chunks(self):
        for chunk in self._chunks:
            yield chunk


class FakeWake:
    """Fires on the chunk named in `fire_on`."""

    model_name = "hey_jarvis"

    def __init__(self, fire_on: bytes | None = None) -> None:
        self.fire_on = fire_on
        self.heard: list[bytes] = []
        self.resets = 0

    def feed(self, chunk: bytes) -> bool:
        self.heard.append(chunk)
        return chunk == self.fire_on

    def reset(self) -> None:
        self.resets += 1


class FakeCues:
    def __init__(self) -> None:
        self.played: list[str] = []

    def ready(self) -> None:
        self.played.append("ready")

    def closed(self) -> None:
        self.played.append("closed")


async def test_nothing_is_sent_before_the_wake_word():
    session, wake, gate = FakeSession(), FakeWake(), MicrophoneGate()
    microphone = FakeMicrophone([b"a", b"b", b"c"])
    await _pump_microphone(session, microphone, gate, wake, Runtime(Settings()), FakeCues())
    assert session.sent == [], "an idle room must cost nothing"
    assert wake.heard == [b"a", b"b", b"c"], "but the detector hears everything"


async def test_audio_flows_only_after_the_wake_word_fires():
    session, wake, gate = FakeSession(), FakeWake(fire_on=b"wake"), MicrophoneGate()
    microphone = FakeMicrophone([b"before", b"wake", b"after", b"more"])
    await _pump_microphone(session, microphone, gate, wake, Runtime(Settings()), FakeCues())
    assert session.sent[-3:] == [b"wake", b"after", b"more"]
    assert gate.is_open


async def test_the_triggering_chunk_is_never_dropped():
    """It holds the end of the wake word and often the start of the request."""
    session, wake, gate = FakeSession(), FakeWake(fire_on=b"wake"), MicrophoneGate()
    settings = Settings(audio=AudioConfig(mode="wake_word", wake=WakeConfig(preroll_seconds=0.0)))
    await _pump_microphone(
        session, FakeMicrophone([b"wake", b"after"]), gate, wake, Runtime(settings), FakeCues()
    )
    assert session.sent == [b"wake", b"after"]


async def test_recent_audio_is_replayed_so_the_request_is_not_clipped():
    """Detection lands after the word ends, by which point the request has already started."""
    session, wake, gate = FakeSession(), FakeWake(fire_on=b"wake"), MicrophoneGate()
    # Chunks are 20 ms, so 40 ms of pre-roll keeps two: the trigger and the one before it.
    settings = Settings(audio=AudioConfig(mode="wake_word", wake=WakeConfig(preroll_seconds=0.04)))
    microphone = FakeMicrophone([b"old", b"recent", b"wake", b"after"])
    await _pump_microphone(session, microphone, gate, wake, Runtime(settings), FakeCues())
    assert session.sent == [b"recent", b"wake", b"after"], "older audio falls out of the window"


async def test_the_preroll_does_not_leak_into_the_next_conversation():
    session, wake, gate = FakeSession(), FakeWake(fire_on=b"wake"), MicrophoneGate()
    settings = Settings(audio=AudioConfig(mode="wake_word", wake=WakeConfig(preroll_seconds=1.0)))
    microphone = FakeMicrophone([b"a", b"wake", b"b"])
    await _pump_microphone(session, microphone, gate, wake, Runtime(settings), FakeCues())
    # Everything buffered was flushed once; nothing is queued to be sent twice.
    assert session.sent.count(b"a") == 1
    assert session.sent.count(b"wake") == 1


async def test_the_detector_rests_while_a_conversation_is_open():
    """No point spending CPU on detection, and it stops Pot re-triggering on itself."""
    session, wake = FakeSession(), FakeWake()
    gate = MicrophoneGate(open_at_start=True)
    microphone = FakeMicrophone([b"a", b"b"])
    await _pump_microphone(session, microphone, gate, wake, Runtime(Settings()), FakeCues())
    assert wake.heard == []
    assert session.sent == [b"a", b"b"]


async def test_without_a_wake_word_the_gate_alone_decides():
    session, gate = FakeSession(), MicrophoneGate()
    microphone = FakeMicrophone([b"a"])
    await _pump_microphone(session, microphone, gate, None, Runtime(Settings()), FakeCues())
    assert session.sent == []


def test_starting_to_listen_announces_itself_and_clears_stale_audio():
    gate, cues, wake = MicrophoneGate(), FakeCues(), FakeWake()
    runtime = Runtime(Settings())
    _start_listening(gate, runtime, cues, wake)
    assert gate.is_open
    assert runtime.listening is True
    assert cues.played == ["ready"], "a screenless device needs the blip"
    assert wake.resets == 1


def test_stopping_closes_the_gate_and_says_so():
    gate, cues = MicrophoneGate(open_at_start=True), FakeCues()
    runtime = Runtime(Settings())
    runtime.set_listening(True)
    _stop_listening(gate, runtime, cues, "back to hey_jarvis")
    assert not gate.is_open
    assert runtime.listening is False
    assert cues.played == ["closed"]


def test_listening_state_is_broadcast_for_ducking():
    """Music has to drop while Pot listens, or the mic hears the speaker instead of you."""
    runtime = Runtime(Settings())
    seen: list[bool] = []
    runtime.on_listen(seen.append)
    runtime.set_listening(True)
    runtime.set_listening(True)
    runtime.set_listening(False)
    assert seen == [True, False]


def test_speaking_and_listening_are_separate_signals():
    runtime = Runtime(Settings())
    speech: list[bool] = []
    listen: list[bool] = []
    runtime.on_speech(speech.append)
    runtime.on_listen(listen.append)
    runtime.set_speaking(True)
    assert (speech, listen) == ([True], [])


def test_no_idle_timeout_is_sent_to_the_api():
    """The API rejects turn_detection.idle_timeout_ms, and a rejected session update silently
    discards the entire configuration: the model comes up with no tools at all. Conversation
    timeouts are handled locally instead."""
    for settings in (Settings(), Settings(audio=AudioConfig(mode="wake_word"))):
        turn_detection = build_run_config(settings)["model_settings"]["audio"]["input"]["turn_detection"]
        assert "idle_timeout_ms" not in turn_detection


def test_the_prompt_tells_you_how_to_start():
    assert "space" in _how_to_talk(AudioConfig(), None)
    assert "always-on" in _how_to_talk(AudioConfig(mode="always_on"), None)
    assert "hey jarvis" in _how_to_talk(AudioConfig(mode="wake_word"), FakeWake())


def test_only_always_on_starts_with_the_mic_sending():
    assert AudioConfig(mode="always_on").mic_open_at_start is True
    assert AudioConfig(mode="wake_word").mic_open_at_start is False
    assert AudioConfig(mode="push_to_talk").mic_open_at_start is False

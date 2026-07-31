"""Mic gating and speech state across a turn.

The mic has to shut while Pot speaks (a laptop's speakers otherwise interrupt it) and reopen
afterwards, or every single turn needs a keypress. The same moment drives ducking, so "Pot
stopped talking" must not fire until playback has actually drained.
"""

from __future__ import annotations

import asyncio

from pot.audio.capture import MicrophoneGate
from pot.audio.echo import EchoSuppressor
from pot.config import AudioConfig, Settings
from pot.runtime import Runtime
from pot.voice.activity import ToolActivity
from pot.voice.conversation import ConversationWindow
from pot.voice.session import (
    RECONNECT_DELAY_CAP_SECONDS,
    RECONNECT_DELAY_SECONDS,
    TranscriptPrinter,
    _finish_speaking,
    _hold_for_question,
    _how_to_talk,
    _is_expected,
    _listen_while_ringing,
    _reconnect_delay,
    _reset_between_connections,
    _should_reopen,
)


class FakeSpeaker:
    def __init__(self, playing_for: float = 0.0) -> None:
        self._until = asyncio.get_event_loop().time() + playing_for

    @property
    def is_playing(self) -> bool:
        return asyncio.get_event_loop().time() < self._until


async def test_mic_reopens_once_playback_has_drained():
    gate = MicrophoneGate()
    gate.close()
    speaker = FakeSpeaker(playing_for=0.2)

    task = asyncio.create_task(
        _finish_speaking(speaker, gate, Runtime(Settings()), reopen_mic=True, settle_seconds=0.01)
    )
    await asyncio.sleep(0.1)
    assert not gate.is_open, "must not reopen while Pot is still speaking"
    await task
    assert gate.is_open


async def test_mic_reopens_immediately_when_nothing_is_playing():
    gate = MicrophoneGate()
    await _finish_speaking(FakeSpeaker(), gate, Runtime(Settings()), reopen_mic=True, settle_seconds=0.01)
    assert gate.is_open


async def test_reopen_gives_up_rather_than_waiting_forever():
    gate = MicrophoneGate()
    await _finish_speaking(
        FakeSpeaker(playing_for=60.0),
        gate,
        Runtime(Settings()),
        reopen_mic=True,
        settle_seconds=0.01,
        timeout=0.1,
    )
    assert gate.is_open, "a stuck playback buffer must not strand the mic shut"


async def test_a_cancelled_reopen_leaves_the_mic_shut():
    gate = MicrophoneGate()
    task = asyncio.create_task(
        _finish_speaking(FakeSpeaker(playing_for=5.0), gate, Runtime(Settings()), reopen_mic=True)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not gate.is_open


async def test_hands_free_mode_leaves_the_gate_alone_but_still_ends_the_turn():
    gate = MicrophoneGate(open_at_start=True)
    runtime = Runtime(Settings())
    runtime.set_speaking(True)
    await _finish_speaking(FakeSpeaker(), gate, runtime, reopen_mic=False, settle_seconds=0.01)
    assert gate.is_open
    assert runtime.speaking is False


async def test_speech_state_only_clears_after_playback_drains():
    """Ducking hangs off this: unducking early would raise the music mid-sentence."""
    runtime = Runtime(Settings())
    seen: list[bool] = []
    runtime.on_speech(seen.append)

    runtime.set_speaking(True)
    task = asyncio.create_task(
        _finish_speaking(
            FakeSpeaker(playing_for=0.2), MicrophoneGate(), runtime, reopen_mic=False, settle_seconds=0.01
        )
    )
    await asyncio.sleep(0.1)
    assert seen == [True], "still speaking while audio is playing"
    await task
    assert seen == [True, False]


async def test_the_mic_reopens_quietly_while_a_tool_is_still_running():
    """Printing "listening" mid-search reads as finished; the spinner is already saying more."""
    from rich.console import Console

    printed = Console(force_terminal=False, no_color=True, record=True, width=100)
    activity = ToolActivity(printed)
    activity.start("research")
    gate = MicrophoneGate()
    await _finish_speaking(
        FakeSpeaker(), gate, Runtime(Settings()), activity, reopen_mic=True, settle_seconds=0.01
    )
    assert gate.is_open, "you must still be able to interrupt"


async def test_listening_is_announced_once_nothing_is_running():
    gate = MicrophoneGate()
    activity = ToolActivity()
    await _finish_speaking(
        FakeSpeaker(), gate, Runtime(Settings()), activity, reopen_mic=True, settle_seconds=0.01
    )
    assert gate.is_open and not activity.busy


def test_repeated_speech_states_are_not_republished():
    runtime = Runtime(Settings())
    seen: list[bool] = []
    runtime.on_speech(seen.append)
    runtime.set_speaking(True)
    runtime.set_speaking(True)
    runtime.set_speaking(False)
    assert seen == [True, False]


def test_a_failing_listener_does_not_break_the_broadcast():
    runtime = Runtime(Settings())
    seen: list[bool] = []

    def boom(_: bool) -> None:
        raise RuntimeError("nope")

    runtime.on_speech(boom)
    runtime.on_speech(seen.append)
    runtime.set_speaking(True)
    assert seen == [True]


class FakeWake:
    """Only its presence matters to the decision, not its behaviour."""


def _audio(**overrides) -> AudioConfig:
    return AudioConfig(**overrides)


def test_a_follow_up_does_not_need_the_wake_word_again():
    """The bug this catches: Pot answering, then ignoring you until you say the word again."""
    window = ConversationWindow(15)
    window.start()
    assert _should_reopen(_audio(mode="wake_word"), FakeWake(), window)


def test_the_wake_word_is_needed_again_once_the_conversation_has_timed_out():
    assert not _should_reopen(_audio(mode="wake_word"), FakeWake(), ConversationWindow(15))


def test_push_to_talk_reopens_as_before():
    assert _should_reopen(_audio(mode="push_to_talk"), None, None)


def test_hands_free_has_nothing_to_reopen():
    assert not _should_reopen(_audio(mode="always_on"), None, None)


def test_auto_reopen_off_is_still_respected():
    window = ConversationWindow(15)
    window.start()
    assert not _should_reopen(_audio(mode="wake_word", auto_reopen=False), FakeWake(), window)


class FakeCues:
    def __init__(self) -> None:
        self.closed_played = False
        self.ready_played = False

    def closed(self) -> None:
        self.closed_played = True

    def ready(self) -> None:
        self.ready_played = True


class FakeGate:
    def __init__(self) -> None:
        self.opened = False

    def open(self) -> None:
        self.opened = True


def test_a_ringing_alarm_opens_the_mic():
    """Otherwise the one moment you need to be heard is the one moment Pot isn't listening."""
    gate, runtime, window = FakeGate(), Runtime(Settings()), ConversationWindow(15)
    _listen_while_ringing(gate, runtime, None, window)(True)

    assert gate.opened, "there is no screen and no button, so 'stop' is the only way out"
    assert window.open, "and the window has to be open or it shuts again in 15 seconds"
    assert runtime.listening


def test_the_end_of_a_ring_does_not_slam_the_mic_shut():
    """Stopping the alarm is usually mid-sentence; the rest of it still has to be heard."""
    gate, runtime, window = FakeGate(), Runtime(Settings()), ConversationWindow(15)
    listener = _listen_while_ringing(gate, runtime, None, window)
    listener(True)
    listener(False)
    assert window.open


def test_a_skill_can_ask_for_the_conversation_to_end():
    runtime = Runtime(Settings())
    assert not runtime.take_conversation_end(), "nothing asked, so nothing to do"

    runtime.end_conversation()
    assert runtime.take_conversation_end()
    assert not runtime.take_conversation_end(), "one request, honoured once"


async def test_ending_the_conversation_says_so_out_loud():
    """Going quiet with no sound is indistinguishable from having crashed, with no screen."""
    gate = MicrophoneGate(open_at_start=True)
    cues = FakeCues()
    await _finish_speaking(
        FakeSpeaker(),
        gate,
        Runtime(Settings()),
        None,
        cues,
        reopen_mic=False,
        closed_reason='say "hey mycroft" again',
        settle_seconds=0.01,
    )
    assert not gate.is_open
    assert cues.closed_played, "the earcon is the only way to hear the state change"


async def test_a_reply_that_keeps_listening_says_nothing_extra():
    gate = MicrophoneGate()
    cues = FakeCues()
    await _finish_speaking(
        FakeSpeaker(), gate, Runtime(Settings()), None, cues, reopen_mic=True, settle_seconds=0.01
    )
    assert gate.is_open
    assert not cues.closed_played


def test_the_quit_key_is_only_offered_where_keys_are_read():
    """Over `ssh host 'pot talk'` there's no terminal, so q reaches nothing and looks like a hang."""
    wake_mode = AudioConfig(mode="wake_word")
    assert "q = quit" in _how_to_talk(wake_mode, None, interactive=True)
    assert "q = quit" not in _how_to_talk(wake_mode, None, interactive=False)
    assert "ctrl-c" in _how_to_talk(wake_mode, None, interactive=False)


def test_every_mode_says_how_to_stop():
    for mode in ("push_to_talk", "always_on", "wake_word"):
        for interactive in (True, False):
            hint = _how_to_talk(AudioConfig(mode=mode), None, interactive=interactive)
            assert "quit" in hint or "stop" in hint, f"{mode}, interactive={interactive}: {hint!r}"


def test_a_session_expiring_is_not_reported_as_a_fault():
    """The API ends a session after an hour. Pot reconnects, so buzzing at the user is crying wolf."""
    assert _is_expected("RealtimeError(code='session_expired')")
    assert _is_expected("conversation_already_has_active_response")
    assert not _is_expected("RealtimeError(code='invalid_api_key')")
    assert not _is_expected("something nobody has seen before")


def test_reconnecting_starts_from_waiting_not_mid_conversation():
    """A fresh session has no idea a conversation was in progress, so don't stream the room at it."""
    gate = MicrophoneGate(open_at_start=True)
    runtime = Runtime(Settings())
    runtime.set_listening(True)
    window = ConversationWindow(15)
    window.start()
    echo = EchoSuppressor(24000)
    echo.playback(b"\x01\x02" * 480)

    _reset_between_connections(gate, runtime, window, echo)

    assert not gate.is_open
    assert not runtime.listening
    assert not window.open
    assert echo.process(b"\x03\x04" * 480) == b"\x03\x04" * 480, "a stale reference is worse than none"


def test_reconnect_backs_off_while_the_network_is_down():
    """Connections dying at birth mean a dead network; retrying every 2s helps nobody."""
    delay = RECONNECT_DELAY_SECONDS
    seen = []
    for _ in range(6):
        delay = _reconnect_delay(delay, connection_lasted=0.1)
        seen.append(delay)
    assert seen[0] > RECONNECT_DELAY_SECONDS, "a failing connection grows the pause"
    assert max(seen) <= RECONNECT_DELAY_CAP_SECONDS, "but never past the cap"


def test_reconnect_is_prompt_again_once_a_connection_survives():
    """The hourly expiry is a healthy connection ending, so the next one starts in seconds."""
    assert _reconnect_delay(30.0, connection_lasted=3600.0) == RECONNECT_DELAY_SECONDS


def test_a_reply_ending_in_a_question_keeps_pot_listening():
    """ "Which light?" answered by silence-then-wake-word is a broken conversation."""
    now = 0.0
    window = ConversationWindow(5, 15, clock=lambda: now)
    window.start()
    transcripts = TranscriptPrinter()
    transcripts.last_assistant_text = "Which light did you mean?"

    _hold_for_question(transcripts, window)
    now = 8.0
    assert not window.expired(), "it asked, so it waits to be answered"


def test_a_confirmation_gets_only_the_short_window():
    now = 0.0
    window = ConversationWindow(5, 15, clock=lambda: now)
    window.start()
    transcripts = TranscriptPrinter()
    transcripts.last_assistant_text = "Done, the lights are off."

    _hold_for_question(transcripts, window)
    now = 6.0
    assert window.expired(), "a confirmation rarely has a follow-up; the mic closes promptly"


def test_the_users_own_question_does_not_hold_the_window():
    """last_assistant_text tracks Pot's words only, so "what time is it?" from the user can't
    trigger the grace meant for Pot's questions."""
    transcripts = TranscriptPrinter()

    class Item:
        role = "user"
        status = "done"
        item_id = "u1"

        class Part:
            transcript = "What time is it?"
            text = None

        content = [Part()]

    transcripts.feed([Item()])
    assert transcripts.last_assistant_text == ""

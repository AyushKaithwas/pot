"""The built-ins, and ending an exchange on request.

Without a way to say "that's all", going quiet takes the full idle timeout, and every one of
those seconds is the mic open and the room going to a paid API.
"""

from __future__ import annotations

from pot.config import AudioConfig, Settings
from pot.runtime import Runtime, set_runtime
from pot.skills.basics import _stop_listening


def runtime_with(mode: str) -> Runtime:
    rt = Runtime(Settings(audio=AudioConfig(mode=mode)))
    set_runtime(rt)
    return rt


def test_asking_it_to_stop_ends_the_exchange():
    rt = runtime_with("wake_word")
    spoken = _stop_listening()

    assert rt.take_conversation_end(), "the session has to be told, or it waits out the timeout"
    assert spoken


def test_push_to_talk_can_be_told_to_stop_too():
    rt = runtime_with("push_to_talk")
    _stop_listening()
    assert rt.take_conversation_end()


def test_always_on_says_it_cannot_rather_than_doing_nothing():
    """There's no gate to close in always-on, and a silent no-op is the confusing kind of bug."""
    rt = runtime_with("always_on")
    spoken = _stop_listening()

    assert not rt.take_conversation_end(), "nothing was ended, so nothing should claim to be"
    assert "always-on" in spoken.lower()


def test_stopping_is_a_one_shot_request():
    """Consumed once: a leftover flag would end the *next* exchange as soon as it started."""
    rt = runtime_with("wake_word")
    _stop_listening()
    assert rt.take_conversation_end()
    assert not rt.take_conversation_end()

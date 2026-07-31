"""Ending a wake-word conversation.

If this never fires, a device keeps streaming a quiet room to a paid API. If it fires too eagerly,
Pot stops listening mid-exchange and you have to say the wake word again to finish a sentence.
"""

from __future__ import annotations

from pot.voice.conversation import ConversationWindow


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def window(seconds: float = 10.0) -> tuple[ConversationWindow, FakeClock]:
    clock = FakeClock()
    return ConversationWindow(seconds, clock=clock), clock


def test_a_closed_window_never_expires():
    """Before the wake word there is no conversation to end."""
    conversation, clock = window()
    clock.advance(3600)
    assert conversation.open is False
    assert conversation.expired() is False


def test_it_expires_after_the_configured_quiet():
    conversation, clock = window(10)
    conversation.start()
    clock.advance(9.9)
    assert not conversation.expired()
    clock.advance(0.2)
    assert conversation.expired()


def test_activity_pushes_the_deadline_back():
    conversation, clock = window(10)
    conversation.start()
    clock.advance(9)
    conversation.touch()
    clock.advance(9)
    assert not conversation.expired(), "still mid-conversation"
    clock.advance(2)
    assert conversation.expired()


def test_touching_a_closed_window_does_not_reopen_it():
    conversation, clock = window(10)
    conversation.touch()
    clock.advance(100)
    assert conversation.open is False
    assert conversation.expired() is False


def test_stopping_ends_it_immediately():
    conversation, _ = window(10)
    conversation.start()
    conversation.stop()
    assert conversation.open is False
    assert conversation.expired() is False


def test_remaining_counts_down_and_floors_at_zero():
    conversation, clock = window(10)
    assert conversation.remaining() == 0.0
    conversation.start()
    clock.advance(4)
    assert conversation.remaining() == 6.0
    clock.advance(100)
    assert conversation.remaining() == 0.0


def test_touch_never_pulls_the_deadline_earlier():
    """A routine event after await_answer must not shrink the question grace, or it would last
    exactly until the next heartbeat."""
    now = 0.0
    window = ConversationWindow(5, 15, clock=lambda: now)
    window.start()
    window.await_answer()

    now = 1.0
    window.touch()
    now = 10.0
    assert not window.expired(), "the 15s question grace outlives a touch at 1s"


def test_a_question_holds_the_window_longer():
    now = 0.0
    window = ConversationWindow(5, 15, clock=lambda: now)
    window.start()
    window.await_answer()
    now = 8.0
    assert not window.expired(), "Pot asked; 8s of thinking time is not abandonment"
    now = 16.0
    assert window.expired()


def test_await_answer_does_not_reopen_a_stopped_window():
    """Music ending the exchange wins over a stray question mark in the same reply."""
    window = ConversationWindow(5, 15)
    window.start()
    window.stop()
    window.await_answer()
    assert not window.open


def test_the_question_grace_defaults_to_the_plain_length():
    window = ConversationWindow(5)
    assert window.question_seconds == 5

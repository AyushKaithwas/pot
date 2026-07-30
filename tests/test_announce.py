"""A fired timer has to reach the session, which means crossing sync -> async correctly."""

from __future__ import annotations

import asyncio

from pot.config import Settings
from pot.runtime import Runtime
from pot.voice.conversation import ConversationWindow
from pot.voice.session import ANNOUNCE_PREAMBLE, Announcer


class FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail

    async def send_message(self, message: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.messages.append(message)


async def test_announcement_reaches_the_session():
    session = FakeSession()
    runtime = Runtime(Settings())
    runtime.on_announce(Announcer(session))
    runtime.announce("The pasta timer is up.")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.messages == [f"{ANNOUNCE_PREAMBLE} The pasta timer is up."]


async def test_a_failed_announcement_does_not_raise_into_the_caller():
    session = FakeSession(fail=True)
    announcer = Announcer(session)
    announcer("anything")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.messages == []


async def test_scheduled_job_announces_through_the_session():
    session = FakeSession()
    runtime = Runtime(Settings())
    runtime.on_announce(Announcer(session))

    async def fire() -> None:
        runtime.announce("Lamp off.")

    runtime.scheduler.schedule(0.01, "lamp", fire)
    await asyncio.sleep(0.1)
    assert session.messages == [f"{ANNOUNCE_PREAMBLE} Lamp off."]


def test_announce_falls_back_to_printing(capsys):
    Runtime(Settings()).announce("no session here")
    assert "no session here" in capsys.readouterr().out


async def test_an_announcement_opens_the_conversation():
    """Pot started this exchange, so it has to listen for the answer.

    The bug: after "now playing", the mic stayed shut and the only way back in was the wake word,
    which music has just drowned out.
    """
    window = ConversationWindow(15)
    session = FakeSession()
    Announcer(session, window)("Now playing Without Me.")
    await asyncio.sleep(0)

    assert window.open, "otherwise the reply reopens nothing and you can't say 'stop'"


async def test_an_announcement_without_a_window_still_speaks():
    session = FakeSession()
    Announcer(session)("The pasta timer is up.")
    await asyncio.sleep(0)
    assert session.messages


async def test_a_failed_announcement_goes_back_in_the_queue():
    """The session dies mid-send during a reconnect; the words are not less true for it."""
    requeued: list[str] = []
    announcer = Announcer(FakeSession(fail=True), requeue=requeued.append)
    announcer("Your timer is up.")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert requeued == ["Your timer is up."], "otherwise a reconnect eats the alarm's words"


async def test_a_delivered_announcement_is_not_requeued():
    requeued: list[str] = []
    announcer = Announcer(FakeSession(), requeue=requeued.append)
    announcer("Lamp off.")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert requeued == []

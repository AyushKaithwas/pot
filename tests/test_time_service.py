"""What happens when an alarm comes due.

Two things went wrong on a real device and are pinned here: an alarm that promised to play a song
and then only chimed, and a ringing alarm that nobody could say "stop" to.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from pot.alarms import AlarmStore
from pot.config import Settings
from pot.skills.time.service import TimeService


class FakeRinger:
    def __init__(self) -> None:
        self.started = ""
        self.stopped = False
        self.on_gave_up = None

    def start(self, label: str, *, on_gave_up=None) -> None:
        self.started = label
        self.on_gave_up = on_gave_up

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def parts(tmp_path: Path):
    said: list[str] = []
    ringer = FakeRinger()
    service = TimeService(
        Settings(), store=AlarmStore(tmp_path / "alarms.json"), ringer=ringer, announce=said.append
    )
    return service, ringer, said


def test_an_alarm_with_something_to_do_rings_and_does_it(parts):
    """ "Alarm in 30 seconds, and set the light red" is both halves, and it was asked for as an alarm."""
    service, ringer, said = parts
    alarm = service.add_timer(1, "alarm", then="set the light to red")
    service._check(alarm.due_at + timedelta(seconds=1))

    assert ringer.started, "an alarm you can't hear isn't an alarm"
    assert service.ringing is not None, "and it keeps ringing until the user stops it"
    assert "set the light to red" in said[0]
    assert "call stop_alarm first" in said[0], "a job that makes noise has to silence the chime"
    assert not said[0].startswith("Call stop_alarm"), "otherwise it silences an alarm nobody dismissed"


def test_a_job_with_a_time_on_it_does_not_ring(parts):
    """ "Turn the lights on in ten seconds" is not an alarm, so there is nothing to chime or stop."""
    service, ringer, said = parts
    alarm = service.add_timer(1, "lights", then="turn the lights on", ring=False)
    service._check(alarm.due_at + timedelta(seconds=1))

    assert not ringer.started
    assert service.ringing is None, 'so "stop" is never needed and never misfires'
    assert "turn the lights on" in said[0]


def test_whether_it_rings_survives_a_restart(parts, tmp_path: Path):
    service, _, _ = parts
    service.add_timer(60, "lights", then="turn the lights on", ring=False)
    assert AlarmStore(tmp_path / "alarms.json").sorted()[0].ring is False


def test_a_plain_alarm_still_rings(parts):
    service, ringer, said = parts
    alarm = service.add_timer(1, "pasta")
    service._check(alarm.due_at + timedelta(seconds=1))

    assert ringer.started == "pasta"
    assert service.ringing is not None
    assert said == ["The pasta timer is up."]


def test_only_one_thing_rings_at_a_time(parts):
    service, ringer, _ = parts
    first = service.add_timer(1, "pasta")
    service.add_timer(1, "eggs")
    service._check(first.due_at + timedelta(seconds=1))
    assert service.ringing.label == "pasta"


def test_stopping_clears_the_ring(parts):
    service, ringer, _ = parts
    alarm = service.add_timer(1, "pasta")
    service._check(alarm.due_at + timedelta(seconds=1))

    assert service.dismiss() is not None
    assert service.ringing is None
    assert ringer.stopped


def test_what_to_do_survives_a_restart(parts, tmp_path: Path):
    service, _, _ = parts
    service.add_timer(60, "alarm", then="play the news")
    assert AlarmStore(tmp_path / "alarms.json").sorted()[0].then == "play the news"


def test_an_unanswered_alarm_clears_itself_when_the_ring_gives_up(parts):
    """The ring times out after a few minutes; the "something is ringing" state must not outlive it.

    Left set, the mic stays open (the idle closer keeps the window alive while an alarm rings),
    music stays ducked, and the next alarm finds the slot occupied. Until a restart.
    """
    service, ringer, _ = parts
    alarm = service.add_timer(1, "unanswered")
    service._check(alarm.due_at + timedelta(seconds=1))
    assert service.ringing is not None

    assert ringer.on_gave_up is not None, "the ringer must be told what to do on timeout"
    ringer.on_gave_up()

    assert service.ringing is None, "the slot is free for the next alarm"
    assert ringer.stopped, "and the ring state is fully torn down"


def test_a_second_simultaneous_alarm_waits_instead_of_vanishing(parts):
    """Two alarms due together: the old code settled the second while the first rang, deleting it
    from the store without it ever making a sound."""
    service, ringer, said = parts
    first = service.add_timer(1, "first")
    second = service.add_timer(1, "second")
    moment = max(first.due_at, second.due_at) + timedelta(seconds=1)

    service._check(moment)
    assert service.ringing.label == "first"
    assert [a.label for a in service.pending()] == ["second"], "still owed a ring"

    service.dismiss()
    service._check(moment + timedelta(seconds=1))
    assert service.ringing.label == "second", "it fires as soon as the first is dealt with"
    assert said[-1] == "The second timer is up."

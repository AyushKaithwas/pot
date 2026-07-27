"""Alarms: persistence, recurrence and snoozing.

An alarm is only useful if it's still there after a restart and if "every weekday at 7" really
means 7 next weekday. Both are easy to get quietly wrong, so they're pinned here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from pot.alarms import Alarm, AlarmStore, Kind, Repeat, describe_all, next_datetime

# A Wednesday, so weekday and weekend rules are both easy to reason about.
WEDNESDAY = datetime(2026, 7, 29, 9, 30)


@pytest.fixture
def store(tmp_path: Path) -> AlarmStore:
    return AlarmStore(tmp_path / "alarms.json")


def test_a_timer_and_an_alarm_are_both_remembered(store: AlarmStore, tmp_path: Path):
    store.add(Kind.TIMER, WEDNESDAY + timedelta(minutes=10), label="pasta")
    store.add(
        Kind.ALARM, WEDNESDAY + timedelta(hours=21), label="gym", repeat=Repeat.WEEKDAYS, at_time=time(7, 0)
    )

    reloaded = AlarmStore(tmp_path / "alarms.json")
    assert [alarm.label for alarm in reloaded.sorted()] == ["pasta", "gym"]
    gym = next(alarm for alarm in reloaded.sorted() if alarm.label == "gym")
    assert gym.repeat is Repeat.WEEKDAYS
    assert gym.at_time == time(7, 0), "a repeating alarm has to remember its time of day"


def test_a_restart_fires_what_was_missed(store: AlarmStore, tmp_path: Path):
    """Waking up late beats not waking up, so an overdue alarm is still due after a reboot."""
    store.add(Kind.ALARM, WEDNESDAY - timedelta(hours=2), label="missed")
    reloaded = AlarmStore(tmp_path / "alarms.json")
    assert [alarm.label for alarm in reloaded.due(WEDNESDAY)] == ["missed"]


def test_a_corrupt_file_does_not_stop_pot_starting(tmp_path: Path):
    path = tmp_path / "alarms.json"
    path.write_text("{ this is not json")
    assert AlarmStore(path).sorted() == []


def test_one_unreadable_entry_does_not_lose_the_others(tmp_path: Path):
    path = tmp_path / "alarms.json"
    path.write_text(
        '{"alarms": [{"id": "1", "kind": "nonsense", "due_at": "x"}, '
        '{"id": "2", "kind": "timer", "due_at": "2026-07-29T10:00:00"}]}'
    )
    assert [alarm.id for alarm in AlarmStore(path).sorted()] == ["2"]


def test_ids_are_short_enough_to_say(store: AlarmStore):
    first = store.add(Kind.TIMER, WEDNESDAY)
    second = store.add(Kind.TIMER, WEDNESDAY)
    assert (first.id, second.id) == ("1", "2")


def test_a_cancelled_id_gets_reused(store: AlarmStore):
    first = store.add(Kind.TIMER, WEDNESDAY)
    store.remove(first.id)
    assert store.add(Kind.TIMER, WEDNESDAY).id == "1"


def test_a_one_off_disappears_after_firing(store: AlarmStore):
    alarm = store.add(Kind.ALARM, WEDNESDAY, label="once")
    assert store.settle(alarm, WEDNESDAY) is None
    assert store.sorted() == []


def test_weekdays_skips_the_weekend(store: AlarmStore):
    friday = datetime(2026, 7, 31, 7, 0)
    alarm = store.add(Kind.ALARM, friday, label="work", repeat=Repeat.WEEKDAYS, at_time=time(7, 0))
    following = store.settle(alarm, friday)
    assert following is not None
    assert following.due_at == datetime(2026, 8, 3, 7, 0), "Friday's next is Monday"


def test_daily_moves_to_tomorrow_at_the_same_time(store: AlarmStore):
    alarm = store.add(Kind.ALARM, WEDNESDAY, label="pills", repeat=Repeat.DAILY, at_time=time(9, 30))
    following = store.settle(alarm, WEDNESDAY)
    assert following is not None and following.due_at == WEDNESDAY + timedelta(days=1)


def test_weekly_keeps_the_same_weekday(store: AlarmStore):
    alarm = store.add(Kind.ALARM, WEDNESDAY, label="bins", repeat=Repeat.WEEKLY, at_time=time(9, 30))
    following = store.settle(alarm, WEDNESDAY)
    assert following is not None and following.due_at.weekday() == WEDNESDAY.weekday()
    assert following.due_at == WEDNESDAY + timedelta(days=7)


def test_weekends_only_lands_on_saturday_or_sunday(store: AlarmStore):
    alarm = store.add(Kind.ALARM, WEDNESDAY, label="lie in", repeat=Repeat.WEEKENDS, at_time=time(9, 30))
    following = store.settle(alarm, WEDNESDAY)
    assert following is not None and following.due_at.weekday() >= 5


def test_snoozing_keeps_the_alarm_and_moves_it(store: AlarmStore):
    alarm = store.add(Kind.ALARM, WEDNESDAY, label="wake")
    snoozed = store.snooze(alarm, 9, now=WEDNESDAY)
    assert snoozed.due_at == WEDNESDAY + timedelta(minutes=9)
    assert snoozed.snoozed_from == WEDNESDAY, "so a second snooze doesn't lose the original time"


def test_the_next_matching_time_is_today_if_it_has_not_passed():
    assert next_datetime(time(18, 0), now=WEDNESDAY) == datetime(2026, 7, 29, 18, 0)


def test_the_next_matching_time_rolls_over_to_tomorrow():
    assert next_datetime(time(7, 0), now=WEDNESDAY) == datetime(2026, 7, 30, 7, 0)


def test_an_explicit_date_is_honoured_even_if_it_has_passed():
    assert next_datetime(time(7, 0), on=date(2026, 8, 5), now=WEDNESDAY) == datetime(2026, 8, 5, 7, 0)


def test_descriptions_are_meant_to_be_said_out_loud(store: AlarmStore):
    timer = Alarm("1", Kind.TIMER, WEDNESDAY + timedelta(minutes=3, seconds=20), label="pasta")
    assert timer.describe(WEDNESDAY) == "1: pasta, 3m 20s left"

    tomorrow = Alarm("2", Kind.ALARM, datetime(2026, 7, 30, 7, 0), label="gym")
    assert tomorrow.describe(WEDNESDAY) == "2: gym at 07:00 tomorrow"

    repeating = Alarm(
        "3", Kind.ALARM, datetime(2026, 7, 30, 7, 0), label="work", repeat=Repeat.WEEKDAYS, at_time=time(7, 0)
    )
    assert "on weekdays" in repeating.describe(WEDNESDAY)


def test_nothing_set_says_so():
    assert describe_all([], WEDNESDAY) == "Nothing is set."

"""Timer lengths, which have to survive the model saying them out loud.

The bug this pins: "set an alarm in 10 seconds" became a ten minute timer, and Pot read back
"10 seconds" anyway, so on a device with no screen nothing looked wrong until it never rang.
"""

from __future__ import annotations

import pytest

from pot.skills.time import _to_seconds


@pytest.mark.parametrize(
    ("duration", "unit", "expected"),
    [
        (10, "seconds", 10),
        (10, "second", 10),
        (10, "sec", 10),
        (10, "s", 10),
        (10, "minutes", 600),
        (10, "min", 600),
        (10, "m", 600),
        (2, "hours", 7200),
        (2, "HOURS", 7200),
        (2, " hour ", 7200),
    ],
)
def test_a_count_and_a_unit_beat_a_multiplication(duration: int, unit: str, expected: int):
    assert _to_seconds(duration, unit) == expected


@pytest.mark.parametrize("unit", ["fortnights", "", "days"])
def test_an_unknown_unit_is_refused_rather_than_guessed(unit: str):
    assert _to_seconds(10, unit) is None

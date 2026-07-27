from __future__ import annotations

import asyncio

import pytest

from pot.scheduler import Scheduler


async def test_job_runs_after_the_delay():
    scheduler = Scheduler()
    fired = asyncio.Event()
    scheduler.schedule(0.01, "test", lambda: _set(fired))
    await asyncio.wait_for(fired.wait(), timeout=1)
    assert scheduler.jobs() == []


async def test_pending_jobs_are_listed_with_ids():
    scheduler = Scheduler()
    first = scheduler.schedule(5, "lamp off", _noop)
    second = scheduler.schedule(10, "pasta", _noop)
    assert [job.id for job in scheduler.jobs()] == [first.id, second.id] == ["t1", "t2"]
    assert "lamp off" in first.describe()


async def test_cancel_removes_the_job_and_it_never_fires():
    scheduler = Scheduler()
    fired = asyncio.Event()
    job = scheduler.schedule(0.05, "test", lambda: _set(fired))
    assert scheduler.cancel(job.id) is job
    await asyncio.sleep(0.1)
    assert not fired.is_set()
    assert scheduler.jobs() == []


async def test_cancel_all_and_unknown_id():
    scheduler = Scheduler()
    scheduler.schedule(5, "a", _noop)
    scheduler.schedule(5, "b", _noop)
    assert len(scheduler.cancel_all()) == 2
    assert scheduler.cancel("t99") is None


async def test_a_failing_job_does_not_break_the_scheduler():
    scheduler = Scheduler()

    async def boom() -> None:
        raise RuntimeError("nope")

    scheduler.schedule(0.01, "boom", boom)
    await asyncio.sleep(0.05)
    assert scheduler.jobs() == []


def test_negative_delay_is_rejected():
    with pytest.raises(ValueError):
        Scheduler().schedule(-1, "past", _noop)


async def _noop() -> None:
    return None


async def _set(event: asyncio.Event) -> None:
    event.set()

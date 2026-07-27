"""Deferred actions: timers, reminders, and "turn it off in ten minutes".

This is what lets one spoken sentence schedule work instead of needing a second turn later.
Jobs are plain asyncio tasks with short spoken-friendly ids ("t1", "t2").
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    label: str
    due_at: datetime
    task: asyncio.Task | None = field(default=None, repr=False)

    def remaining(self) -> timedelta:
        return max(timedelta(0), self.due_at - datetime.now())

    def describe(self) -> str:
        seconds = int(self.remaining().total_seconds())
        left = f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"
        return f"{self.id}: {self.label}, {left} left"


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    def schedule(self, delay_seconds: float, label: str, action: Callable[[], Awaitable[None]]) -> Job:
        if delay_seconds < 0:
            raise ValueError("delay must not be negative")
        self._counter += 1
        job = Job(
            id=f"t{self._counter}",
            label=label,
            due_at=datetime.now() + timedelta(seconds=delay_seconds),
        )
        job.task = asyncio.create_task(self._run(job, delay_seconds, action), name=f"job-{job.id}")
        self._jobs[job.id] = job
        return job

    def jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.due_at)

    def cancel(self, job_id: str) -> Job | None:
        job = self._jobs.pop(job_id, None)
        if job is not None and job.task is not None:
            job.task.cancel()
        return job

    def cancel_all(self) -> list[Job]:
        return [job for job_id in list(self._jobs) if (job := self.cancel(job_id)) is not None]

    async def _run(self, job: Job, delay: float, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(delay)
            await action()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled job %s (%s) failed", job.id, job.label)
        finally:
            self._jobs.pop(job.id, None)

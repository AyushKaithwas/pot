"""The sound an alarm makes until you deal with it.

Speaking "your timer is up" once is not an alarm: miss the sentence and it's gone. This repeats a
chime through the speaker, getting slightly louder for the first few rounds, until it's dismissed,
snoozed, or gives up after a few minutes so a forgotten alarm doesn't ring forever in an empty
house.

It rings on the Speaker's chime channel: mixed over speech and music rather than queued behind
them, still in the echo reference so the wake word can hear "stop" over the top, and silenced on
dismissal without cutting off whatever Pot was saying.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pot.audio.cues import tone
from pot.audio.playback import Speaker

log = logging.getLogger(__name__)

# A two-note chime, repeated. Long enough to notice, short enough to talk over.
CHIME_NOTES = [880.0, 1180.0]
NOTE_SECONDS = 0.18
GAP_SECONDS = 0.45
# Start gentle and grow, the way a decent alarm clock does.
START_VOLUME = 0.25
MAX_VOLUME = 0.9
VOLUME_STEP = 0.12
# Long enough to wake someone, short enough not to annoy the neighbours.
MAX_RING_SECONDS = 180.0


class Ringer:
    """Rings until told to stop."""

    def __init__(self, speaker: Speaker, *, sample_rate: int, max_seconds: float = MAX_RING_SECONDS):
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._max_seconds = max_seconds
        self._task: asyncio.Task | None = None
        self._on_gave_up: Callable[[], None] | None = None
        self.label = ""

    @property
    def ringing(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, label: str = "", *, on_gave_up: Callable[[], None] | None = None) -> None:
        """Ring until stopped. `on_gave_up` fires only if the ring times out unanswered.

        Whoever started the ring holds state that says "something is ringing", and without this
        callback that state outlives the noise: the mic stays open, music stays ducked, and the
        next alarm finds the slot occupied.
        """
        if self.ringing:
            return
        self.label = label
        self._on_gave_up = on_gave_up
        self._task = asyncio.create_task(self._ring(), name="ringer")

    def stop(self) -> None:
        task, self._task = self._task, None
        self.label = ""
        if task is not None:
            task.cancel()
        # Only the chime: flushing the voice channel would cut Pot off mid-sentence, and the
        # dismissal usually arrives while Pot is saying something.
        self._speaker.stop_chime()

    async def wait(self) -> None:
        """For tests and shutdown: block until the ring finishes on its own."""
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _ring(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_seconds
        volume = START_VOLUME
        try:
            while loop.time() < deadline:
                self._speaker.write_chime(
                    tone(
                        CHIME_NOTES,
                        sample_rate=self._sample_rate,
                        seconds=NOTE_SECONDS,
                        volume=volume,
                        gap_seconds=0.05,
                    )
                )
                volume = min(MAX_VOLUME, volume + VOLUME_STEP)
                await asyncio.sleep(GAP_SECONDS + NOTE_SECONDS * len(CHIME_NOTES))
        except asyncio.CancelledError:
            raise
        finally:
            self.label = ""
        log.info("alarm rang for %.0fs with no response", self._max_seconds)
        if self._on_gave_up is not None:
            self._on_gave_up()

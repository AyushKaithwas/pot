"""Push-to-talk keys for the terminal.

Space toggles the mic, q quits. The mic also closes by itself as soon as Pot starts replying,
which is what keeps a laptop's speakers from talking to its own microphone. Wake word will
replace this later by opening the same gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import termios
import tty

log = logging.getLogger(__name__)


@contextlib.contextmanager
def raw_terminal():
    """Read single keypresses without waiting for Enter."""
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        # Drop anything still typed, or the keypress that quit lands on the shell prompt.
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


async def keypresses():
    """Yield single characters from stdin as they are typed."""
    if not sys.stdin.isatty():
        return
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def on_readable() -> None:
        char = sys.stdin.read(1)
        if char:
            queue.put_nowait(char)

    loop.add_reader(sys.stdin.fileno(), on_readable)
    try:
        while True:
            yield await queue.get()
    finally:
        loop.remove_reader(sys.stdin.fileno())

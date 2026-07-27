"""One console for the whole process.

Rich coordinates a live spinner with ordinary printing only through a single Console instance, so
anything that writes to the terminal has to share this one or it will scribble over the spinner.
"""

from __future__ import annotations

from rich.console import Console

console = Console()

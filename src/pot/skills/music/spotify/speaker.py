"""Pot as a Spotify Connect speaker.

This is the Echo model: the device itself runs a Spotify client and audio streams to it
directly, while Pot only sends control commands. librespot is what provides that client here.

Pot supervises the process rather than bundling it. librespot is an unofficial client, so it
stays optional and off by default: everything still works by controlling the user's phone or
desktop, which is the path that's actually within Spotify's terms.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from pot.config import Settings

log = logging.getLogger(__name__)

RESTART_DELAY_SECONDS = 3.0
STOP_GRACE_SECONDS = 5.0

INSTALL_HINTS = {
    "darwin": "brew install librespot",
    "linux": "sudo apt install librespot   # or see github.com/librespot-org/librespot",
}


def install_hint() -> str:
    return INSTALL_HINTS.get(sys.platform, "see github.com/librespot-org/librespot")


class ManagedSpeaker:
    """Runs librespot for as long as Pot is running, restarting it if it dies."""

    def __init__(self, settings: Settings) -> None:
        self._config = settings.music.speaker
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def binary_path(self) -> str | None:
        return shutil.which(self._config.binary)

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        if self.binary_path() is None:
            log.warning(
                "Pot's speaker is enabled but %s isn't installed (%s)",
                self._config.binary,
                install_hint(),
            )
            return
        self._supervisor = asyncio.create_task(self._supervise(), name="librespot")

    async def stop(self) -> None:
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await self._terminate()

    def command(self) -> list[str]:
        command = [
            self._config.binary,
            "--name",
            self._config.name,
            "--backend",
            self._config.backend,
            "--bitrate",
            str(self._config.bitrate),
            "--cache",
            str(self._config.cache_path()),
        ]
        if self._config.device:
            command += ["--device", self._config.device]
        return command

    async def _supervise(self) -> None:
        while True:
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command(),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                log.warning("could not start %s: %s", self._config.binary, exc)
                return
            log.info("Pot's Spotify speaker %r is up", self._config.name)
            code = await self._process.wait()
            if code == 0:
                return
            log.warning("librespot exited with %s, restarting", code)
            await asyncio.sleep(RESTART_DELAY_SECONDS)

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()

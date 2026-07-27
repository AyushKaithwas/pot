"""Running as a service, so a device starts talking after a power cut without anyone logging in.

A *user* unit rather than a system one: no root anywhere, audio and the user's config just work,
and Pot never has to invoke sudo on its own behalf. The cost is `loginctl enable-linger`, one
command, which is what lets a user unit run with nobody logged in.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "pot.service"

UNIT_TEMPLATE = """\
[Unit]
Description={description}
Documentation=https://github.com/{repo}
# The wake word needs models on disk and the realtime API needs the network, so don't race boot.
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} talk
WorkingDirectory={working_directory}
Restart=always
RestartSec=5
# A voice assistant that dies quietly is indistinguishable from a broken one, so keep the logs.
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def is_supported() -> bool:
    """systemd is Linux-only; on macOS this whole command is a documentation exercise."""
    return sys.platform.startswith("linux")


def unit_directory() -> Path:
    return Path.home() / ".config/systemd/user"


def unit_path() -> Path:
    return unit_directory() / UNIT_NAME


def executable() -> str:
    """The installed `pot`, so the unit doesn't depend on a shell or a virtualenv being active."""
    found = shutil.which("pot")
    if found:
        return found
    # Running from a checkout without the venv on PATH: point at the venv's script directly.
    candidate = Path(sys.executable).with_name("pot")
    return str(candidate if candidate.exists() else "pot")


def render_unit(*, description: str = "Pot voice assistant", repo: str = "ayuzz/pot") -> str:
    return UNIT_TEMPLATE.format(
        description=description,
        repo=repo,
        executable=executable(),
        working_directory=Path.home(),
    )


def write_unit(text: str | None = None) -> Path:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else render_unit())
    return path


def enable_commands() -> list[str]:
    """What the user runs next. Pot writes the unit but never enables things behind your back."""
    return [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now pot",
        f"sudo loginctl enable-linger {Path.home().name}",
    ]


def run_systemctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True, check=False)


def journal(lines: int = 50, follow: bool = False) -> list[str]:
    command = ["journalctl", "--user", "-u", UNIT_NAME, "-n", str(lines)]
    if follow:
        command.append("-f")
    return command

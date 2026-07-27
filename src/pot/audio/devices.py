"""PortAudio device lookup, shared by capture and playback."""

from __future__ import annotations

import sounddevice as sd


class AudioDeviceError(RuntimeError):
    pass


def find_device(name_fragment: str, *, kind: str) -> int | None:
    """Resolve a device by case-insensitive name fragment. Empty fragment means system default."""
    if not name_fragment:
        return None
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    wanted = name_fragment.lower()
    for index, device in enumerate(sd.query_devices()):
        if device[channels_key] > 0 and wanted in device["name"].lower():
            return index
    available = ", ".join(device["name"] for device in sd.query_devices() if device[channels_key] > 0)
    raise AudioDeviceError(f"no {kind} device matching {name_fragment!r}. Available: {available}")


def default_samplerate(device: int | None, *, kind: str) -> int:
    info = sd.query_devices(device, kind)
    return int(info["default_samplerate"])


def describe(device: int | None, *, kind: str) -> str:
    info = sd.query_devices(device, kind)
    return f"{info['name']} @ {int(info['default_samplerate'])} Hz"

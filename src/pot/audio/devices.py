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


def stream_rate(device: int | None, *, kind: str, wanted: int) -> int:
    """The rate to actually open the stream at, which is the one the hardware is running.

    PortAudio accepts a rate the device isn't running at and quietly lets the operating system
    convert. That conversion is somebody else's resampler: on a 48 kHz Mac fed a 24 kHz stream it
    adds a harsh buzz over the top of otherwise clean speech. Opening the device at its own rate
    and converting with soxr is both better and the same on every machine.
    """
    native = default_samplerate(device, kind=kind)
    if native == wanted:
        return wanted
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    try:
        check(device=device, samplerate=native, channels=1)
    except Exception:
        return wanted
    return native


def describe(device: int | None, *, kind: str) -> str:
    info = sd.query_devices(device, kind)
    return f"{info['name']} @ {int(info['default_samplerate'])} Hz"

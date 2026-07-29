"""`pot` command line: talk to it, or drive the pieces directly while debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import typer
from rich.table import Table

from pot.config import Settings, devices_file, load_settings
from pot.console import console
from pot.runtime import Runtime, set_runtime
from pot.skills.lights import discovery
from pot.skills.lights.provider import LightChange
from pot.skills.lights.service import get_service

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Pot, a voice assistant.")
lights_app = typer.Typer(no_args_is_help=True, help="Control and inspect the lights directly.")
music_app = typer.Typer(no_args_is_help=True, help="Sign in to Spotify and drive playback.")
audio_app = typer.Typer(no_args_is_help=True, help="Check the microphone, speaker and wake word.")
config_app = typer.Typer(no_args_is_help=True, help="Where settings live, and what Pot resolved.")
service_app = typer.Typer(no_args_is_help=True, help="Run Pot at boot on a Linux device.")
alarms_app = typer.Typer(no_args_is_help=True, help="Inspect timers and alarms outside a conversation.")
app.add_typer(lights_app, name="lights")
app.add_typer(music_app, name="music")
app.add_typer(audio_app, name="audio")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")
app.add_typer(alarms_app, name="alarms")


def _setup(verbose: bool = False) -> Runtime:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    runtime = Runtime(load_settings())
    set_runtime(runtime)
    return runtime


@app.command()
def talk(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    hands_free: bool = typer.Option(
        False, "--hands-free", help="Always-on mic, for a device with no keyboard"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="No earcons"),
) -> None:
    """Start a voice conversation."""
    from pot.voice import run_voice

    settings = _setup(verbose).settings
    if hands_free or quiet:
        audio = settings.audio.model_copy(
            update={
                "mode": "always_on" if hands_free else settings.audio.mode,
                "cues": not quiet and settings.audio.cues,
            }
        )
        settings = settings.model_copy(update={"audio": audio})
    try:
        asyncio.run(run_voice(settings))
    except KeyboardInterrupt:
        console.print("bye")


@app.command()
def skills() -> None:
    """List discovered skills and whether they are enabled."""
    from pot.skills import discover, load

    settings = _setup().settings
    enabled = {skill.name for skill in load(settings)}
    table = Table("skill", "enabled", "tools", "description")
    for name, skill in sorted(discover().items()):
        table.add_row(
            name,
            "yes" if name in enabled else "no",
            str(len(skill.tools)),
            skill.description,
        )
    console.print(table)


@app.command()
def costs() -> None:
    """Show the configured per-million-token rates, and what the current setup uses."""
    settings = _setup().settings
    table = Table("model", "audio in", "cached audio in", "audio out", "text in", "text out")
    for name, rates in sorted(settings.costs.models.items()):
        table.add_row(
            name,
            f"${rates.audio_in:g}",
            f"${rates.cached_audio_in:g}",
            f"${rates.audio_out:g}",
            f"${rates.text_in:g}",
            f"${rates.text_out:g}",
        )
    console.print(table)
    console.print(
        f"voice: [bold]{settings.voice.model}[/bold], capped at "
        f"{settings.voice.max_output_tokens} output tokens"
    )
    console.print(
        f"research: [bold]{settings.research.model}[/bold] for quick lookups, "
        f"[bold]{settings.research.deep_model}[/bold] when asked for depth"
    )
    if settings.voice.transcribe_input:
        console.print(
            f"[yellow]input transcription on[/yellow] ({settings.voice.transcription_model}), "
            "billed per minute on top of the audio"
        )
    for model in _priced_models(settings):
        if model not in settings.costs.models:
            console.print(f"[yellow]no rates configured for {model}[/yellow]")


@app.command()
def doctor() -> None:
    """Check keys, audio devices and light reachability."""
    settings = _setup().settings
    console.print(f"[bold]config[/bold] {settings.assistant.name}, voice model {settings.voice.model}")
    if settings.service.hosted:
        _check(
            f"model access via {settings.service.base_url or settings.service.realtime_url}",
            bool(settings.service_token),
            fix="set POT_SERVICE_TOKEN in ~/.config/pot/env",
        )
    else:
        _check("OPENAI_API_KEY", bool(settings.openai_api_key), fix="put it in ~/.config/pot/env")
    _check(
        "Tuya cloud credentials",
        settings.has_tuya_cloud,
        fix="TUYA_API_KEY, TUYA_API_SECRET and TUYA_API_REGION in ~/.config/pot/env; see the README",
    )
    unpriced = [model for model in _priced_models(settings) if model not in settings.costs.models]
    _check(
        "cost rates for every configured model" + (f" (missing: {', '.join(unpriced)})" if unpriced else ""),
        not unpriced,
    )

    _check_audio(settings)
    _check_music(settings)

    devices = settings.lights.devices
    if not devices:
        from pot.config import config_file

        console.print(f"[yellow]no lights configured in {config_file()}[/yellow]")
        return
    for device in devices.values():
        _check(
            f"local key for {device.spoken_name}",
            bool(device.local_key),
            fix="run `pot lights keys` once the Tuya project is linked",
        )

    found = asyncio.run(discovery.scan(timeout=6.0))
    by_gwid = {d.gwid: d for d in found}
    for device in devices.values():
        hit = by_gwid.get(device.gwid)
        detail = f"{hit.ip}, protocol {hit.version}" if hit else "not answering on the LAN"
        _check(f"{device.spoken_name} on the network ({detail})", hit is not None)


@lights_app.command("scan")
def lights_scan(timeout: float = typer.Option(8.0, help="How long to listen, in seconds")) -> None:
    """Listen for Tuya devices broadcasting on the LAN."""
    settings = _setup().settings
    known = {d.gwid: d for d in settings.lights.devices.values()}
    found = asyncio.run(discovery.scan(timeout=timeout))
    if not found:
        console.print("[yellow]nothing heard. Same Wi-Fi? Device powered?[/yellow]")
        return
    table = Table("gwId", "ip", "protocol", "configured as")
    for device in found:
        configured = known.get(device.gwid)
        table.add_row(
            device.gwid,
            device.ip,
            str(device.version),
            configured.spoken_name if configured else "[dim]unconfigured[/dim]",
        )
    console.print(table)


@lights_app.command("list")
def lights_list() -> None:
    """Show configured lights and their current state."""
    runtime = _setup()

    async def run() -> None:
        service = get_service()
        outcomes = await service.read(list(service.devices.values()))
        table = Table("light", "state", "aliases")
        for outcome in outcomes:
            state = outcome.error or outcome.state.describe() if outcome.state else outcome.error
            table.add_row(outcome.device.spoken_name, state or "unknown", ", ".join(outcome.device.aliases))
        console.print(table)
        await runtime.close()

    asyncio.run(run())


@lights_app.command("set")
def lights_set(
    target: str = typer.Argument(..., help='Light name, room, or "all"'),
    on: bool | None = typer.Option(None, "--on/--off", help="Switch the light on or off"),
    brightness: int | None = typer.Option(None, "--brightness", "-b", min=1, max=100),
    temperature: int | None = typer.Option(None, "--temp", "-t", min=0, max=100),
    color: str | None = typer.Option(None, "--color", help="6-digit hex, colour-capable lights only"),
) -> None:
    """Apply a change to a light: `pot lights set beta1 --on -b 30`."""
    runtime = _setup()
    change = LightChange(on, brightness, temperature, color)
    if change.is_empty():
        raise typer.BadParameter("give at least one of --on/--off, --brightness, --temp, --color")

    async def run() -> None:
        service = get_service()
        devices = service.resolve_many([target])
        for outcome in await service.apply(devices, change):
            console.print(outcome.describe())
        await runtime.close()

    asyncio.run(run())


@lights_app.command("scene")
def lights_scene(name: str = typer.Argument(..., help="Scene name from config/pot.toml")) -> None:
    """Apply a configured scene."""
    runtime = _setup()

    async def run() -> None:
        for outcome in await get_service().apply_scene(name):
            console.print(outcome.describe())
        await runtime.close()

    asyncio.run(run())


@lights_app.command("dps")
def lights_dps(target: str = typer.Argument(..., help="Light name")) -> None:
    """Dump a light's raw data points, to work out what it actually supports."""
    import tinytuya

    _setup()

    async def run() -> None:
        device = get_service().resolve(target)[0]
        if not device.local_key:
            console.print("[red]no local key; run `pot lights keys` first[/red]")
            raise typer.Exit(1)
        found = await discovery.find(device.gwid, refresh=True)
        if found is None:
            console.print("[red]device not answering on the LAN[/red]")
            raise typer.Exit(1)
        bulb = tinytuya.BulbDevice(
            dev_id=device.gwid,
            address=found.ip,
            local_key=device.local_key,
            version=device.version or found.version,
        )
        status = await asyncio.to_thread(bulb.status)
        console.print_json(data=status)
        detected = await asyncio.to_thread(bulb.detect_available_dps)
        console.print_json(data=detected)
        await asyncio.to_thread(bulb.close)

    asyncio.run(run())


@lights_app.command("keys")
def lights_keys() -> None:
    """Fetch device local keys from the Tuya cloud project into config/devices.json."""
    import tinytuya

    settings = _setup().settings
    if not settings.has_tuya_cloud:
        console.print("[red]set TUYA_API_KEY / TUYA_API_SECRET / TUYA_API_REGION in .env first[/red]")
        raise typer.Exit(1)
    cloud = tinytuya.Cloud(
        apiRegion=settings.tuya_api_region,
        apiKey=settings.tuya_api_key,
        apiSecret=settings.tuya_api_secret,
    )
    devices = cloud.getdevices()
    if isinstance(devices, dict) and devices.get("Error"):
        console.print(f"[red]{devices['Error']}[/red]")
        raise typer.Exit(1)
    target = devices_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(devices, indent=2) + "\n")
    target.chmod(0o600)  # local keys are as good as a password on the LAN
    console.print(f"wrote {len(devices)} device(s) to {target}")
    for device in devices:
        console.print(
            f"  {device.get('name')} · {device.get('id')} · key {'yes' if device.get('key') else 'no'}"
        )


@service_app.command("install")
def service_install(
    print_only: bool = typer.Option(False, "--print", help="Show the unit instead of writing it"),
) -> None:
    """Write a systemd user unit so Pot starts at boot and restarts if it dies."""
    from pot import service

    unit = service.render_unit()
    if print_only:
        console.print(unit, highlight=False)
        return
    if not service.is_supported():
        console.print("[yellow]systemd is Linux-only, so this only helps on the Pi.[/yellow]")
        console.print("Here's the unit it would write:\n")
        console.print(unit, highlight=False)
        raise typer.Exit(1)

    path = service.write_unit(unit)
    console.print(f"wrote {path}\n\nnow run:")
    for command in service.enable_commands():
        console.print(f"  {command}")
    console.print(
        "\n[dim]the last one lets the service run with nobody logged in, "
        "and is the only step needing sudo[/dim]"
    )


@service_app.command("status")
def service_status() -> None:
    """Is Pot running?"""
    from pot import service

    if not service.is_supported():
        console.print("[yellow]no systemd here[/yellow]")
        raise typer.Exit(1)
    result = service.run_systemctl("status", service.UNIT_NAME, "--no-pager")
    console.print(result.stdout or result.stderr, highlight=False)


@service_app.command("logs")
def service_logs(
    lines: int = typer.Option(50, "--lines", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f"),
) -> None:
    """Pot's log, which on a headless box is the only way to see what it's doing."""
    import subprocess

    from pot import service

    if not service.is_supported():
        console.print("[yellow]no journald here[/yellow]")
        raise typer.Exit(1)
    subprocess.run(service.journal(lines, follow), check=False)


STARTER_CONFIG = """\
# Your Pot settings. Merged over the defaults shipped with Pot, so anything you leave out keeps
# the shipped value and an update never overwrites your choices. See the shipped config for every
# available option, or run `pot config defaults`.

[audio]
# wake_word needs `uv sync --extra wakeword`. It also means audio only reaches OpenAI after the
# word fires, so an idle room costs nothing.
# mode = "wake_word"

[music.spotify]
# From developer.spotify.com/dashboard, with http://127.0.0.1:8765/callback as a redirect URI.
client_id = ""

# Declare your lights here. `pot lights scan` prints the gwid of everything on the network.
# [lights.devices.tube]
# gwid = ""
# name = "Tube light"
# room = "bedroom"
# aliases = ["tube", "batten"]
"""


@config_app.command("path")
def config_path() -> None:
    """Where Pot reads settings, secrets and state from."""
    from pot.config import DEFAULTS_FILE, config_file, devices_file, env_file, state_dir

    for label, path in (
        ("your config", config_file()),
        ("shipped defaults", DEFAULTS_FILE),
        ("secrets", env_file()),
        ("state", state_dir()),
        ("device keys", devices_file()),
    ):
        mark = "" if path.exists() else " [dim](absent)[/dim]"
        console.print(f"  {label}: {path}{mark}")


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Write a starter config to ~/.config/pot/pot.toml."""
    from pot.config import config_file

    target = config_file()
    if target.exists() and not force:
        console.print(f"{target} already exists; --force to replace it")
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_CONFIG)
    console.print(f"wrote {target}")


@config_app.command("edit")
def config_edit() -> None:
    """Open your config in $EDITOR."""
    import os
    import subprocess

    from pot.config import config_file

    target = config_file()
    if not target.exists():
        config_init()
    editor = os.environ.get("EDITOR", "nano" if sys.platform.startswith("linux") else "open -e")
    subprocess.run([*editor.split(), str(target)], check=False)


@config_app.command("show")
def config_show() -> None:
    """Print the merged configuration Pot is actually using."""
    from pot.config import read_config

    console.print_json(data=read_config())


@alarms_app.command("list")
def alarms_list() -> None:
    """What's pending, read from the same file the assistant uses."""
    from pot.alarms import describe_all
    from pot.skills.time.service import get_service

    _setup()
    service = get_service()
    console.print(describe_all(service.pending()))
    console.print(f"[dim]{service.store.path}[/dim]")


@alarms_app.command("cancel")
def alarms_cancel(which: str = typer.Argument(..., help='An id from `alarms list`, or "all"')) -> None:
    """Cancel a timer or alarm without talking to Pot."""
    from pot.skills.time.service import get_service

    _setup()
    service = get_service()
    if which.strip().lower() == "all":
        console.print(f"cancelled {len(service.cancel_all())}")
        return
    alarm = service.cancel(which.strip())
    console.print(f"cancelled {alarm.describe()}" if alarm else f"nothing with id {which}")


def _check_audio(settings: Settings) -> None:
    from pot.audio.devices import AudioDeviceError, describe, find_device
    from pot.audio.diagnose import defaults

    audio = settings.audio
    for kind, wanted in (("input", audio.input_device), ("output", audio.output_device)):
        try:
            index = find_device(wanted, kind=kind)
            console.print(f"  {kind}:  {describe(index, kind=kind)}")
        except AudioDeviceError as exc:
            _check(f"{kind} device {wanted!r}: {exc}", False)
        except Exception:
            console.print(f"  {kind}:  {defaults().get(kind, 'unavailable')}")

    console.print(f"  mode: {audio.mode}")
    if audio.uses_wake_word:
        state = "on" if audio.echo_suppression else "off"
        console.print(f"  echo suppression: {state} (measure it with `pot audio echo`)")
        if settings.music.speaker.enabled:
            console.print(
                "  [yellow]music plays on Pot's own speaker while the wake word listens.[/yellow]\n"
                "  [dim]Pot can't subtract music (no reference signal): point [music] "
                "preferred_device at another speaker, or use an OS echo canceller.[/dim]"
            )
    if not audio.uses_wake_word:
        return
    from pot.audio.wake import WakeWordUnavailable, build, triggers_for

    console.print(f"  wake: listening for {', '.join(triggers_for(audio.wake))}")
    try:
        # Actually building it is the only honest check: it covers the extra being installed, the
        # models being present, and the names existing.
        build(audio)
    except WakeWordUnavailable as exc:
        _check(
            f"wake word ready ({exc})",
            False,
            fix="`uv sync --extra wakeword` installs it; models download themselves on first use",
        )
        return
    _check("wake word ready", True)


@audio_app.command("devices")
def audio_devices() -> None:
    """List audio devices. Put a piece of the name into input_device or output_device."""
    from pot.audio.diagnose import defaults, device_rows

    settings = _setup().settings
    table = Table("#", "device", "in", "out", "default rate")
    for row in device_rows():
        table.add_row(
            str(row["index"]),
            row["name"],
            str(row["inputs"]) if row["inputs"] else "",
            str(row["outputs"]) if row["outputs"] else "",
            f"{row['rate']} Hz",
        )
    console.print(table)
    for kind, description in defaults().items():
        console.print(f"  system {kind}: {description}")
    for kind, configured in (
        ("input", settings.audio.input_device),
        ("output", settings.audio.output_device),
    ):
        console.print(f"  configured {kind}: {configured or '[dim]system default[/dim]'}")


@audio_app.command("test")
def audio_test(seconds: float = typer.Option(3.0, "--seconds", "-s", min=0.5, max=30)) -> None:
    """Record, play it back, and report the level. The first thing to run on new hardware."""
    from pot.audio.diagnose import describe_level, peak_level, play, record

    settings = _setup().settings

    async def run() -> None:
        console.print(f"recording {seconds:g}s, say something")
        pcm = await record(settings.audio, seconds)
        console.print(describe_level(peak_level(pcm)))
        console.print("playing it back")
        await play(settings.audio, pcm)
        console.print("done")

    try:
        asyncio.run(run())
    except Exception as exc:
        console.print(f"[red]audio failed:[/red] {exc}")
        raise typer.Exit(1) from exc


@audio_app.command("cues")
def audio_cues() -> None:
    """Play the earcons, to set cue_volume by ear."""
    from pot.audio.cues import Cues
    from pot.audio.playback import Speaker

    settings = _setup().settings

    async def run() -> None:
        speaker = Speaker(
            sample_rate=settings.audio.sample_rate,
            device_name=settings.audio.output_device,
            prebuffer_ms=settings.audio.playback_buffer_ms,
        )
        async with speaker:
            cues = Cues(speaker, sample_rate=settings.audio.sample_rate, volume=settings.audio.cue_volume)
            for name, play_cue in (
                ("listening", cues.ready),
                ("mic closed", cues.closed),
                ("still working", cues.working),
                ("failed", cues.error),
            ):
                console.print(f"  {name}")
                play_cue()
                cues.reset_working()
                await asyncio.sleep(1.0)

    asyncio.run(run())


@audio_app.command("echo")
def audio_echo(seconds: float = typer.Option(4.0, "--seconds", "-s", min=2, max=20)) -> None:
    """Measure how much of Pot's own speaker the microphone hears, and what suppression removes.

    Run this with the volume where you normally keep it. If the mic barely hears the speaker, echo
    isn't your problem and music is (see the README: music has no reference to subtract).
    """
    import numpy as np

    from pot.audio.capture import Microphone
    from pot.audio.echo import EchoSuppressor
    from pot.audio.playback import Speaker

    settings = _setup().settings
    audio = settings.audio
    rate = audio.sample_rate

    def level(pcm: bytes) -> float:
        if not pcm:
            return 0.0
        return float(np.abs(np.frombuffer(pcm, dtype=np.int16)).mean())

    def noise(length: float) -> bytes:
        """Speech-shaped-ish noise: broadband, so it excites the whole band the mic cares about."""
        rng = np.random.default_rng(11)
        samples = rng.normal(0, 6000, int(rate * length))
        return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()

    async def measure(playing: bool) -> tuple[float, float, float]:
        echo = EchoSuppressor(rate, enabled=True)
        speaker = Speaker(
            sample_rate=rate,
            device_name=audio.output_device,
            prebuffer_ms=audio.playback_buffer_ms,
        )
        microphone = Microphone(sample_rate=rate, device_name=audio.input_device)
        raw, clean = [], []
        async with speaker, microphone:
            if playing:
                speaker.write(noise(seconds))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + seconds
            async for chunk in microphone.chunks():
                echo.playback(speaker.drain_reference())
                raw.append(level(chunk))
                cleaned = echo.process(chunk)
                if cleaned:
                    clean.append(level(cleaned))
                if loop.time() >= deadline:
                    break
        return (
            float(np.mean(raw or [0])),
            float(np.mean(clean or [0])),
            echo.delay_ms,
        )

    async def run() -> None:
        console.print(f"measuring the room for {seconds:g}s, stay quiet")
        quiet, _, _ = await measure(playing=False)
        console.print(f"  room noise: {quiet:.0f}")
        console.print(f"playing {seconds:g}s of test noise through the speaker")
        loud, suppressed, delay = await measure(playing=True)
        console.print(f"  mic heard: {loud:.0f}  (after suppression: {suppressed:.0f})")

        echo_above_room = loud / max(quiet, 1.0)
        console.print(f"\nspeaker is {echo_above_room:.1f}x louder in the mic than the room alone")
        if echo_above_room < 2.0:
            console.print(
                "[green]Pot barely hears itself[/green] on this hardware, so its own voice isn't "
                "your problem. Music from another app is a separate matter: there's no reference "
                "to subtract, so keep it off this speaker or use an OS echo canceller."
            )
            return
        gain_db = 20 * np.log10(max(loud, 1.0) / max(suppressed, 1.0))
        console.print(f"suppression removes {gain_db:.1f} dB, round trip estimated at {delay:.0f}ms")
        if gain_db < 3:
            console.print(
                "[yellow]that's not much.[/yellow] Move the mic away from the speaker, turn the "
                "volume down, or use an OS echo canceller (README)."
            )

    asyncio.run(run())


@audio_app.command("benchmark")
def audio_benchmark(
    repeats: int = typer.Option(3, "--repeats", "-n", min=1, max=10, help="Takes per phrase"),
    seconds: float = typer.Option(2.5, "--seconds", "-s", help="Recording window per take"),
) -> None:
    """Score every stock wake word against your own voice, and recommend one.

    The stock models are trained on synthetic speech, so which one hears you is close to luck.
    This measures it instead of guessing.
    """
    from pot.audio.diagnose import peak_level, record, score_recording
    from pot.audio.wake import available, download, is_downloaded, stock_models

    settings = _setup().settings
    engine = settings.audio.wake.engine
    if not available(engine):
        extra = "porcupine" if engine == "porcupine" else "wakeword"
        console.print(f"[red]{engine} not installed: uv sync --extra {extra}[/red]")
        raise typer.Exit(1)

    # The phrase triggers, not the timer/weather classifiers openWakeWord also ships.
    candidates = [name for name in stock_models(engine) if name not in {"timer", "weather"}]
    if engine == "porcupine":
        # Porcupine ships ~20 keywords; testing them all would take ten minutes of talking.
        candidates = [name for name in candidates if name in {"computer", "jarvis", "bumblebee", "picovoice"}]

    async def run() -> None:
        missing = [name for name in candidates if not is_downloaded(name)]
        if missing:
            console.print(f"fetching {len(missing)} model(s), about 1 MB each")
            download(*missing)

        totals: dict[str, list[float]] = {name: [] for name in candidates}
        for model in candidates:
            spoken = model.replace("_", " ")
            for take in range(1, repeats + 1):
                console.print(f'say "[bold]{spoken}[/bold]" ({take}/{repeats})')
                pcm = await record(settings.audio, seconds)
                level = peak_level(pcm)
                if level < 0.02:
                    console.print("  [yellow]heard nothing; is the right mic selected?[/yellow]")
                scores = await score_recording([model], pcm, settings.audio.sample_rate)
                peak = scores[model]["peak"]
                totals[model].append(peak)
                mark = "[green]would fire[/green]" if peak >= 0.5 else "[red]too quiet[/red]"
                console.print(f"  peak {peak:.3f} · level {level:.0%} · {mark}")

        table = Table("wake word", "hits", "best", "worst", "verdict")
        best_model, best_worst = "", -1.0
        for model, peaks in totals.items():
            hits = sum(1 for value in peaks if value >= 0.5)
            worst = min(peaks)
            if worst > best_worst:
                best_model, best_worst = model, worst
            verdict = "reliable" if hits == len(peaks) and worst > 0.7 else "patchy" if hits else "no"
            table.add_row(
                model.replace("_", " "), f"{hits}/{len(peaks)}", f"{max(peaks):.3f}", f"{worst:.3f}", verdict
            )
        console.print(table)
        if best_worst >= 0.7:
            console.print(f"best for your voice: [bold]{best_model}[/bold] (worst take {best_worst:.3f})")
            console.print(f'  set models = ["{best_model}"] under \\[audio.wake]')
        else:
            console.print(
                "[yellow]none of the stock models hear you reliably.[/yellow] A model trained on "
                "your own voice is the fix; see the wake word section of the README."
            )

    asyncio.run(run())


@audio_app.command("wake")
def audio_wake(seconds: float = typer.Option(30.0, "--seconds", "-s", help="How long to listen")) -> None:
    """Listen and report wake word scores, for tuning the threshold in the actual room."""
    from pot.audio.capture import Microphone
    from pot.audio.wake import WakeWordUnavailable, build

    settings = _setup().settings
    audio = settings.audio

    async def run() -> None:
        try:
            wake = build(audio)
        except WakeWordUnavailable as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        console.print(f'say "{wake.spoken}" · threshold {audio.wake.threshold} · listening {seconds:g}s')
        microphone = Microphone(sample_rate=audio.sample_rate, device_name=audio.input_device)
        best, hits = 0.0, 0
        async with microphone:
            deadline = asyncio.get_running_loop().time() + seconds
            async for chunk in microphone.chunks():
                if wake.feed(chunk):
                    hits += 1
                    console.print(f"[green]fired[/green] on {wake.last_model} at {wake.last_score:.3f}")
                best = max(best, wake.last_score)
                if asyncio.get_running_loop().time() >= deadline:
                    break
        console.print(f"peak score {best:.3f}, {hits} detection(s)")
        if hits == 0 and best > 0.2:
            console.print("[yellow]close but under the threshold; try lowering it[/yellow]")

    asyncio.run(run())


def _check_music(settings: Settings) -> None:
    from pot.skills.music.spotify.auth import SpotifyAuth, TokenStore
    from pot.skills.music.spotify.speaker import ManagedSpeaker, install_hint

    spotify = settings.music.spotify
    _check(
        "Spotify client id",
        bool(spotify.client_id),
        fix="register an app at developer.spotify.com/dashboard, then set [music.spotify] client_id",
    )
    auth = SpotifyAuth(spotify.client_id, spotify.redirect_port, TokenStore(spotify.token_path()))
    _check("signed in to Spotify (`pot music login`)", auth.logged_in)

    speaker = ManagedSpeaker(settings)
    if not speaker.enabled:
        console.print("  [dim]Pot's own speaker is off; music plays on your other devices[/dim]")
        return
    _check(
        f"{settings.music.speaker.binary} installed for Pot's speaker ({install_hint()})",
        speaker.binary_path() is not None,
    )


@music_app.command("login")
def music_login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening it, for a headless device"
    ),
) -> None:
    """Authorise Pot against your own Spotify app."""
    from pot.skills.music.service import get_service
    from pot.skills.music.spotify.auth import SpotifyAuthError

    runtime = _setup()
    provider = _spotify(runtime)
    if provider is None:
        raise typer.Exit(1)

    async def run() -> None:
        try:
            await provider.auth.login(open_browser=not no_browser)
        except SpotifyAuthError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[green]signed in[/green]")
        for speaker in await get_service().speakers():
            console.print(f"  {speaker.describe()}")
        await runtime.close()

    asyncio.run(run())


@music_app.command("logout")
def music_logout() -> None:
    """Forget the stored Spotify token."""
    runtime = _setup()
    provider = _spotify(runtime)
    if provider is None:
        raise typer.Exit(1)
    provider.auth.logout()
    console.print("signed out")


@music_app.command("devices")
def music_devices() -> None:
    """List the speakers music can play on."""
    from pot.skills.music.service import get_service

    runtime = _setup()

    async def run() -> None:
        service = get_service()
        speakers = await service.speakers()
        if not speakers:
            console.print("[yellow]no Spotify devices are awake right now[/yellow]")
        table = Table("speaker", "type", "volume", "notes")
        for speaker in speakers:
            notes = ", ".join(
                mark for mark, on in (("Pot", speaker.is_pot), ("active", speaker.is_active)) if on
            )
            volume = f"{speaker.volume_percent}%" if speaker.volume_percent is not None else "-"
            table.add_row(speaker.name, speaker.kind, volume, notes)
        console.print(table)
        await runtime.close()

    _run_music(runtime, run)


@music_app.command("play")
def music_play(
    query: str = typer.Argument("", help="What to play; empty resumes"),
    kind: str = typer.Option("track", "--kind", "-k", help="track, album, artist or playlist"),
    speaker: str = typer.Option("", "--speaker", "-s", help="Speaker name, empty for the usual one"),
) -> None:
    """Play something: `pot music play "brown eyed girl"`."""
    from pot.skills.music.service import get_service

    runtime = _setup()

    async def run() -> None:
        console.print(await get_service().play(query or None, kind, speaker))
        await runtime.close()

    _run_music(runtime, run)


@music_app.command("pause")
def music_pause() -> None:
    """Pause playback."""
    _simple_music("pause")


@music_app.command("resume")
def music_resume() -> None:
    """Resume playback."""
    _simple_music("resume")


@music_app.command("skip")
def music_skip(
    backwards: bool = typer.Option(False, "--back", "-b", help="Previous track instead of next"),
) -> None:
    """Skip to the next track, or back."""
    _simple_music("skip", backwards)


@music_app.command("volume")
def music_volume(percent: int = typer.Argument(..., min=0, max=100)) -> None:
    """Set the music volume."""
    _simple_music("set_volume", percent)


@music_app.command("status")
def music_status() -> None:
    """What's playing."""
    _simple_music("now_playing")


def _simple_music(method: str, *args: object) -> None:
    from pot.skills.music.service import get_service

    runtime = _setup()

    async def run() -> None:
        console.print(await getattr(get_service(), method)(*args))
        await runtime.close()

    _run_music(runtime, run)


def _run_music(runtime: Runtime, run) -> None:
    from pot.skills.music.provider import MusicError
    from pot.skills.music.spotify.auth import SpotifyAuthError

    try:
        asyncio.run(run())
    except (MusicError, SpotifyAuthError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _spotify(runtime: Runtime):
    """The Spotify provider, or None with an explanation printed."""
    from pot.skills.music.service import get_service
    from pot.skills.music.spotify.provider import SpotifyProvider

    provider = get_service().provider
    if not isinstance(provider, SpotifyProvider):
        console.print(f"[red]the music provider is {provider.name}, not spotify[/red]")
        return None
    if not runtime.settings.music.spotify.client_id:
        console.print(
            "[red]no Spotify client id configured.[/red]\n"
            "  1. Create an app at https://developer.spotify.com/dashboard\n"
            f"  2. Add the redirect URI {provider.auth.redirect_uri}\n"
            r"  3. Put the client id under \[music.spotify] in config/pot.toml"
        )
        return None
    return provider


def _priced_models(settings: Settings) -> list[str]:
    """Models this configuration will actually spend money on."""
    return [settings.voice.model, settings.research.model, settings.research.deep_model]


def _check(label: str, ok: bool, fix: str = "") -> None:
    """Report one requirement, and when it isn't met, what to do about it.

    A bare "missing" leaves the reader to go and find out where the thing goes, and doctor is
    exactly where someone lands when they're stuck.
    """
    mark = "[green]ok[/green]" if ok else "[red]missing[/red]"
    console.print(f"  {mark} {label}")
    if not ok and fix:
        console.print(f"        [dim]{fix}[/dim]")


if __name__ == "__main__":
    app()

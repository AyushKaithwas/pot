"""`pot` command line: talk to it, or drive the pieces directly while debugging."""

from __future__ import annotations

import asyncio
import json
import logging

import typer
from rich.console import Console
from rich.table import Table

from pot.config import DEVICES_FILE, Settings, load_settings
from pot.runtime import Runtime, set_runtime
from pot.skills.lights import discovery
from pot.skills.lights.provider import LightChange
from pot.skills.lights.service import get_service

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Pot, a voice assistant.")
lights_app = typer.Typer(no_args_is_help=True, help="Control and inspect the lights directly.")
app.add_typer(lights_app, name="lights")
console = Console()


def _setup(verbose: bool = False) -> Runtime:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    runtime = Runtime(load_settings())
    set_runtime(runtime)
    return runtime


@app.command()
def talk(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Start a voice conversation."""
    from pot.voice import run_voice

    settings = _setup(verbose).settings
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
    _check("OPENAI_API_KEY", bool(settings.openai_api_key))
    _check("Tuya cloud credentials", settings.has_tuya_cloud)
    unpriced = [model for model in _priced_models(settings) if model not in settings.costs.models]
    _check(
        "cost rates for every configured model" + (f" (missing: {', '.join(unpriced)})" if unpriced else ""),
        not unpriced,
    )

    try:
        from pot.audio.devices import describe

        console.print(f"  input:  {describe(None, kind='input')}")
        console.print(f"  output: {describe(None, kind='output')}")
    except Exception as exc:
        console.print(f"[red]audio devices unavailable:[/red] {exc}")

    devices = settings.lights.devices
    if not devices:
        console.print("[yellow]no lights configured in config/pot.toml[/yellow]")
        return
    for device in devices.values():
        _check(f"local key for {device.spoken_name}", bool(device.local_key))

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
    DEVICES_FILE.write_text(json.dumps(devices, indent=2) + "\n")
    console.print(f"wrote {len(devices)} device(s) to {DEVICES_FILE}")
    for device in devices:
        console.print(
            f"  {device.get('name')} · {device.get('id')} · key {'yes' if device.get('key') else 'no'}"
        )


def _priced_models(settings: Settings) -> list[str]:
    """Models this configuration will actually spend money on."""
    return [settings.voice.model, settings.research.model, settings.research.deep_model]


def _check(label: str, ok: bool) -> None:
    mark = "[green]ok[/green]" if ok else "[red]missing[/red]"
    console.print(f"  {mark} {label}")


if __name__ == "__main__":
    app()

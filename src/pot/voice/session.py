"""The voice loop: mic in, audio out, events on the terminal.

Three concurrent jobs while the session is open: pump mic chunks up, pump model audio down,
and watch the keyboard. The mic gate closes the moment Pot starts speaking so the laptop's
speakers can't talk into its own microphone, and reopens once playback has drained.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable

from agents.realtime import RealtimeRunner, RealtimeSession

from pot.audio.capture import Microphone, MicrophoneGate
from pot.audio.cues import Cues
from pot.audio.playback import Speaker
from pot.audio.ptt import keypresses, raw_terminal
from pot.config import Settings
from pot.console import console
from pot.runtime import Runtime, set_runtime
from pot.skills import load
from pot.voice.activity import ToolActivity
from pot.voice.agent import build_agent, build_run_config

log = logging.getLogger(__name__)

ANNOUNCE_PREAMBLE = "System notice, say this to the user now, briefly and in your own words:"


class Announcer:
    """Bridges the runtime's sync announce callback onto the session's async send_message."""

    def __init__(self, session: RealtimeSession) -> None:
        self._session = session
        self._pending: set[asyncio.Task] = set()

    def __call__(self, text: str) -> None:
        task = asyncio.create_task(self._session.send_message(f"{ANNOUNCE_PREAMBLE} {text}"))
        self._pending.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("announcement failed: %s", task.exception())


async def run_voice(settings: Settings) -> None:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; copy .env.example to .env and fill it in")

    runtime = Runtime(settings)
    set_runtime(runtime)
    skills = load(settings)
    agent = build_agent(settings, skills)
    runner = RealtimeRunner(starting_agent=agent, config=build_run_config(settings))

    always_on = settings.audio.mode == "always_on"
    gate = MicrophoneGate(open_at_start=always_on)
    microphone = Microphone(
        sample_rate=settings.audio.sample_rate,
        device_name=settings.audio.input_device,
        gate=gate,
    )
    speaker = Speaker(sample_rate=settings.audio.sample_rate, device_name=settings.audio.output_device)
    cues = Cues(
        speaker,
        sample_rate=settings.audio.sample_rate,
        volume=settings.audio.cue_volume,
        enabled=settings.audio.cues,
    )

    console.print(
        f"[bold]{settings.assistant.name}[/bold] ready. Skills: {', '.join(skill.name for skill in skills)}"
    )
    console.print(f"mic {microphone.description} · speaker {speaker.description}")
    if always_on:
        console.print("[dim]always-on mic · q = quit[/dim]")
    elif settings.audio.auto_reopen:
        console.print("[dim]space = start talking, then just keep talking · q = quit[/dim]")
    else:
        console.print("[dim]space = talk, once per turn · q = quit[/dim]")

    # The key lives in .env, which pydantic-settings reads without exporting to the environment,
    # so hand it to the model rather than letting it look itself up.
    session = await runner.run(model_config={"api_key": settings.openai_api_key})
    async with session, speaker, microphone:
        runtime.on_announce(Announcer(session))
        await _start_skill_services(runtime, skills)
        tasks = [
            asyncio.create_task(_pump_microphone(session, microphone), name="mic"),
            asyncio.create_task(_pump_events(session, speaker, gate, runtime, cues), name="events"),
        ]
        with raw_terminal() as interactive:
            if interactive:
                tasks.append(asyncio.create_task(_watch_keys(session, gate, cues), name="keys"))
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                task.result()
    runtime.on_announce(None)
    console.print(runtime.usage.summary())
    await runtime.close()


async def _start_skill_services(runtime: Runtime, skills: Iterable[object]) -> None:
    """Let skills spin up anything long-lived, e.g. Pot's own Spotify speaker."""
    for skill in skills:
        start = getattr(skill, "start", None)
        if start is None:
            continue
        try:
            await start(runtime)
        except Exception:
            log.exception("skill %s failed to start", getattr(skill, "name", skill))


async def _pump_microphone(session: RealtimeSession, microphone: Microphone) -> None:
    async for chunk in microphone.chunks():
        await session.send_audio(chunk)


async def _pump_events(
    session: RealtimeSession,
    speaker: Speaker,
    gate: MicrophoneGate,
    runtime: Runtime,
    cues: Cues,
) -> None:
    transcripts = TranscriptPrinter()
    activity = ToolActivity()
    ticker = asyncio.create_task(_tick_while_working(activity, cues), name="working-cue")
    audio = runtime.settings.audio
    hands_free = audio.mode == "always_on"
    reopen: asyncio.Task | None = None

    try:
        async for event in session:
            if event.type == "audio":
                if not hands_free:
                    # Pot is talking: stop listening so we don't hear ourselves.
                    gate.close()
                    if reopen is not None:
                        reopen.cancel()
                runtime.set_speaking(True)
                speaker.write(event.audio.data)
            elif event.type == "audio_end":
                reopen = asyncio.create_task(
                    _finish_speaking(
                        speaker,
                        gate,
                        runtime,
                        activity,
                        cues,
                        reopen_mic=not hands_free and audio.auto_reopen,
                    ),
                    name="finish-speaking",
                )
            elif event.type == "audio_interrupted":
                speaker.flush()
                runtime.set_speaking(False)
            elif event.type == "tool_start":
                activity.start(event.tool.name)
                cues.reset_working()
            elif event.type == "tool_end":
                activity.finish(event.tool.name)
            elif event.type == "history_added":
                transcripts.feed([event.item])
            elif event.type == "history_updated":
                transcripts.feed(event.history)
            elif event.type == "raw_model_event" and event.data.type == "usage":
                _record_usage(event.data, runtime)
            elif event.type == "error":
                # Nothing to read on a screenless device, so the buzz is the whole message.
                console.print(f"[red]error[/red] {event.error}")
                cues.error()
            elif event.type == "guardrail_tripped":
                console.print("[yellow]guardrail tripped[/yellow]")
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        activity.stop()


def _record_usage(event: object, runtime: Runtime) -> None:
    turn_cost = runtime.usage.record_realtime(runtime.settings.voice.model, event)
    if runtime.settings.costs.show_per_turn:
        console.print(f"[dim]${turn_cost:.4f} this turn · ${runtime.usage.cost():.4f} total[/dim]")


async def _tick_while_working(activity: ToolActivity, cues: Cues) -> None:
    """Sound a soft tick while a tool is still running, for anyone who can't see the spinner."""
    while True:
        await asyncio.sleep(0.5)
        if activity.busy:
            cues.working()


async def _finish_speaking(
    speaker: Speaker,
    gate: MicrophoneGate,
    runtime: Runtime,
    activity: ToolActivity | None = None,
    cues: Cues | None = None,
    *,
    reopen_mic: bool,
    settle_seconds: float = 0.2,
    timeout: float = 30.0,
) -> None:
    """Wait for Pot to actually stop talking, then reopen the mic and let listeners know.

    Waiting for playback to drain rather than for the last audio event matters: the model sends
    its audio faster than the speaker plays it, so acting on the event would let the mic hear the
    tail of Pot's own reply, and would unduck the music while Pot is still mid-sentence.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while speaker.is_playing and loop.time() < deadline:
        await asyncio.sleep(0.05)
    await asyncio.sleep(settle_seconds)
    runtime.set_speaking(False)
    if not reopen_mic:
        return
    gate.open()
    if cues is not None:
        cues.ready()
    # The spinner already says Pot is working and listening, so don't contradict it.
    if activity is None or not activity.busy:
        console.print("[green]listening[/green]")


async def _watch_keys(session: RealtimeSession, gate: MicrophoneGate, cues: Cues) -> None:
    async for key in keypresses():
        if key in {"q", "\x03", "\x04"}:
            return
        if key in {" ", "\r", "\n"}:
            if gate.toggle():
                cues.ready()
                console.print("[green]listening[/green]")
            else:
                cues.closed()
                console.print("[dim]mic off[/dim]")


class TranscriptPrinter:
    """Prints each side of the conversation once it's final.

    With audio output the transcript arrives through history updates rather than the initial
    add, and it streams in, so items are printed when they stop being in progress.
    """

    def __init__(self) -> None:
        self._printed: dict[str, str] = {}

    def feed(self, items: Iterable[object]) -> None:
        for item in items:
            self._print(item)

    def _print(self, item: object) -> None:
        role = getattr(item, "role", None)
        if role not in {"user", "assistant"} or getattr(item, "status", None) == "in_progress":
            return
        text = " ".join(
            part
            for content in getattr(item, "content", []) or []
            if (part := getattr(content, "transcript", None) or getattr(content, "text", None))
        ).strip()
        item_id = getattr(item, "item_id", "")
        if not text or self._printed.get(item_id) == text:
            return
        self._printed[item_id] = text
        label = "[cyan]you[/cyan]" if role == "user" else "[magenta]pot[/magenta]"
        console.print(f"{label} {text}")

"""The voice loop: mic in, audio out, events on the terminal.

Three concurrent jobs while the session is open: pump mic chunks up, pump model audio down,
and watch the keyboard. The mic gate closes the moment Pot starts speaking so the laptop's
speakers can't talk into its own microphone, and reopens once playback has drained.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections import deque
from collections.abc import Callable, Iterable

from agents.realtime import RealtimeRunner, RealtimeSession
from websockets.exceptions import ConnectionClosed

from pot.audio.capture import BLOCK_MILLISECONDS, MicBoost, Microphone, MicrophoneGate
from pot.audio.cues import Cues
from pot.audio.echo import EchoSuppressor
from pot.audio.playback import Speaker
from pot.audio.ptt import keypresses, raw_terminal
from pot.audio.ring import Ringer
from pot.audio.wake import WakeWord, WakeWordUnavailable
from pot.audio.wake import build as build_wake
from pot.config import AudioConfig, Settings
from pot.console import console
from pot.runtime import Runtime, set_runtime
from pot.skills import load
from pot.voice.activity import ToolActivity
from pot.voice.agent import build_agent, build_run_config
from pot.voice.conversation import ConversationWindow

log = logging.getLogger(__name__)

# Announcements are mostly things to say, but an alarm can carry something to do ("play Wavy when
# it goes off"), and that only works if the model is told it's allowed to act on one.
# Long enough not to hammer the API if it is refusing connections, short enough that an hourly
# reconnect is not a gap anyone notices. Grows toward the cap while connections keep dying young,
# which is what a dead network looks like.
RECONNECT_DELAY_SECONDS = 2.0
RECONNECT_DELAY_CAP_SECONDS = 30.0
HEALTHY_CONNECTION_SECONDS = 30.0

ANNOUNCE_PREAMBLE = (
    "System notice. If it asks you to do something, do it now with your tools. "
    "Then tell the user, briefly and in your own words:"
)


class Announcer:
    """Delivers announcements to the session, one at a time, when Pot isn't mid-reply.

    Sending while a response is active gets conversation_already_has_active_response back: the
    words land in the history but nothing speaks them until the user happens to talk again, which
    for "turn the lights off in ten minutes" means the lights don't change. So delivery waits for
    quiet, and a send that fails outright puts its text back for the next connection.
    """

    def __init__(
        self,
        session: RealtimeSession,
        window: ConversationWindow | None = None,
        requeue: Callable[[str], None] | None = None,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._window = window
        self._requeue = requeue
        self._busy = busy or (lambda: False)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        # The item the worker has dequeued but not yet delivered. Held here rather than in a
        # local so that close() can hand it on: cancellation mid-wait would otherwise eat it.
        self._carrying: str | None = None
        self._worker = asyncio.create_task(self._deliver(), name="announcer")

    def __call__(self, text: str) -> None:
        self._queue.put_nowait(text)

    async def _deliver(self) -> None:
        while True:
            self._carrying = await self._queue.get()
            text = self._carrying
            while self._busy():
                await asyncio.sleep(0.25)
            if self._window is not None:
                # Pot started this exchange, so it listens for the answer afterwards. Without
                # this the mic stays shut and the only way back in is the wake word, which is
                # exactly what an announcement like "now playing" has just made hard to hear.
                self._window.start()
            try:
                await self._session.send_message(f"{ANNOUNCE_PREAMBLE} {text}")
            except Exception as exc:
                log.warning("announcement failed: %s", exc)
                if self._requeue is not None:
                    self._requeue(text)
                self._carrying = None
                continue
            self._carrying = None
            if not self._queue.empty():
                # Give the reply time to register as busy, or the next item races it and gets
                # refused. Only worth waiting for when something is actually behind it.
                for _ in range(20):
                    if self._busy():
                        break
                    await asyncio.sleep(0.1)

    def close(self) -> None:
        """Stop delivering, putting anything unsent back for whoever takes over."""
        self._worker.cancel()
        if self._carrying is not None and self._requeue is not None:
            self._requeue(self._carrying)
            self._carrying = None
        while not self._queue.empty():
            text = self._queue.get_nowait()
            if self._requeue is not None:
                self._requeue(text)


async def run_voice(settings: Settings) -> None:
    if not settings.has_model_access:
        raise RuntimeError(
            "no model access: put OPENAI_API_KEY in ~/.config/pot/env, or set [service] base_url "
            "and POT_SERVICE_TOKEN to use a hosted one"
        )
    _check_input_is_reachable(settings.audio)

    runtime = Runtime(settings)
    set_runtime(runtime)
    skills = load(settings)
    agent = build_agent(settings, skills)
    runner = RealtimeRunner(starting_agent=agent, config=build_run_config(settings))

    audio_config = settings.audio
    gate = MicrophoneGate(open_at_start=audio_config.mic_open_at_start)
    microphone = Microphone(
        sample_rate=audio_config.sample_rate,
        device_name=audio_config.input_device,
        gate=gate,
    )
    boost = MicBoost(cap=audio_config.mic_boost)
    speaker = Speaker(
        sample_rate=audio_config.sample_rate,
        device_name=audio_config.output_device,
        prebuffer_ms=audio_config.playback_buffer_ms,
    )
    cues = Cues(
        speaker,
        sample_rate=audio_config.sample_rate,
        volume=audio_config.cue_volume,
        enabled=audio_config.cues,
        mute=gate.mute_for,
    )
    wake = _build_wake_word(settings)
    window = ConversationWindow(
        audio_config.wake.conversation_idle_seconds, audio_config.wake.question_idle_seconds
    )
    # The detector has to keep listening while Pot talks, or you could never interrupt it, so it
    # gets a copy of the mic with Pot's own voice subtracted out.
    echo = EchoSuppressor(audio_config.sample_rate, enabled=audio_config.echo_suppression)
    # Alarms need to make noise without the model being involved, so the ringer goes on the
    # runtime for the time skill to pick up when it starts.
    runtime.ringer = Ringer(speaker, sample_rate=audio_config.sample_rate)
    runtime.speaker = speaker
    runtime.on_ring(_listen_while_ringing(gate, runtime, wake, window))

    console.print(
        f"[bold]{settings.assistant.name}[/bold] ready. Skills: {', '.join(skill.name for skill in skills)}"
    )
    console.print(f"mic {microphone.description} · speaker {speaker.description}")
    console.print(f"[dim]{_how_to_talk(audio_config, wake, interactive=sys.stdin.isatty())}[/dim]")

    # Anything announced while no connection is live waits here for the next one, instead of
    # being a warning in a log nobody is reading. Bounded: if Pot has been offline long enough to
    # accumulate more, the oldest announcements are stale anyway.
    backlog: deque[str] = deque(maxlen=8)
    runtime.on_announce(backlog.append)

    async with speaker, microphone:
        await _start_skill_services(runtime, skills)
        with raw_terminal() as interactive:
            # The API ends a session after an hour, and a box in the corner has to outlive that,
            # so the connection is the only thing that restarts. Devices, skills and pending
            # alarms all live outside this loop and carry across.
            delay = RECONNECT_DELAY_SECONDS
            while True:
                started = asyncio.get_running_loop().time()
                try:
                    quitting = await _one_connection(
                        runner,
                        settings,
                        speaker,
                        microphone,
                        gate,
                        wake,
                        runtime,
                        cues,
                        window,
                        echo,
                        interactive,
                        backlog,
                        boost,
                    )
                except ConnectionClosed as exc:
                    log.info("connection closed: %s", exc)
                    quitting = False
                except (OSError, TimeoutError) as exc:
                    # Wi-Fi dropping or DNS not answering is routine for an appliance; a bad API
                    # key is not, and that arrives as a rejected handshake, which still crashes.
                    log.warning("connection failed: %s", exc)
                    quitting = False
                if quitting:
                    break
                # Back to buffering until the next connection is live.
                runtime.on_announce(backlog.append)
                _reset_between_connections(gate, runtime, window, echo)
                lasted = asyncio.get_running_loop().time() - started
                delay = _reconnect_delay(delay, lasted)
                console.print(f"[dim]reconnecting in {delay:g}s[/dim]")
                await asyncio.sleep(delay)
    runtime.on_announce(None)
    if speaker.underruns:
        console.print(
            f"[yellow]{speaker.underruns} playback underrun(s)[/yellow] — audio arrived later "
            "than it was needed, heard as a click mid-word. Check the network and CPU load."
        )
    console.print(runtime.usage.summary())
    await runtime.close()


async def _one_connection(
    runner: RealtimeRunner,
    settings: Settings,
    speaker: Speaker,
    microphone: Microphone,
    gate: MicrophoneGate,
    wake: WakeWord | None,
    runtime: Runtime,
    cues: Cues,
    window: ConversationWindow,
    echo: EchoSuppressor,
    interactive: bool,
    backlog: deque[str],
    boost: MicBoost,
) -> bool:
    """Hold one model connection open. True if the user asked to quit, False if it just ended."""
    session = await runner.run(model_config=settings.realtime_config())
    async with session:
        activity = ToolActivity()
        announcer = Announcer(
            session,
            window,
            requeue=backlog.append,
            # Speaking covers a reply in flight; a running tool means one is about to be.
            busy=lambda: runtime.speaking or activity.busy,
        )
        runtime.on_announce(announcer)
        if backlog:
            # Whatever fired while there was no connection: an alarm during the reconnect gap, or
            # one that was overdue at startup. One message rather than several.
            held = "; ".join(backlog)
            backlog.clear()
            announcer(held)
        tasks = [
            asyncio.create_task(
                _pump_microphone(
                    session, microphone, gate, wake, runtime, cues, window, echo, speaker, boost
                ),
                name="mic",
            ),
            asyncio.create_task(
                _pump_events(session, speaker, gate, runtime, cues, wake, window, activity),
                name="events",
            ),
        ]
        if interactive:
            tasks.append(asyncio.create_task(_watch_keys(session, gate, cues, runtime), name="keys"))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        quitting = any(task.get_name() == "keys" for task in done)
        announcer.close()
        for task in done:
            # A closed connection is how a session ends, not a failure to report.
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                task.result()
    return quitting


def _reconnect_delay(current: float, connection_lasted: float) -> float:
    """The next pause: patient while the network is down, prompt once it is back.

    An hourly expiry reconnects in seconds. A connection that keeps dying at birth backs off
    toward the cap, because retrying a dead network every two seconds helps nobody.
    """
    if connection_lasted >= HEALTHY_CONNECTION_SECONDS:
        return RECONNECT_DELAY_SECONDS
    return min(RECONNECT_DELAY_CAP_SECONDS, current * 2)


def _reset_between_connections(
    gate: MicrophoneGate, runtime: Runtime, window: ConversationWindow, echo: EchoSuppressor
) -> None:
    """Start the next connection waiting, not mid-conversation.

    Carrying an open gate across would stream the room into a session that has no idea a
    conversation was in progress, and the suppressor's reference belongs to audio already played.
    """
    gate.close()
    runtime.set_listening(False)
    window.stop()
    echo.reset()


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


def _check_input_is_reachable(audio: AudioConfig) -> None:
    """Refuse to start mute.

    Under systemd there's no terminal, so push-to-talk would wait forever for a keypress that
    can't arrive. Failing with the fix in the journal beats a service that looks healthy and
    never answers.
    """
    if audio.mode != "push_to_talk" or sys.stdin.isatty():
        return
    raise RuntimeError(
        'push_to_talk needs a terminal, and there isn\'t one here. Set mode = "wake_word" '
        '(or "always_on") under [audio], or run with --hands-free.'
    )


def _build_wake_word(settings: Settings) -> WakeWord | None:
    if not settings.audio.uses_wake_word:
        return None
    try:
        return build_wake(settings.audio)
    except WakeWordUnavailable as exc:
        # Falling back silently would leave a screenless device deaf with no explanation.
        raise RuntimeError(f"wake word mode needs a working wake word: {exc}") from exc


def _how_to_talk(audio: AudioConfig, wake: WakeWord | None, *, interactive: bool = True) -> str:
    """What to do, given where this is running.

    Keys are only read when there's a terminal to read them from. Offering "q = quit" over
    `ssh host \'pot talk\'`, or under systemd, promises a key nothing is listening for, and the
    only way out then looks like there isn\'t one.
    """
    quit_hint = " · q = quit" if interactive else " · ctrl-c to stop"
    if wake is not None:
        return f'say "{wake.spoken}" to start{quit_hint}'
    if audio.mode == "always_on":
        return f"always-on mic{quit_hint}"
    if audio.auto_reopen:
        return f"space = start talking, then just keep talking{quit_hint}"
    return f"space = talk, once per turn{quit_hint}"


async def _pump_microphone(
    session: RealtimeSession,
    microphone: Microphone,
    gate: MicrophoneGate,
    wake: WakeWord | None,
    runtime: Runtime,
    cues: Cues,
    window: ConversationWindow | None = None,
    echo: EchoSuppressor | None = None,
    speaker: Speaker | None = None,
    boost: MicBoost | None = None,
) -> None:
    """Route every chunk: always to the wake word, to OpenAI only once it has fired.

    This is the whole cost story. While the gate is shut nothing leaves the machine, so a room
    with nobody in it bills nothing.

    The pre-roll matters more than it looks. Detection only completes a few hundred milliseconds
    after the wake word ends, and by then you're already saying what you want, so sending audio
    from the moment of detection loses the first syllables of the request.
    """
    # Always at least one chunk deep: the chunk that triggered the wake word is part of the
    # request too, so dropping it would clip the very first word.
    recent: deque[bytes] | None = (
        deque(maxlen=_preroll_chunks(runtime.settings.audio)) if wake is not None else None
    )
    async for chunk in microphone.chunks():
        if echo is not None and speaker is not None:
            # What actually left the speaker since the last chunk, which is what the mic is
            # hearing now. Pulled rather than pushed, because writes run ahead of playback.
            echo.playback(speaker.drain_reference())
        if gate.is_open:
            if not gate.is_muted:
                await session.send_audio(boost.apply(chunk) if boost else chunk)
            continue
        if recent is None:
            continue
        recent.append(chunk)
        # Only the detector gets the echo-suppressed copy: it has artefacts, and what reaches the
        # model is gated while Pot speaks anyway, so it never needs them.
        heard = echo.process(chunk) if echo is not None else chunk
        if wake is not None and heard and wake.feed(heard):
            _start_listening(gate, runtime, cues, wake)
            if window is not None:
                window.start()
            for buffered in recent:
                await session.send_audio(boost.apply(buffered) if boost else buffered)
            recent.clear()


def _preroll_chunks(audio: AudioConfig) -> int:
    """How many mic chunks of history to replay when the wake word fires."""
    return max(1, round(audio.wake.preroll_seconds * 1000 / BLOCK_MILLISECONDS))


def _start_listening(gate: MicrophoneGate, runtime: Runtime, cues: Cues, wake: WakeWord | None) -> None:
    if wake is not None:
        wake.reset()
    gate.open()
    runtime.set_listening(True)
    cues.ready()
    console.print("[green]listening[/green]")


def _stop_listening(gate: MicrophoneGate, runtime: Runtime, cues: Cues, reason: str) -> None:
    gate.close()
    runtime.set_listening(False)
    cues.closed()
    console.print(f"[dim]{reason}[/dim]")


async def _pump_events(
    session: RealtimeSession,
    speaker: Speaker,
    gate: MicrophoneGate,
    runtime: Runtime,
    cues: Cues,
    wake: WakeWord | None = None,
    window: ConversationWindow | None = None,
    activity: ToolActivity | None = None,
) -> None:
    transcripts = TranscriptPrinter()
    activity = activity if activity is not None else ToolActivity()
    ticker = asyncio.create_task(_tick_while_working(activity, cues), name="working-cue")
    watcher = (
        asyncio.create_task(_close_when_idle(gate, runtime, cues, wake, window), name="idle")
        if window is not None and wake is not None
        else None
    )
    audio = runtime.settings.audio
    hands_free = audio.mode == "always_on"
    reopen: asyncio.Task | None = None

    try:
        async for event in session:
            if window is not None:
                window.touch()
            if event.type == "audio":
                if not hands_free:
                    # Pot is talking: stop listening so we don't hear ourselves.
                    gate.close()
                    if reopen is not None:
                        reopen.cancel()
                runtime.set_speaking(True)
                speaker.write(event.audio.data)
            elif event.type == "audio_end":
                speaker.finish()
                # A skill can ask for the exchange to end, e.g. once music is playing. Applied here
                # rather than when it was asked for, so Pot still finishes the sentence it's on.
                if runtime.take_conversation_end() and window is not None:
                    window.stop()
                    ended_by_skill = wake is not None
                else:
                    ended_by_skill = False
                reopen = asyncio.create_task(
                    _finish_speaking(
                        speaker,
                        gate,
                        runtime,
                        activity,
                        cues,
                        reopen_mic=_should_reopen(audio, wake, window),
                        closed_reason=f'say "{wake.spoken}" again' if ended_by_skill and wake else "",
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
                _hold_for_question(transcripts, window)
            elif event.type == "history_updated":
                transcripts.feed(event.history)
                _hold_for_question(transcripts, window)
            elif event.type == "raw_model_event" and event.data.type == "usage":
                _record_usage(event.data, runtime)
            elif (
                event.type == "raw_model_event"
                and event.data.type == "input_audio_timeout_triggered"
                and wake is not None
            ):
                _stop_listening(gate, runtime, cues, f'back to "{wake.spoken}"')
            elif event.type == "error":
                if _is_expected(event.error):
                    log.debug("expected: %s", event.error)
                    continue
                # Nothing to read on a screenless device, so the buzz is the whole message.
                console.print(f"[red]error[/red] {event.error}")
                cues.error()
            elif event.type == "guardrail_tripped":
                console.print("[yellow]guardrail tripped[/yellow]")
    finally:
        # reopen included: left running, it would fire after the reconnect reset and open the mic
        # into a fresh session that is supposed to be waiting for the wake word.
        for task in (ticker, watcher, reopen):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (ticker, watcher, reopen) if t is not None), return_exceptions=True)
        activity.stop()


# Neither of these is a fault. Talking over a long tool call is a feature, and the API declining a
# second reply is how it says so; an hour-long session ending is the documented limit, and Pot
# reconnects. On a device whose only error channel is a buzz, reporting either would cry wolf.
EXPECTED_ERRORS = ("conversation_already_has_active_response", "session_expired")


def _is_expected(error: object) -> bool:
    return any(code in str(error) for code in EXPECTED_ERRORS)


def _hold_for_question(transcripts: TranscriptPrinter, window: ConversationWindow | None) -> None:
    """When Pot's reply ends in a question, keep listening long enough to be answered.

    Everything else gets the short window: a confirmation rarely has a follow-up, and Alexa ships
    the same default. The check is on the finalized transcript, which is the only place Pot's own
    words are visible to us.
    """
    if window is None:
        return
    if transcripts.last_assistant_text.rstrip().endswith(("?", "？")):
        window.await_answer()


def _record_usage(event: object, runtime: Runtime) -> None:
    turn_cost = runtime.usage.record_realtime(runtime.settings.voice.model, event)
    if runtime.settings.costs.show_per_turn:
        console.print(f"[dim]${turn_cost:.4f} this turn · ${runtime.usage.cost():.4f} total[/dim]")


async def _close_when_idle(
    gate: MicrophoneGate,
    runtime: Runtime,
    cues: Cues,
    wake: WakeWord | None,
    window: ConversationWindow,
) -> None:
    """Stop streaming once the exchange has gone quiet, and wait for the wake word again."""
    while True:
        await asyncio.sleep(0.5)
        if runtime.alarm_ringing:
            # A ringing alarm is the one time silence doesn't mean the exchange is over: the mic
            # has to stay open, or "stop" has nowhere to land and the noise can't be called off.
            window.touch()
            continue
        if not window.expired():
            continue
        window.stop()
        spoken = wake.spoken if wake is not None else "the wake word"
        _stop_listening(gate, runtime, cues, f'quiet · say "{spoken}" again')


async def _tick_while_working(activity: ToolActivity, cues: Cues) -> None:
    """Sound a soft tick while a tool is still running, for anyone who can't see the spinner."""
    while True:
        await asyncio.sleep(0.5)
        if activity.busy:
            cues.working()


def _listen_while_ringing(
    gate: MicrophoneGate,
    runtime: Runtime,
    wake: WakeWord | None,
    window: ConversationWindow,
) -> Callable[[bool], None]:
    """Open the mic for as long as an alarm is ringing.

    Otherwise the one moment you most need to be heard is the one moment Pot isn't listening: on a
    device with no screen and no buttons, an alarm you can't say "stop" to has to be waited out.
    """

    def ringing_changed(ringing: bool) -> None:
        if not ringing:
            return
        if wake is not None:
            wake.reset()
        window.start()
        gate.open()
        runtime.set_listening(True)
        console.print("[green]listening[/green] · say stop or snooze")

    return ringing_changed


def _should_reopen(audio: AudioConfig, wake: WakeWord | None, window: ConversationWindow | None) -> bool:
    """Whether the mic should come back on now that Pot has finished a reply.

    The gate is shut while Pot talks so it doesn't hear itself, which means every reply ends with
    the mic closed. With a wake word it has to reopen for as long as the conversation is still
    running, or a follow-up would need the word again, which is the opposite of what the window
    is for. Hands-free never closes the gate in the first place, so there is nothing to reopen.
    """
    if audio.mode == "always_on" or not audio.auto_reopen:
        return False
    if wake is None:
        return True
    return window is not None and window.open


async def _finish_speaking(
    speaker: Speaker,
    gate: MicrophoneGate,
    runtime: Runtime,
    activity: ToolActivity | None = None,
    cues: Cues | None = None,
    *,
    reopen_mic: bool,
    closed_reason: str = "",
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
        if closed_reason and cues is not None:
            # Say so: on a screenless device, going quiet without a sound is indistinguishable
            # from having broken.
            _stop_listening(gate, runtime, cues, closed_reason)
        return
    gate.open()
    if cues is not None:
        cues.ready()
    # The spinner already says Pot is working and listening, so don't contradict it.
    if activity is None or not activity.busy:
        console.print("[green]listening[/green]")


async def _watch_keys(session: RealtimeSession, gate: MicrophoneGate, cues: Cues, runtime: Runtime) -> None:
    async for key in keypresses():
        if key in {"q", "\x03", "\x04"}:
            return
        if key in {" ", "\r", "\n"}:
            if gate.toggle():
                # Published, not just toggled: music ducks on the listening signal, and a
                # push-to-talk mic that doesn't announce itself leaves the song talking over you.
                runtime.set_listening(True)
                cues.ready()
                console.print("[green]listening[/green]")
            else:
                runtime.set_listening(False)
                cues.closed()
                console.print("[dim]mic off[/dim]")


class TranscriptPrinter:
    """Prints each side of the conversation once it's final.

    With audio output the transcript arrives through history updates rather than the initial
    add, and it streams in, so items are printed when they stop being in progress.
    """

    def __init__(self) -> None:
        self._printed: dict[str, str] = {}
        # Pot's most recent finalized words, for whoever needs to know how the reply ended.
        self.last_assistant_text = ""

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
        if role == "assistant" and text:
            self.last_assistant_text = text
        item_id = getattr(item, "item_id", "")
        if not text or self._printed.get(item_id) == text:
            return
        self._printed[item_id] = text
        label = "[cyan]you[/cyan]" if role == "user" else "[magenta]pot[/magenta]"
        console.print(f"{label} {text}")

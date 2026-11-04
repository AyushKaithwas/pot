# Pot

[![tests](https://github.com/AyushKaithwas/pot/actions/workflows/tests.yml/badge.svg)](https://github.com/AyushKaithwas/pot/actions/workflows/tests.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A voice assistant that takes a whole sentence at a time. Speech-to-speech on OpenAI's realtime
models, so "turn the tube light on at 30% warm, kill it in ten minutes, and tell me if it'll
rain tomorrow" is one turn, not four commands.

Runs on macOS for development and on a Raspberry Pi as a standalone device: local wake word, no
screen, no keyboard, starts at boot. Audio only reaches OpenAI after the wake word fires, so an
idle room costs nothing.

MIT licensed. Skills are one file each, and the ones that exist (lights, music, alarms, research)
are as much examples as features.

## What one sentence turns into

> "Turn the tube light on at 30% warm, kill it in ten minutes, and tell me if it'll rain
> tomorrow."

The realtime model hears that once and answers it with three tool calls in the same turn:

| tool | what happens |
|---|---|
| `set_lights` | switched on over the LAN, 30% brightness, warm white |
| `set_lights` with `after_seconds=600` | parked as a silent timer in the alarm store, so it survives a restart and cancels by voice |
| `research` | a web-search model looks it up; Pot keeps listening while it does |

Some other things it takes in one go:

```
"wake me at 6:30 every weekday"                 rings, gets louder, ducks the music
"play moondance, the album, on the kitchen speaker"
"save this and add it to my running playlist"   two music tools, one turn
"what's the time, and dim everything to 10%"
"that's all"                                    closes the mic now instead of waiting out the quiet
```

## How it fits together

```
 mic ─► wake word ─► mic gate ─► realtime session ◄──► skills, as tool calls
        on device    opens when   (OpenAI, speech       lights    Tuya on the LAN, cloud fallback
                     it fires      in and out)          music     Spotify Web API, librespot
                                       │                time      timers and alarms, kept on disk
                                       │ Pot's voice    research  web search on a text model
                                       ▼                basics    clock, "that's all"
 speaker ◄────────────────────────── mixer ◄── earcons, alarm ring, librespot music
                                       │
                                       └──► echo reference, subtracted from what the wake word hears
```

Each skill is a module under `src/pot/skills/` that exports a `SKILL`; the registry finds it, puts
its tools in the session and appends its prompt to the persona. The connection is the only thing
that restarts: the API ends sessions after an hour, and Pot reconnects in a couple of seconds with
devices, skills and pending alarms intact.

## Contents

- [Quickstart](#quickstart)
- [Controlling the lights](#controlling-the-lights)
- [Music](#music)
- [Talking to it](#talking-to-it)
- [On a Raspberry Pi](#on-a-raspberry-pi)
- [Wake word](#wake-word)
- [Timers and alarms](#timers-and-alarms)
- [Echo: the mic hearing the speaker](#echo-the-mic-hearing-the-speaker)
- [As an audio-only device](#as-an-audio-only-device)
- [What it costs](#what-it-costs)
- [Adding a skill](#adding-a-skill)
- [Layout](#layout) · [Development](#development) · [Not built yet](#not-built-yet)

## Quickstart

You need an OpenAI API key, Python 3.11+ and PortAudio. Lights, music and the wake word are all
optional and set up separately below.

```bash
git clone https://github.com/AyushKaithwas/pot && cd pot
brew install uv portaudio          # macOS; on Debian or a Pi: sudo apt install libportaudio2
uv sync
uv run pot config init             # writes ~/.config/pot/pot.toml
echo 'OPENAI_API_KEY=sk-...' >> ~/.config/pot/env
uv run pot doctor
uv run pot talk                    # press space, then talk
```

Secrets go in `~/.config/pot/env` (or a `.env` beside the source while developing; see
[.env.example](.env.example)). `doctor` reports what's missing: keys, audio devices, the wake
word model, and whether each configured light is answering on the network.

### Where things live

Nothing you configure lives in the repo, so an update never overwrites it and the repo ships
nobody's house.

| | |
|---|---|
| `~/.config/pot/pot.toml` | your settings, merged over the shipped defaults |
| `~/.config/pot/env` | secrets |
| `~/.local/state/pot/` | device keys, Spotify token, alarms, wake word cache |
| `pot config defaults` | prints the shipped defaults, the reference for every option |

`pot config path` prints all of it, `pot config show` prints the merged result.

## Controlling the lights

The lights are Tuya devices (Wipro is a Tuya white-label), so they speak a documented LAN
protocol. Pot talks to them directly over Wi-Fi, with the Tuya cloud API as fallback. Local
control needs each device's local key, and the only way to get that is a Tuya cloud project.

One-time, on your side:

1. Install **Smart Life** (Tuya's own app) and make sure the light is paired to it. If the light
   is currently in the Wipro app, remove it there first, then factory-reset it into pairing mode
   (power off/on until it blinks fast) and add it in Smart Life. A Tuya device belongs to one
   account at a time.
2. Create a free developer account at [iot.tuya.com](https://iot.tuya.com) → **Cloud** →
   **Create Cloud Project**, data centre matching your region (India for a Wipro light).
   Enable *IoT Core*, *Authorization Token Management* and *Smart Home Basic Service*.
3. In the project, **Devices** → **Link Tuya App Account** → **Add App Account**, and scan the
   QR code with Smart Life (Me → scan icon, top right). Your light should now appear in the
   project's device list.
4. Put the project's Access ID and Access Secret in `~/.config/pot/env` as `TUYA_API_KEY` and
   `TUYA_API_SECRET`, with `TUYA_API_REGION` (`in`, `eu`, `us`, `cn`).

Then:

```bash
uv run pot lights keys     # pulls local keys into ~/.local/state/pot/devices.json
uv run pot lights scan     # who's on the LAN, and at which protocol version
uv run pot lights list     # configured lights and their current state
uv run pot lights set beta1 --on -b 30 -t 20
uv run pot lights set beta1 --color ff8800
uv run pot lights dps beta1   # raw data points, when a light behaves oddly
```

Declare lights in `~/.config/pot/pot.toml` by **gwId**, not IP, because DHCP moves them; Pot finds
the current address from the device's own discovery broadcast at startup.

Two gotchas that cost time on the first device, in case a future one behaves oddly:

- **Work modes.** An RGB light in colour mode parks its white brightness data point, so setting
  brightness or temperature does nothing visible until the light is switched back to white mode.
  `tuya_local` handles that; `pot lights dps` shows the current mode as dp 21.
- **Stale reads.** tinytuya's persistent connection caches the last status, and it doesn't always
  reflect a mode change, so every change re-queries the device before reporting back.

## Music

Pot copies how an Echo does this. An Echo *is* a Spotify Connect speaker: it runs a Spotify
client, audio streams to it from Spotify's servers, and the voice assistant only sends control
commands. So Pot has two halves too: it always controls playback through the Spotify Web API, and
it can optionally *be* a speaker by running librespot.

Spotify **Premium** is required for either half.

### Setup, roughly five minutes

Everyone runs their own Spotify app: development-mode apps are capped at five authenticated
users, and extended quota is reserved for businesses with 250k+ monthly actives, so there's no
shared "sign in to Pot" to offer.

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → Create app.
2. Redirect URI: `http://127.0.0.1:8765/callback`. Tick the Web API.
3. Copy the Client ID into `client_id` under `[music.spotify]` in `~/.config/pot/pot.toml`.
   There's no client secret to copy: Pot uses PKCE.
4. `uv run pot music login`, approve in the browser.

```bash
uv run pot music devices              # what's available to play on
uv run pot music play "brown eyed girl"
uv run pot music play "moondance" --kind album
uv run pot music pause / resume / skip / volume 40 / status
```

### Pot as a speaker

Without this, music plays on whatever Spotify device is awake: your phone, your laptop, an Echo.
That's the whole story on a Mac, where the desktop app is already a Connect device.

On a Pi there's nothing else to play through, so Pot runs its own client:

```bash
sudo apt install librespot     # brew install librespot on macOS
```

Then set `enabled = true` under `[music.speaker]`. Pot starts librespot with the session, restarts
it if it dies, and prefers it over other devices. "Pot" appears in your Spotify app's device list
like any other speaker. Its audio comes back through a pipe and plays through Pot's own mixer
rather than going to the sound card directly, which is the same on a Mac and a Pi, so there's no
audio backend to pick.

librespot is an unofficial Spotify client. Fine for your own home, not something to ship in a
product, which is why it's optional and off by default.

### Ducking

Music and Pot share a speaker, so the music drops to `duck_to_percent` while Pot talks and goes
back to exactly where it was afterwards. It waits for playback to actually drain rather than for
the last audio packet, or it would come back up mid-sentence. Setting the volume by hand while
Pot is talking wins: your choice isn't undone.

## Talking to it

```bash
uv run pot talk
```

Pot speaks at 1.15x and answers in a sentence or less by default; `speed` under `[voice]` and the
persona in [src/pot/voice/persona.py](src/pot/voice/persona.py) are the two dials.

Press space once and then just talk. The mic shuts while Pot is answering, so the speakers can't
talk into the microphone, and reopens by itself once playback has drained. Space toggles it off
again mid-conversation, `q` quits.

A tool that takes a while (a web search runs about ten seconds) doesn't block Pot from talking,
so it will answer, hand back the mic, and keep working. A spinner stays up for as long as anything
is in flight, saying what it's doing and that you can still interrupt, and each tool reports how
long it took when it lands.

Under `[audio]`: `auto_reopen = false` goes back to one keypress per turn (use it if the room echoes
enough that Pot interrupts itself), `mode = "always_on"` is hands-free with the mic permanently
open, and `mode = "wake_word"` is what a real device wants.

## On a Raspberry Pi

A Pi with a USB mic and a powered speaker is the target: no screen, no keyboard, starts on its own.

```bash
sudo apt update && sudo apt install -y libportaudio2 git
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/AyushKaithwas/pot ~/pot && cd ~/pot
uv venv --python 3.12          # not 3.13: onnxruntime's aarch64 wheels lag
uv sync --extra wakeword
```

Python 3.12 is deliberate, and `--extra wakeword` pulls onnxruntime rather than the deprecated
tflite-runtime openWakeWord asks for by default (that package stops at 3.11 and is patchy on ARM;
a dependency override in `pyproject.toml` drops it). Everything else resolves to aarch64 wheels,
so nothing compiles.

Then check the hardware before blaming the software:

```bash
uv run pot audio devices        # names to put in input_device / output_device
uv run pot audio test           # record 3s, play it back, report the level
uv run pot audio wake           # say the wake word, watch the scores
```

Set `mode = "wake_word"` under `[audio]` in your config, then have it start at boot:

```bash
uv run pot service install      # writes a systemd *user* unit, no root
systemctl --user enable --now pot
sudo loginctl enable-linger $USER   # so it runs with nobody logged in
```

`pot service logs -f` follows the journal, which on a headless box is the only way to watch it
work. Push-to-talk refuses to start without a terminal rather than coming up silently.

## Wake word

```toml
[audio]
mode = "wake_word"

[audio.wake]
models = ["hey_mycroft"]   # any of them firing wakes Pot
threshold = 0.5
preroll_seconds = 0.2
conversation_idle_seconds = 5   # quiet this long and it waits for the word again
question_idle_seconds = 15      # used instead when Pot's reply ended in a question
```

Detection runs entirely on the machine. The stock models are fetched once (~1 MB each) and after
that nothing is uploaded, which is the same promise as the wake gate itself: nothing leaves the
room until you speak to it.

**Pick the model carefully; the stock ones are not equally good.** Measured across three voices at
three volumes:

| model | fired | median score |
|---|---|---|
| hey_mycroft | 9/9 | 1.000 |
| alexa | 9/9 | 0.931 |
| hey_jarvis | 6/9 | 0.757 |

`hey_jarvis` scored 0.14 on one voice, which in use feels exactly like being ignored. `alexa` is
reliable but will also wake a real Echo in the room. Listing two models costs about 1 ms per 80 ms
of audio each, so it's a cheap way to be heard more often.

Real detections score around 1.000 and 40 seconds of a quiet room peaked at 0.000, so the default
threshold has plenty of margin; there's little reason to lower it.

**`pot audio benchmark` measures the stock models against your own voice** and names the best one,
or tells you plainly that none of them hear you well. If that's the answer, train a model for the
phrase you want: see [docs/custom-wake-word.md](docs/custom-wake-word.md). A path ending `.onnx`
goes straight into `models`.

A commercial engine (Porcupine) was tried here and removed. It detects better, but its library
opens sockets, carries a vendor hostname and reads the machine's platform UUID, and its free tier
is non-commercial. No audio leaves the device, but handing a vendor a device fingerprint to listen
for its own name is the wrong trade for this project. The engine seam in `src/pot/audio/wake/`
remains, so another engine is one class implementing `scores`.

The microphone runs constantly but **audio only reaches OpenAI after the word fires**, so an empty
room costs nothing. After that the mic stays open for follow-ups until the conversation goes quiet
for `conversation_idle_seconds`, then it closes and waits for the word again. Five seconds is short
on purpose: a chained request fits in one sentence, so most exchanges are over when the reply is,
and an open mic streams the room to a paid API. If Pot's reply ends in a question it waits
`question_idle_seconds` instead, and "that's all" closes it straight away.

Two details that took measuring:

- **Pre-roll.** Pot replays a little audio from before detection so the start of the request
  isn't clipped. Only a little: detection completes as the word ends, so the pre-roll just covers
  the detector being a fraction late. Much more and it replays the wake word itself, which is then
  transcribed and answered as a turn of its own ("Mycroft." -> "Your MacBook Pro is available").
- **The conversation timeout is local.** The Realtime API appears to offer
  `turn_detection.idle_timeout_ms`, and the SDK's types advertise it, but the server rejects it as
  an unknown parameter, which silently discards the whole session config and leaves the model with
  no tools at all. It's a timer here instead.

A custom "hey pot" needs a training run rather than a config change, so a stock trigger comes
first. Tune `threshold` in your actual room with `pot audio wake`; lower catches more and misfires
more.

## Timers and alarms

```
"set a timer for ten minutes"
"wake me at seven"                      -> rings at 07:00, once
"alarm at 6:30 every weekday"           -> repeats, skips the weekend
"remind me to take the bins out at six"
"what timers do I have?"  ·  "cancel the pasta timer"
"snooze"  ·  "stop"                     -> while it's ringing
```

Three things make these trustworthy rather than decorative:

- **They survive a restart.** Everything is stored in `~/.local/state/pot/alarms.json` and reloaded
  on start, because a device gets rebooted and an alarm you can't rely on is worse than none.
  Anything that came due while Pot was off fires immediately: waking up late beats not waking up.
- **They ring rather than announce.** A single spoken sentence is missable. Pot chimes, gets
  gradually louder, ducks any music, and keeps going until you say stop or snooze, giving up after
  three minutes so a forgotten alarm doesn't ring at an empty house.
- **Recurring alarms keep their wall-clock time.** "7am on weekdays" is recomputed from 7am rather
  than by adding 24 hours, so it doesn't drift an hour when the clocks change, and snoozing a
  recurring alarm doesn't move tomorrow's.

```bash
uv run pot alarms list          # what's pending, without starting a conversation
uv run pot alarms cancel 2
```

## Echo: the mic hearing the speaker

A speaker and a microphone in one box create two separate problems, and only one of them is
solvable in software here.

**Pot hearing itself.** While Pot speaks, the mic gate already stops that audio reaching OpenAI,
but the wake word detector has to keep listening or you could never interrupt. Since Pot knows
exactly what it sent to the speaker, it subtracts that from the detector's copy of the microphone.
Measured on a MacBook at full volume, that removes about **8 dB** (roughly 84% of the echo energy).
It's suppression, not true cancellation, so it leaves artefacts, which is why it's applied only to
the detector and never to what Pot sends to the model. Turn it off with `echo_suppression = false`.

```bash
uv run pot audio echo     # how much the mic hears the speaker, and what suppression removes
```

Run that at your normal volume. If it reports the speaker is barely louder than the room, echo
isn't your problem.

**Music.** This one can't be fixed the same way. The audio comes from the Spotify app or from
librespot, a different process, so Pot has no reference signal to subtract. Three real options,
best first:

1. **Don't share the speaker.** Set `preferred_device` under `[music]` to an Echo, a phone or
   another speaker. Zero echo, no processing, and Pot's own speaker stays free for its voice.
2. **Use an OS echo canceller** on the Pi, which taps the speaker's monitor and so cancels
   *whatever* is playing, including music from other processes:
   ```bash
   sudo apt install pipewire wireplumber
   # then load module-echo-cancel and point input_device at the echo-cancelled source
   ```
   This is webrtc's AEC3 and is far better than what Pot does in-process. `pot audio devices` will
   show the new source once it exists.
3. **Let Pot be the speaker** (`[music.speaker]`, above). librespot's audio is mixed through Pot,
   so it is in the reference the suppressor subtracts. That routing is what any in-process
   cancellation needs first, but the suppressor is built for speech and is the wrong tool for
   continuous music, so expect it to help rather than solve this.

## As an audio-only device

The Pi build has no screen and no keyboard, so nothing can rely on the terminal. Everything the
terminal says has a sound:

| Sound | Means |
|---|---|
| rising two-tone blip | Pot is listening |
| falling blip | mic closed |
| soft double tick, every 5s | a tool is still running (a web search takes 10-30s) |
| low buzz | something failed |

Cues never play over Pot's own speech, because playback is a queue rather than a mixer: a cue
during a reply would land after it. Tune with `cues` and `cue_volume` under `[audio]`, `pot audio
cues` plays them, and `--quiet` silences them.

```bash
uv run pot talk --hands-free      # always-on mic, no keypress to start
```

One thing still assumes a laptop, with a workaround:

- **Spotify login needs a browser.** On a headless box, forward the callback port and run the
  login there, then open the printed URL on your laptop:
  ```bash
  ssh -L 8765:127.0.0.1:8765 pi@pot
  uv run pot music login --no-browser
  ```

## What it costs

Pot measures rather than estimates: the realtime session reports exact token counts per
response, including how much of the resent context came from cache, and the research skill adds
its own. Each turn prints its cost, and the session prints a breakdown on exit.

```bash
uv run pot costs     # configured rates, and which models this setup will spend on
```

Defaults are picked for cheapness: `gpt-realtime-2.1-mini` for voice (3.2× cheaper than the full
model on audio both ways), `gpt-5.4-nano` for lookups with `gpt-5.6` held back for
`depth="deep"`, input transcription off, and output capped at 800 tokens. Measured on the same
two exchanges, one light command plus one weather question: **$0.078 before, $0.0102 after.**

Rates live under `[costs.models]` as dollars per million tokens (shipped defaults, overridable in
your own config). When
OpenAI's prices move, edit that table and nothing else; `pot doctor` warns if a model in use has
no rates configured.

Other things that matter for the bill:

- **Long sessions cost more per turn**, because every turn resends the conversation. Caching
  covers the stable part at roughly 1% of full price, but quitting and restarting is cheaper than
  leaving one session open all day.
- **Push-to-talk means you don't pay for silence.** Always-on mode streams continuously.
- **Input transcription** (`transcribe_input = true`) is billed per minute and only feeds the
  "you said" line in the terminal. Pot's own side is transcribed either way.
- **`async_tool_calls = false`** makes multi-tool turns about 30% cheaper and noticeably
  shorter, because each result no longer gets its own reply. The cost is that Pot can't hear you
  until the tool finishes, which on a 30-second search is a long silence. Measured on one
  weather-plus-light turn: 3 replies / 50 words / $0.0108 with it on, 2 replies / 30 words /
  $0.0073 with it off.

## Adding a skill

Drop one file in `src/pot/skills/` that exports a `SKILL`. Nothing else needs editing: the
registry finds it, its tools go into the session, and its prompt fragment is appended to the
persona.

```python
from agents import function_tool
from pot.skills.base import Skill


@function_tool
def coin_flip() -> str:
    """Flip a coin."""
    return "heads"


SKILL = Skill(
    name="coinflip",
    description="Flip a coin",
    tools=(coin_flip,),
    prompt="Use coin_flip when asked to pick between two things at random.",
)
```

`uv run pot skills` lists what's loaded. Turn one off with `disabled = ["coinflip"]` under
`[skills]`, or restrict to a subset with `enabled = [...]`.

Skills that need something long-lived (a connection pool, an HTTP client) should park it on the
runtime with `runtime().resource(key, factory)`; anything with an async `close` is shut down
with the session. See `src/pot/skills/lights/service.py`.

## Layout

| Path | What |
|---|---|
| `src/pot/cli.py` | `talk`, `doctor`, `skills`, `costs`, and the `lights`, `music`, `audio`, `alarms`, `config`, `service` groups |
| `src/pot/voice/` | persona, agent assembly, the realtime event loop and reconnects |
| `src/pot/audio/` | mic capture, the playback mixer, earcons, echo suppression, push-to-talk keys |
| `src/pot/audio/wake/` | local wake word: framing, resampling, debounce, the engine seam |
| `src/pot/skills/` | one module per capability, each exporting `SKILL` |
| `src/pot/skills/lights/` | discovery, provider chain (LAN → cloud), alias resolution |
| `src/pot/skills/music/` | Spotify auth, Web API client, speaker choice, librespot, ducking |
| `src/pot/skills/time/` | timers, alarms and reminders: ringing, snooze, delayed light changes |
| `src/pot/alarms.py` | the alarm store on disk: atomic writes, recurrence by wall-clock time |
| `src/pot/service.py` | the systemd user unit |
| `src/pot/defaults.toml` | shipped defaults: every option with a comment |

## Development

```bash
uv sync --extra wakeword   # without it the wake word tests skip
uv run pytest
uv run ruff check . && uv run ruff format .
```

Tests need no API keys and no audio hardware: config, secrets and state are pointed at a throwaway
directory before anything reads them, and the tests that open a real audio stream skip on a
machine without a sound card. The wake word tests fetch the stock `hey_mycroft` model once if it
isn't there yet. CI runs the same thing on every push.

## Not built yet

- **A trained "hey pot".** The stock triggers work; [docs/custom-wake-word.md](docs/custom-wake-word.md)
  covers training your own.
- **Hearing the wake word over loud music** on a shared speaker. See [Echo](#echo-the-mic-hearing-the-speaker)
  for what works today.
- **A setup wizard**, so provider credentials don't need an SSH session.

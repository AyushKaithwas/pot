# Pot

A voice assistant that takes a whole sentence at a time. Speech-to-speech on OpenAI's realtime
models, so "turn the tube light on at 30% warm, kill it in ten minutes, and tell me if it'll
rain tomorrow" is one turn, not four commands.

Runs on macOS now, on a Raspberry Pi 4 later. Wake word comes later too; for now the mic is
push-to-talk.

## Setup

```bash
brew install uv portaudio
uv sync
cp .env.example .env   # then fill in OPENAI_API_KEY
uv run pot doctor
```

`doctor` tells you what's missing: keys, audio devices, and whether each configured light is
answering on the network.

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
4. Copy the project's Access ID and Access Secret into `.env` as `TUYA_API_KEY` and
   `TUYA_API_SECRET`, and set `TUYA_API_REGION` (`in`, `eu`, `us`, `cn`).

Then:

```bash
uv run pot lights keys     # pulls local keys into config/devices.json (gitignored)
uv run pot lights scan     # who's on the LAN, and at which protocol version
uv run pot lights list     # configured lights and their current state
uv run pot lights set beta1 --on -b 30 -t 20
uv run pot lights set beta1 --color ff8800
uv run pot lights dps beta1   # raw data points, when a light behaves oddly
```

Lights are configured in `config/pot.toml` by **gwId**, not IP, because DHCP moves them; Pot
finds the current address from the device's own discovery broadcast at startup.

Two gotchas that cost time on the first device, in case a future one behaves oddly:

- **Work modes.** An RGB light in colour mode parks its white brightness data point, so setting
  brightness or temperature does nothing visible until the light is switched back to white mode.
  `tuya_local` handles that; `pot lights dps` shows the current mode as dp 21.
- **Stale reads.** tinytuya's persistent connection caches the last status, and it doesn't always
  reflect a mode change, so every change re-queries the device before reporting back.

## Talking to it

```bash
uv run pot talk
```

Press space once and then just talk. The mic shuts while Pot is answering, so the speakers can't
talk into the microphone, and reopens by itself once playback has drained. Space toggles it off
again mid-conversation, `q` quits.

Under `[audio]` in `config/pot.toml`: `auto_reopen = false` goes back to one keypress per turn
(use it if the room echoes enough that Pot interrupts itself), and `mode = "always_on"` is fully
hands-free, which is what the Pi will want with a proper mic.

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

Rates live under `[costs.models]` in `config/pot.toml` as dollars per million tokens. When
OpenAI's prices move, edit that table and nothing else; `pot doctor` warns if a model in use has
no rates configured.

Other things that matter for the bill:

- **Long sessions cost more per turn**, because every turn resends the conversation. Caching
  covers the stable part at roughly 1% of full price, but quitting and restarting is cheaper than
  leaving one session open all day.
- **Push-to-talk means you don't pay for silence.** Always-on mode streams continuously.
- **Input transcription** (`transcribe_input = true`) is billed per minute and only feeds the
  "you said" line in the terminal. Pot's own side is transcribed either way.

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
| `src/pot/cli.py` | `talk`, `doctor`, `skills`, `lights …` |
| `src/pot/voice/` | persona, agent assembly, the realtime event loop |
| `src/pot/audio/` | mic capture, playback with flush-on-interrupt, push-to-talk keys |
| `src/pot/skills/` | one module per capability, each exporting `SKILL` |
| `src/pot/skills/lights/` | discovery, provider chain (LAN → cloud), alias resolution |
| `src/pot/scheduler.py` | deferred actions: timers, "in ten minutes" |
| `config/pot.toml` | models, voice, audio devices, lights, scenes |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Later

- **Wake word**: `openWakeWord` in front of the capture stream, opening the same mic gate that
  the spacebar opens today.
- **Raspberry Pi 4**: set `[audio] input_device` / `output_device` to the USB mic and speaker,
  run it under systemd. Echo cancellation matters once mic and speaker share a room.

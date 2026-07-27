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
3. Copy the Client ID into `client_id` under `[music.spotify]` in `config/pot.toml`. There's no
   client secret to copy: Pot uses PKCE.
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

Then set `enabled = true` under `[music.speaker]` (and `backend = "alsa"` on the Pi). Pot starts
librespot with the session, restarts it if it dies, and prefers it over other devices. "Pot"
appears in your Spotify app's device list like any other speaker.

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

Under `[audio]` in `config/pot.toml`: `auto_reopen = false` goes back to one keypress per turn
(use it if the room echoes enough that Pot interrupts itself), and `mode = "always_on"` is fully
hands-free, which is what the Pi will want with a proper mic.

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
during a reply would land after it. Tune with `cues` and `cue_volume` under `[audio]`, and pass
`--quiet` to silence them.

```bash
uv run pot talk --hands-free      # always-on mic, no keypress to start
```

Two things still assume a laptop, and both have a workaround until the setup pass lands:

- **Spotify login needs a browser.** On a headless box, forward the callback port and run the
  login there, then open the printed URL on your laptop:
  ```bash
  ssh -L 8765:127.0.0.1:8765 pi@pot
  uv run pot music login --no-browser
  ```
- **Config is a file.** Editing `config/pot.toml` over SSH works, but a real product needs the
  setup wizard.

Still to build for a screenless device: a wake word (so there's no keypress at all), a physical
button as a fallback, and running under systemd so there's no `q` to quit.

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
| `src/pot/cli.py` | `talk`, `doctor`, `skills`, `lights …` |
| `src/pot/voice/` | persona, agent assembly, the realtime event loop |
| `src/pot/audio/` | mic capture, playback with flush-on-interrupt, push-to-talk keys |
| `src/pot/skills/` | one module per capability, each exporting `SKILL` |
| `src/pot/skills/lights/` | discovery, provider chain (LAN → cloud), alias resolution |
| `src/pot/skills/music/` | Spotify auth, Web API client, speaker choice, ducking |
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

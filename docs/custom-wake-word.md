# Training "hey pot"

The stock openWakeWord models are trained on synthetic speech, so how well they hear you is close
to luck. Measured across three voices at three volumes, `hey_mycroft` fired 9/9 but `hey_jarvis`
managed 6/9 and scored 0.14 on one voice. If none of them hear you reliably, the fix is a model
trained for the phrase you actually want to say, in a voice like yours.

This keeps everything local: training happens once, off the device, and the result is a 1 MB
`.onnx` file that Pot loads like any other model. Nothing is uploaded at runtime and no vendor is
involved.

## Before training, rule out the boring causes

```bash
uv run pot audio test        # is the mic even working, and at a sane level?
uv run pot audio benchmark   # which stock model hears *you*, measured not guessed
uv run pot audio wake        # live scores while you say the current trigger
```

If `benchmark` finds a model that fires on every take with a worst score above 0.7, use that and
skip this document. Training is worth it when nothing clears that bar, or when you want the
assistant to answer to its own name.

## Training

openWakeWord trains on synthetic speech generated from a text phrase, augmented with room
impulse responses and background noise. A GPU makes this an hour rather than a day, so Colab is
the practical route.

1. Open openWakeWord's training notebook:
   [automatic_model_training.ipynb](https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb)
   in Colab, with a GPU runtime.
2. Set the target phrase to `hey pot`. Two syllables with a plosive is on the short side; if the
   trained model false-fires a lot, `hey potter` or `okay pot` are easier for a model to isolate.
3. Leave the augmentation defaults alone. They're what make the model survive a real room rather
   than only the clean audio it was trained on.
4. **Add recordings of your own voice** if the notebook offers it. This is the step that fixes an
   accent the synthetic voices don't cover, which is the usual reason a stock model struggles.
5. Train, then download the `.onnx` (not the `.tflite`; Pot runs the ONNX path deliberately, since
   tflite-runtime is deprecated and awkward on ARM).

## Installing it

```bash
mkdir -p ~/.local/state/pot/wakewords
cp ~/Downloads/hey_pot.onnx ~/.local/state/pot/wakewords/
```

In `~/.config/pot/pot.toml`:

```toml
[audio.wake]
models = ["~/.local/state/pot/wakewords/hey_pot.onnx"]
threshold = 0.5
```

A path is recognised by its `.onnx` extension, and its file stem becomes the trigger name in logs.
You can mix a custom model with stock ones, and any of them firing wakes Pot:

```toml
models = ["~/.local/state/pot/wakewords/hey_pot.onnx", "hey_mycroft"]
```

Then measure it in the room it will live in:

```bash
uv run pot audio wake        # say it ten times; count the misses
uv run pot doctor            # confirms the file was found and loaded
```

Tune `threshold` from what you see: real hits on a good model score near 1.000, so if your
detections sit around 0.6 the model needs more training data rather than a lower threshold. And
check the false-fire side by leaving `pot audio wake` running while you talk about something else
entirely.

## Why not a commercial engine

Porcupine detects noticeably better than openWakeWord's stock models, and it was briefly wired in
here. It was removed on purpose. Its native library opens sockets, contains a Picovoice hostname
and reads the machine's platform UUID, which reads as licence validation plus device
fingerprinting; and its free tier is personal and non-commercial, with free custom keywords
restricted to x86_64 so a custom word wouldn't even run on a Pi.

None of that sends your audio anywhere. But an assistant whose whole cost and privacy story is
"nothing leaves the room until you speak to it" shouldn't hand a vendor a device fingerprint to
listen for its own name. The engine seam in `src/pot/audio/wake/` is still there, so adding an
engine later is one class implementing `scores`.

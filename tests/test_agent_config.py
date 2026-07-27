"""Session settings that cost money if they're wrong."""

from __future__ import annotations

from pot.config import Settings, VoiceConfig
from pot.voice.agent import build_run_config


def test_input_transcription_is_off_by_default():
    audio_input = build_run_config(Settings())["model_settings"]["audio"]["input"]
    assert "transcription" not in audio_input


def test_input_transcription_can_be_turned_on():
    settings = Settings(voice=VoiceConfig(transcribe_input=True, transcription_model="whisper-1"))
    audio_input = build_run_config(settings)["model_settings"]["audio"]["input"]
    assert audio_input["transcription"] == {"model": "whisper-1"}


def test_output_length_is_capped():
    model_settings = build_run_config(Settings())["model_settings"]
    assert model_settings["max_output_tokens"] == 800


def test_the_cheap_realtime_model_is_the_default():
    assert build_run_config(Settings())["model_settings"]["model_name"] == "gpt-realtime-2.1-mini"


def test_speech_is_a_little_quicker_than_natural():
    speed = build_run_config(Settings())["model_settings"]["audio"]["output"]["speed"]
    assert 1.0 < speed <= 1.5


def test_tools_stay_asynchronous_so_pot_can_be_interrupted_mid_lookup():
    """With this off, a 30s search leaves Pot unable to hear you at all."""
    assert build_run_config(Settings())["async_tool_calls"] is True


def test_async_tool_calls_can_be_turned_off_for_tighter_replies():
    settings = Settings(voice=VoiceConfig(async_tool_calls=False))
    assert build_run_config(settings)["async_tool_calls"] is False


def test_turn_detection_still_allows_interruptions():
    turn_detection = build_run_config(Settings())["model_settings"]["audio"]["input"]["turn_detection"]
    assert turn_detection["interrupt_response"] is True

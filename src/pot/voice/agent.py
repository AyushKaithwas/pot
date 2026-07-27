"""Assemble the realtime agent from whatever skills are loaded."""

from __future__ import annotations

from collections.abc import Sequence

from agents.realtime import RealtimeAgent, RealtimeRunConfig, RealtimeSessionModelSettings
from agents.realtime.config import RealtimeAudioInputConfig, RealtimeTurnDetectionConfig

from pot.config import Settings
from pot.skills.base import Skill
from pot.voice.persona import build_instructions


def build_agent(settings: Settings, skills: Sequence[Skill]) -> RealtimeAgent:
    tools = [tool for skill in skills for tool in skill.tools]
    return RealtimeAgent(
        name=settings.assistant.name,
        instructions=build_instructions(settings, skills),
        tools=tools,
    )


def build_run_config(settings: Settings) -> RealtimeRunConfig:
    # Nothing speculative goes in here. The SDK's types advertise turn_detection.idle_timeout_ms,
    # but the API rejects it as an unknown parameter, and a rejected session update takes the
    # whole configuration down with it: the model ends up with no tools and improvises excuses.
    # Ending a wake-word conversation is handled locally instead, in voice/session.py.
    turn_detection: RealtimeTurnDetectionConfig = {
        "type": settings.voice.turn_detection,
        "interrupt_response": True,
        "create_response": True,
    }

    audio_input: RealtimeAudioInputConfig = {
        "format": "pcm16",
        "turn_detection": turn_detection,
        "noise_reduction": {"type": "near_field"},
    }
    # Transcribing the user's own audio is a separate per-minute charge and buys only the
    # terminal transcript, so it's opt-in.
    if settings.voice.transcribe_input:
        audio_input["transcription"] = {"model": settings.voice.transcription_model}

    model_settings: RealtimeSessionModelSettings = {
        "model_name": settings.voice.model,
        "output_modalities": ["audio"],
        "audio": {
            "input": audio_input,
            "output": {
                "format": "pcm16",
                "voice": settings.voice.voice,
                "speed": settings.voice.speed,
            },
        },
        "reasoning": {"effort": settings.voice.reasoning_effort},
        "max_output_tokens": settings.voice.max_output_tokens,
    }
    return {
        "model_settings": model_settings,
        "async_tool_calls": settings.voice.async_tool_calls,
    }

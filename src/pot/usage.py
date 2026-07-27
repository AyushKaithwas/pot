"""What a conversation actually costs.

The realtime session reports exact token counts per response, including which part of the resent
context was served from cache, so this measures rather than estimates. Rates live in
config/pot.toml because they change more often than the code does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens. Text-only models leave the audio rates at zero."""

    audio_in: float = 0.0
    cached_audio_in: float = 0.0
    audio_out: float = 0.0
    text_in: float = 0.0
    cached_text_in: float = 0.0
    text_out: float = 0.0


@dataclass
class Tokens:
    fresh_audio_in: int = 0
    cached_audio_in: int = 0
    fresh_text_in: int = 0
    cached_text_in: int = 0
    audio_out: int = 0
    text_out: int = 0

    def add(self, other: Tokens) -> None:
        self.fresh_audio_in += other.fresh_audio_in
        self.cached_audio_in += other.cached_audio_in
        self.fresh_text_in += other.fresh_text_in
        self.cached_text_in += other.cached_text_in
        self.audio_out += other.audio_out
        self.text_out += other.text_out

    def total(self) -> int:
        return (
            self.fresh_audio_in
            + self.cached_audio_in
            + self.fresh_text_in
            + self.cached_text_in
            + self.audio_out
            + self.text_out
        )

    def cost(self, rates: Rates) -> float:
        return (
            self.fresh_audio_in * rates.audio_in
            + self.cached_audio_in * rates.cached_audio_in
            + self.fresh_text_in * rates.text_in
            + self.cached_text_in * rates.cached_text_in
            + self.audio_out * rates.audio_out
            + self.text_out * rates.text_out
        ) / PER_MILLION


class UsageMeter:
    def __init__(self, rates: dict[str, Rates]) -> None:
        self._rates = rates
        self._tokens: dict[str, Tokens] = {}
        self._unpriced: set[str] = set()
        self.turns = 0

    def rates_for(self, model: str) -> Rates | None:
        return self._rates.get(model)

    def record_realtime(self, model: str, event: object) -> float:
        """Record one realtime response's usage and return what that turn cost."""
        tokens = _realtime_tokens(event)
        self.turns += 1
        return self._charge(model, tokens)

    def record_text(self, model: str, *, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        """Record a text call, e.g. the research skill's lookup."""
        tokens = Tokens(
            fresh_text_in=max(0, input_tokens - cached_tokens),
            cached_text_in=cached_tokens,
            text_out=output_tokens,
        )
        return self._charge(model, tokens)

    def cost(self) -> float:
        return sum(tokens.cost(self._rate_or_zero(model)) for model, tokens in self._tokens.items())

    def summary(self) -> str:
        if not self._tokens:
            return "no usage recorded"
        lines = [f"session cost ${self.cost():.4f} over {self.turns} turn(s)"]
        for model, tokens in sorted(self._tokens.items()):
            rates = self.rates_for(model)
            price = f"${tokens.cost(rates):.4f}" if rates else "unpriced"
            lines.append(f"  {model}: {tokens.total():,} tokens, {price}")
            if tokens.cached_audio_in or tokens.cached_text_in:
                cached = tokens.cached_audio_in + tokens.cached_text_in
                lines.append(f"    {cached:,} of those served from cache")
        if self._unpriced:
            lines.append(f"  no rates configured for: {', '.join(sorted(self._unpriced))}")
        return "\n".join(lines)

    def _charge(self, model: str, tokens: Tokens) -> float:
        bucket = self._tokens.setdefault(model, Tokens())
        bucket.add(tokens)
        rates = self.rates_for(model)
        if rates is None:
            if model not in self._unpriced:
                self._unpriced.add(model)
                log.warning("no rates configured for %s, its cost is not being counted", model)
            return 0.0
        return tokens.cost(rates)

    def _rate_or_zero(self, model: str) -> Rates:
        return self._rates.get(model, Rates())


def _realtime_tokens(event: object) -> Tokens:
    """Pull the audio/text split out of a realtime usage event.

    Cached counts are a subset of the input totals, so the fresh part is the difference: paying
    full price for everything would badly overstate a long conversation.
    """
    details_in = getattr(event, "input_tokens_details", None)
    details_out = getattr(event, "output_tokens_details", None)
    cached = getattr(details_in, "cached_tokens_details", None)

    cached_audio = _count(cached, "audio_tokens")
    cached_text = _count(cached, "text_tokens")
    audio_in = _count(details_in, "audio_tokens")
    text_in = _count(details_in, "text_tokens")

    if details_in is None:
        # No breakdown: fall back to the totals so the turn still shows up somewhere.
        usage = getattr(event, "usage", None)
        text_in = _count(usage, "input_tokens")

    tokens = Tokens(
        fresh_audio_in=max(0, audio_in - cached_audio),
        cached_audio_in=cached_audio,
        fresh_text_in=max(0, text_in - cached_text),
        cached_text_in=cached_text,
        audio_out=_count(details_out, "audio_tokens"),
        text_out=_count(details_out, "text_tokens"),
    )
    if details_out is None:
        usage = getattr(event, "usage", None)
        tokens.text_out = _count(usage, "output_tokens")
    return tokens


def _count(source: object, attribute: str) -> int:
    value = getattr(source, attribute, None) if source is not None else None
    return int(value) if isinstance(value, int) else 0

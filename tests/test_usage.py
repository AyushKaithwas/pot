"""Cost accounting.

The numbers only help if they're right, and the easy mistake is charging full price for the
context that was served from cache: it's the bulk of a long conversation's input.
"""

from __future__ import annotations

from dataclasses import dataclass

from pot.usage import Rates, UsageMeter

REALTIME = Rates(
    audio_in=10.0, cached_audio_in=0.30, audio_out=20.0, text_in=0.60, cached_text_in=0.06, text_out=2.40
)
NANO = Rates(text_in=0.20, cached_text_in=0.02, text_out=1.25)


@dataclass
class FakeCached:
    audio_tokens: int = 0
    text_tokens: int = 0
    image_tokens: int = 0


@dataclass
class FakeInputDetails:
    audio_tokens: int = 0
    text_tokens: int = 0
    image_tokens: int = 0
    cached_tokens: int = 0
    cached_tokens_details: FakeCached | None = None


@dataclass
class FakeOutputDetails:
    audio_tokens: int = 0
    text_tokens: int = 0


@dataclass
class FakeTotals:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class FakeUsageEvent:
    input_tokens_details: FakeInputDetails | None = None
    output_tokens_details: FakeOutputDetails | None = None
    usage: FakeTotals | None = None
    type: str = "usage"


def meter() -> UsageMeter:
    return UsageMeter({"gpt-realtime-2.1-mini": REALTIME, "gpt-5.4-nano": NANO})


def test_cached_input_is_charged_at_the_cache_rate():
    event = FakeUsageEvent(
        input_tokens_details=FakeInputDetails(
            audio_tokens=1000,
            text_tokens=500,
            cached_tokens=1200,
            cached_tokens_details=FakeCached(audio_tokens=800, text_tokens=400),
        ),
        output_tokens_details=FakeOutputDetails(audio_tokens=300, text_tokens=50),
    )
    cost = meter().record_realtime("gpt-realtime-2.1-mini", event)
    expected = (
        200 * 10.0  # fresh audio in
        + 800 * 0.30  # cached audio in
        + 100 * 0.60  # fresh text in
        + 400 * 0.06  # cached text in
        + 300 * 20.0  # audio out
        + 50 * 2.40  # text out
    ) / 1_000_000
    assert cost == expected


def test_charging_everything_fresh_would_cost_far_more():
    """Guards the cache split: without it a resent conversation looks enormously expensive."""
    event = FakeUsageEvent(
        input_tokens_details=FakeInputDetails(
            audio_tokens=10_000,
            cached_tokens=9_500,
            cached_tokens_details=FakeCached(audio_tokens=9_500),
        ),
        output_tokens_details=FakeOutputDetails(audio_tokens=200),
    )
    cost = meter().record_realtime("gpt-realtime-2.1-mini", event)
    all_fresh = (10_000 * 10.0 + 200 * 20.0) / 1_000_000
    assert cost < all_fresh / 3


def test_turns_accumulate_across_a_session():
    m = meter()
    event = FakeUsageEvent(
        input_tokens_details=FakeInputDetails(audio_tokens=100),
        output_tokens_details=FakeOutputDetails(audio_tokens=100),
    )
    first = m.record_realtime("gpt-realtime-2.1-mini", event)
    m.record_realtime("gpt-realtime-2.1-mini", event)
    assert m.turns == 2
    assert m.cost() == first * 2


def test_text_calls_share_the_same_tally():
    m = meter()
    m.record_realtime(
        "gpt-realtime-2.1-mini",
        FakeUsageEvent(output_tokens_details=FakeOutputDetails(audio_tokens=1000)),
    )
    research = m.record_text("gpt-5.4-nano", input_tokens=5000, cached_tokens=0, output_tokens=100)
    assert research == (5000 * 0.20 + 100 * 1.25) / 1_000_000
    assert m.cost() == (1000 * 20.0 / 1_000_000) + research
    assert "gpt-5.4-nano" in m.summary()


def test_a_search_on_the_cheap_model_costs_a_fraction_of_the_expensive_one():
    m = UsageMeter({"gpt-5.4-nano": NANO, "gpt-5.6": Rates(text_in=5.0, text_out=30.0)})
    cheap = m.record_text("gpt-5.4-nano", input_tokens=5000, cached_tokens=0, output_tokens=100)
    dear = m.record_text("gpt-5.6", input_tokens=5000, cached_tokens=0, output_tokens=100)
    assert dear > cheap * 10


def test_an_unpriced_model_is_flagged_rather_than_silently_free():
    m = meter()
    assert m.record_realtime("some-new-model", FakeUsageEvent()) == 0.0
    assert "no rates configured for: some-new-model" in m.summary()


def test_missing_details_fall_back_to_the_totals():
    m = meter()
    m.record_realtime(
        "gpt-realtime-2.1-mini", FakeUsageEvent(usage=FakeTotals(input_tokens=400, output_tokens=90))
    )
    assert m.cost() == (400 * 0.60 + 90 * 2.40) / 1_000_000


def test_summary_without_usage_says_so():
    assert meter().summary() == "no usage recorded"

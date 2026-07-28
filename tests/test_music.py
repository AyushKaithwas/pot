"""Choosing a speaker, talking to the API, and ducking around Pot's own voice."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from pot.config import MusicConfig, Settings, SpeakerConfig, SpotifyConfig
from pot.runtime import Runtime, set_runtime
from pot.skills.music.provider import MusicError, MusicUnavailable
from pot.skills.music.service import MusicService
from pot.skills.music.spotify.auth import SpotifyAuth, Token, TokenStore
from pot.skills.music.spotify.client import NoActiveDevice, PremiumRequired, SpotifyClient, SpotifyError
from pot.skills.music.spotify.provider import SpotifyProvider

PHONE = {
    "id": "phone",
    "name": "Sam's iPhone",
    "type": "Smartphone",
    "is_active": True,
    "volume_percent": 70,
    "supports_volume": True,
}
LAPTOP = {
    "id": "laptop",
    "name": "MacBook Pro",
    "type": "Computer",
    "is_active": False,
    "volume_percent": 50,
    "supports_volume": True,
}
POT = {
    "id": "pot",
    "name": "Pot",
    "type": "Speaker",
    "is_active": False,
    "volume_percent": 40,
    "supports_volume": True,
}


def settings(**music) -> Settings:
    base = {"spotify": SpotifyConfig(client_id="test-client")}
    base.update(music)
    return Settings(music=MusicConfig(**base))


def auth_for(tmp_path) -> SpotifyAuth:
    store = TokenStore(tmp_path / "token.json")
    store.save(Token("access", "refresh", time.time() + 3600))
    return SpotifyAuth("test-client", 8765, store)


class FakeManagedSpeaker:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.started = False

    @property
    def enabled(self) -> bool:
        return True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        return None


def provider_for(tmp_path, devices: list[dict], *, speaker_running=False, handler=None, **music):
    calls: list[httpx.Request] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": devices})
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "name": "Brown Eyed Girl",
                                "uri": "spotify:track:1",
                                "artists": [{"name": "Van Morrison"}],
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/me/player"):
            return httpx.Response(
                200,
                json={
                    "is_playing": True,
                    "item": {"name": "Brown Eyed Girl", "artists": [{"name": "Van Morrison"}]},
                    "device": {"name": "Pot", "volume_percent": 65},
                },
            )
        return httpx.Response(204)

    transport = httpx.MockTransport(handler or default_handler)
    config = settings(**music)
    client = SpotifyClient(auth_for(tmp_path), transport=transport)
    provider = SpotifyProvider(config, client=client, speaker=FakeManagedSpeaker(running=speaker_running))
    return provider, calls


async def test_pot_speaker_wins_when_it_is_running(tmp_path):
    provider, _ = provider_for(
        tmp_path, [PHONE, LAPTOP, POT], speaker_running=True, speaker=SpeakerConfig(enabled=True)
    )
    assert (await provider.pick_speaker()).name == "Pot"


async def test_preferred_device_wins_when_pot_is_not_playing(tmp_path):
    provider, _ = provider_for(tmp_path, [PHONE, LAPTOP, POT], preferred_device="MacBook Pro")
    assert (await provider.pick_speaker()).name == "MacBook Pro"


async def test_the_active_device_is_the_fallback(tmp_path):
    provider, _ = provider_for(tmp_path, [LAPTOP, PHONE])
    assert (await provider.pick_speaker()).name == "Sam's iPhone"


async def test_anything_awake_beats_nothing(tmp_path):
    provider, _ = provider_for(tmp_path, [LAPTOP])
    assert (await provider.pick_speaker()).name == "MacBook Pro"


async def test_no_devices_explains_what_to_do(tmp_path):
    provider, _ = provider_for(tmp_path, [])
    with pytest.raises(MusicUnavailable, match="Open Spotify"):
        await provider.pick_speaker()


async def test_pot_is_flagged_in_the_speaker_list(tmp_path):
    provider, _ = provider_for(tmp_path, [PHONE, POT])
    speakers = {speaker.name: speaker for speaker in await provider.speakers()}
    assert speakers["Pot"].is_pot is True
    assert speakers["Sam's iPhone"].is_pot is False


async def test_playing_a_track_sends_uris_and_targets_the_speaker(tmp_path):
    provider, calls = provider_for(tmp_path, [PHONE])
    result = await provider.play("brown eyed girl", "track")
    play = [c for c in calls if c.url.path.endswith("/me/player/play")][0]
    assert b'"uris"' in play.content
    assert play.url.params["device_id"] == "phone"
    assert "Brown Eyed Girl" in result and "Sam's iPhone" in result


async def test_track_search_asks_for_several_so_skip_has_somewhere_to_go(tmp_path):
    provider, calls = provider_for(tmp_path, [PHONE])
    await provider.play("brown eyed girl", "track")
    search = [c for c in calls if c.url.path.endswith("/search")][0]
    assert int(search.url.params["limit"]) > 1


async def test_pausing_an_already_paused_player_is_a_sentence_not_a_crash(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"status": 403, "message": "Player command failed: Restriction violated"}}
        )

    provider, _ = provider_for(tmp_path, [PHONE], handler=handler)
    assert await provider.pause() == "it was already paused"


async def test_skipping_past_the_end_of_the_queue_is_a_sentence(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"status": 403, "message": "Player command failed: Restriction violated"}}
        )

    provider, _ = provider_for(tmp_path, [PHONE], handler=handler)
    assert "nothing is queued" in await provider.skip()
    assert "further back" in await provider.skip(backwards=True)


async def test_unexpected_spotify_errors_become_music_errors(tmp_path):
    """The CLI and the skill only catch MusicError; a raw SpotifyError means a stack trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    provider, _ = provider_for(tmp_path, [PHONE], handler=handler)
    with pytest.raises(MusicError):
        await provider.now_playing()


async def test_an_album_is_played_as_a_context(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [PHONE]})
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200, json={"albums": {"items": [{"name": "Moondance", "uri": "spotify:album:9"}]}}
            )
        assert request.url.path.endswith("/me/player/play")
        assert b'"context_uri"' in request.content
        return httpx.Response(204)

    provider, _ = provider_for(tmp_path, [PHONE], handler=handler)
    assert "Moondance" in await provider.play("moondance", "album")


async def test_a_query_with_no_matches_says_so(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [PHONE]})
        return httpx.Response(200, json={"tracks": {"items": []}})

    provider, _ = provider_for(tmp_path, [PHONE], handler=handler)
    with pytest.raises(MusicError, match="nothing on Spotify"):
        await provider.play("asdkjhasd", "track")


async def test_resolving_a_named_speaker_is_forgiving(tmp_path):
    provider, _ = provider_for(tmp_path, [PHONE, LAPTOP])
    speakers = await provider.speakers()
    assert provider.resolve(speakers, "macbook pro").id == "laptop"
    assert provider.resolve(speakers, "iphone").id == "phone"
    with pytest.raises(MusicError, match="no speaker called"):
        provider.resolve(speakers, "kitchen radio")


async def test_premium_is_reported_as_unavailable_not_as_a_crash(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "Player command failed: Premium required"}})

    provider, _ = provider_for(tmp_path, [], handler=handler)
    with pytest.raises(MusicUnavailable, match="Premium"):
        await provider.speakers()


def test_client_error_types_are_distinguishable():
    assert issubclass(PremiumRequired, SpotifyError)
    assert issubclass(NoActiveDevice, SpotifyError)


class FakeProvider:
    name = "fake"

    def __init__(self, volume: int | None = 70) -> None:
        self._volume = volume
        self.volumes: list[int] = []

    async def volume(self) -> int | None:
        return self._volume

    async def set_volume(self, percent: int) -> None:
        self._volume = percent
        self.volumes.append(percent)

    async def speakers(self):
        return []

    async def play(self, query, kind, speaker):
        return "playing"

    async def pause(self):
        return "paused"

    async def resume(self):
        return "resumed"

    async def skip(self, backwards=False):
        return "skipped"

    async def now_playing(self):
        return "nothing"

    async def close(self):
        return None


async def test_ducking_lowers_then_restores_the_original_volume():
    provider = FakeProvider(volume=70)
    service = MusicService(settings(duck_to_percent=20), provider)
    await service.duck()
    assert provider.volumes == [20]
    await service.unduck()
    assert provider.volumes == [20, 70]


async def test_ducking_twice_does_not_lose_the_original_volume():
    """Pot speaking in two bursts must not restore to the ducked level."""
    provider = FakeProvider(volume=70)
    service = MusicService(settings(duck_to_percent=20), provider)
    await service.duck()
    await service.duck()
    await service.unduck()
    assert provider.volumes == [20, 70]


async def test_unduck_without_a_duck_does_nothing():
    provider = FakeProvider(volume=70)
    await MusicService(settings(), provider).unduck()
    assert provider.volumes == []


async def test_music_quieter_than_the_duck_target_is_left_alone():
    provider = FakeProvider(volume=10)
    service = MusicService(settings(duck_to_percent=20), provider)
    await service.duck()
    await service.unduck()
    assert provider.volumes == []


async def test_setting_the_volume_by_hand_wins_over_a_pending_restore():
    provider = FakeProvider(volume=70)
    service = MusicService(settings(duck_to_percent=20), provider)
    await service.duck()
    await service.set_volume(35)
    await service.unduck()
    assert provider.volumes == [20, 35], "the user's choice must not be undone"


async def test_ducking_survives_a_provider_that_cannot_report_volume():
    provider = FakeProvider(volume=None)
    service = MusicService(settings(), provider)
    await service.duck()
    await service.unduck()
    assert provider.volumes == []


class PlayingProvider:
    """Enough of a provider to see what the service does around it."""

    name = "fake"

    async def play(self, query, kind, speaker):
        return f"playing {query}"

    async def pause(self):
        return "paused"

    async def resume(self):
        return "resumed"

    async def skip(self, backwards=False):
        return "skipped"

    async def close(self):
        return None


async def test_starting_a_track_ends_the_exchange():
    """An Echo goes back to its wake word here, and a follow-up after "play X" is rare."""
    rt = Runtime(Settings())
    set_runtime(rt)
    service = MusicService(Settings(), PlayingProvider())

    assert await service.play("Not Afraid") == "playing Not Afraid"
    assert rt.take_conversation_end(), "otherwise the mic stays open for the whole track"


async def test_resuming_and_skipping_end_it_too():
    for action in ("resume", "skip"):
        rt = Runtime(Settings())
        set_runtime(rt)
        service = MusicService(Settings(), PlayingProvider())
        await getattr(service, action)()
        assert rt.take_conversation_end(), f"{action} leaves music playing as well"


async def test_pausing_keeps_listening():
    """ "Pause", "actually skip it", "volume down" come in runs, so don't shut the mic between them."""
    rt = Runtime(Settings())
    set_runtime(rt)
    service = MusicService(Settings(), PlayingProvider())

    await service.pause()
    assert not rt.take_conversation_end()


async def test_playing_outside_a_session_does_not_crash():
    """`pot music play` from the shell has no session and no mic to close."""
    set_runtime(None)
    service = MusicService(Settings(), PlayingProvider())
    assert await service.play("anything") == "playing anything"


NOW_PLAYING = {
    "is_playing": True,
    "progress_ms": 64_000,
    "shuffle_state": True,
    "repeat_state": "track",
    "item": {
        "name": "Brown Eyed Girl",
        "uri": "spotify:track:abc",
        "duration_ms": 232_000,
        "artists": [{"name": "Van Morrison"}],
    },
    "device": {"name": "Pot", "volume_percent": 65},
}


def rich_provider(tmp_path, extra=None):
    """A provider whose player is mid-track, so seeking and saving have something to act on."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        for suffix, payload in (extra or {}).items():
            if path.endswith(suffix):
                return httpx.Response(200, json=payload)
        if path.endswith("/me/player"):
            return httpx.Response(200, json=NOW_PLAYING)
        if path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [POT]})
        if path.endswith("/search"):
            return httpx.Response(
                200, json={"tracks": {"items": [{"name": "Song", "uri": "spotify:track:z"}]}}
            )
        return httpx.Response(204)

    client = SpotifyClient(auth_for(tmp_path), transport=httpx.MockTransport(handler))
    provider = SpotifyProvider(settings(), client=client, speaker=FakeManagedSpeaker())
    return provider, calls


def sought(calls) -> int:
    for request in calls:
        if request.url.path.endswith("/me/player/seek"):
            return int(request.url.params["position_ms"])
    raise AssertionError("nothing was sought")


async def test_seeking_forward_moves_from_the_current_position(tmp_path):
    provider, calls = rich_provider(tmp_path)
    spoken = await provider.seek(20)
    assert sought(calls) == 84_000, "1:04 plus twenty seconds"
    assert "1:24" in spoken


async def test_seeking_backwards_never_goes_below_zero(tmp_path):
    provider, calls = rich_provider(tmp_path)
    await provider.seek(-600)
    assert sought(calls) == 0


async def test_seeking_from_the_start_is_absolute(tmp_path):
    provider, calls = rich_provider(tmp_path)
    await provider.seek(30, relative=False)
    assert sought(calls) == 30_000


async def test_seeking_past_the_end_stops_short_of_skipping(tmp_path):
    """Spotify treats a seek past the end as a skip, which is rarely what "forward" meant."""
    provider, calls = rich_provider(tmp_path)
    await provider.seek(9999)
    assert sought(calls) == 231_000


async def test_now_playing_says_where_it_is_and_how_it_is_playing(tmp_path):
    provider, _ = rich_provider(tmp_path)
    spoken = await provider.now_playing()
    assert "1:04 of 3:52" in spoken
    assert "shuffled" in spoken
    assert "repeating this track" in spoken


async def test_repeat_only_accepts_modes_spotify_knows(tmp_path):
    provider, _ = rich_provider(tmp_path)
    assert "repeating this track" in await provider.set_repeat("track")
    with pytest.raises(MusicError):
        await provider.set_repeat("sometimes")


async def test_queueing_does_not_start_playback(tmp_path):
    provider, calls = rich_provider(tmp_path)
    await provider.queue("Song")
    assert any(r.url.path.endswith("/me/player/queue") for r in calls)
    assert not any(r.url.path.endswith("/me/player/play") for r in calls), "queueing interrupts nothing"


async def test_saving_uses_the_id_of_what_is_playing(tmp_path):
    provider, calls = rich_provider(tmp_path)
    spoken = await provider.save_current()
    saved = next(r for r in calls if r.url.path.endswith("/me/tracks"))
    assert saved.method == "PUT"
    assert saved.url.params["ids"] == "abc"
    assert "Brown Eyed Girl" in spoken


async def test_unsaving_is_the_same_call_backwards(tmp_path):
    provider, calls = rich_provider(tmp_path)
    await provider.save_current(remove=True)
    assert next(r for r in calls if r.url.path.endswith("/me/tracks")).method == "DELETE"


async def test_a_playlist_is_matched_against_the_users_own(tmp_path):
    """A public search won't find someone's private playlist, so match their own list loosely."""
    provider, calls = rich_provider(
        tmp_path,
        {"/me/playlists": {"items": [{"name": "Morning Run", "uri": "spotify:playlist:pl1"}]}},
    )
    spoken = await provider.add_current_to_playlist("morning run")
    assert any(r.url.path == "/v1/playlists/pl1/tracks" for r in calls)
    assert "Morning Run" in spoken


async def test_an_unknown_playlist_says_so(tmp_path):
    provider, _ = rich_provider(tmp_path, {"/me/playlists": {"items": []}})
    with pytest.raises(MusicError):
        await provider.add_current_to_playlist("nonexistent")


async def test_the_queue_and_recent_tracks_are_read_back(tmp_path):
    provider, _ = rich_provider(
        tmp_path,
        {
            "/me/player/queue": {"queue": [{"name": "Next One", "uri": "spotify:track:n", "type": "track"}]},
            "/me/player/recently-played": {
                "items": [{"track": {"name": "Earlier One", "uri": "spotify:track:e", "type": "track"}}]
            },
        },
    )
    assert "Next One" in await provider.upcoming()
    assert "Earlier One" in await provider.recent()


async def test_seeking_with_nothing_playing_says_so(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = SpotifyClient(auth_for(tmp_path), transport=httpx.MockTransport(handler))
    provider = SpotifyProvider(settings(), client=client, speaker=FakeManagedSpeaker())
    with pytest.raises(MusicError):
        await provider.seek(10)


async def test_a_podcast_episode_plays_as_one_thing_not_a_context(tmp_path):
    """Spotify plays a single episode through `uris`; sent as a context it just fails."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={"episodes": {"items": [{"name": "Ep 1", "uri": "spotify:episode:e1"}]}},
            )
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [POT]})
        return httpx.Response(204)

    client = SpotifyClient(auth_for(tmp_path), transport=httpx.MockTransport(handler))
    provider = SpotifyProvider(settings(), client=client, speaker=FakeManagedSpeaker())
    await provider.play("some podcast", "episode")

    body = json.loads(next(r for r in calls if r.url.path.endswith("/me/player/play")).content)
    assert body == {"uris": ["spotify:episode:e1"]}


async def test_a_whole_show_plays_as_a_context(tmp_path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200, json={"shows": {"items": [{"name": "A Show", "uri": "spotify:show:s1"}]}}
            )
        if request.url.path.endswith("/me/player/devices"):
            return httpx.Response(200, json={"devices": [POT]})
        return httpx.Response(204)

    client = SpotifyClient(auth_for(tmp_path), transport=httpx.MockTransport(handler))
    provider = SpotifyProvider(settings(), client=client, speaker=FakeManagedSpeaker())
    await provider.play("a show", "show")

    body = json.loads(next(r for r in calls if r.url.path.endswith("/me/player/play")).content)
    assert body == {"context_uri": "spotify:show:s1"}


async def test_saving_something_already_saved_says_so_instead(tmp_path):
    """Answering "saved it" when it was already there is a small lie worth not telling."""
    provider, calls = rich_provider(tmp_path, {"/me/tracks/contains": [True]})
    spoken = await provider.save_current()
    assert "already" in spoken
    assert not any(r.method == "PUT" and r.url.path.endswith("/me/tracks") for r in calls)

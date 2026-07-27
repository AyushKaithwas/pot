"""PKCE login and token handling, offline.

Auth is the part that silently rots: a token expires, a refresh returns no new refresh token, a
stored file gets truncated. All of those are cheap to pin here and painful to debug live.
"""

from __future__ import annotations

import time
import urllib.parse

import httpx
import pytest

from pot.skills.music.spotify.auth import (
    SCOPES,
    SpotifyAuth,
    SpotifyAuthError,
    Token,
    TokenStore,
    authorize_url,
    make_challenge,
    make_verifier,
)


def store(tmp_path) -> TokenStore:
    return TokenStore(tmp_path / "token.json")


def test_verifier_is_long_enough_for_spotify():
    verifier = make_verifier()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "+" not in verifier and "/" not in verifier


def test_challenge_is_deterministic_and_url_safe():
    verifier = make_verifier()
    assert make_challenge(verifier) == make_challenge(verifier)
    assert make_challenge(verifier) != make_challenge(make_verifier())
    assert "=" not in make_challenge(verifier)


def test_authorize_url_carries_the_challenge_not_the_verifier():
    verifier = make_verifier()
    url = authorize_url("client-123", "http://127.0.0.1:8765/callback", verifier, "state-abc")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["client_id"] == ["client-123"]
    assert query["code_challenge"] == [make_challenge(verifier)]
    assert verifier not in url, "the verifier must never leave the machine"
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state-abc"]
    assert set(query["scope"][0].split()) == set(SCOPES)


def test_token_store_round_trip_is_owner_only(tmp_path):
    keeper = store(tmp_path)
    keeper.save(Token("access", "refresh", time.time() + 3600))
    loaded = keeper.load()
    assert loaded is not None and loaded.refresh_token == "refresh"
    assert oct(keeper.path.stat().st_mode)[-3:] == "600"


def test_a_corrupt_token_file_is_treated_as_absent(tmp_path):
    keeper = store(tmp_path)
    keeper.path.write_text("{not json")
    assert keeper.load() is None


def test_expiry_has_a_margin_so_tokens_do_not_die_mid_request():
    assert Token("a", "r", time.time() + 3600).expired is False
    assert Token("a", "r", time.time() + 10).expired is True


async def test_refresh_swaps_in_the_new_access_token(tmp_path):
    keeper = store(tmp_path)
    keeper.save(Token("old", "refresh-1", time.time() - 1))
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    auth = SpotifyAuth("client", 8765, keeper, transport=httpx.MockTransport(handler))
    assert await auth.access_token() == "new"
    assert seen[0]["grant_type"] == "refresh_token"
    assert seen[0]["refresh_token"] == "refresh-1"
    # Spotify often omits a new refresh token; the old one has to survive.
    assert keeper.load().refresh_token == "refresh-1"


async def test_a_new_refresh_token_replaces_the_old_one(tmp_path):
    keeper = store(tmp_path)
    keeper.save(Token("old", "refresh-1", time.time() - 1))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"access_token": "new", "refresh_token": "refresh-2", "expires_in": 3600}
        )
    )
    auth = SpotifyAuth("client", 8765, keeper, transport=transport)
    await auth.access_token()
    assert keeper.load().refresh_token == "refresh-2"


async def test_a_rejected_refresh_asks_the_user_to_log_in_again(tmp_path):
    keeper = store(tmp_path)
    keeper.save(Token("old", "refresh-1", time.time() - 1))
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    auth = SpotifyAuth("client", 8765, keeper, transport=transport)
    with pytest.raises(SpotifyAuthError, match="pot music login"):
        await auth.access_token()


async def test_not_logged_in_says_what_to_run(tmp_path):
    auth = SpotifyAuth("client", 8765, store(tmp_path))
    assert auth.logged_in is False
    with pytest.raises(SpotifyAuthError, match="pot music login"):
        await auth.access_token()


async def test_login_without_a_client_id_explains_the_setup(tmp_path):
    auth = SpotifyAuth("", 8765, store(tmp_path))
    with pytest.raises(SpotifyAuthError, match="developer.spotify.com"):
        await auth.login(open_browser=False)


def test_logout_forgets_the_token(tmp_path):
    keeper = store(tmp_path)
    keeper.save(Token("access", "refresh", time.time() + 3600))
    auth = SpotifyAuth("client", 8765, keeper)
    auth.logout()
    assert not keeper.path.exists()
    assert auth.logged_in is False

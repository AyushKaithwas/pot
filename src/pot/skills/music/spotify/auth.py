"""Spotify login, using Authorization Code with PKCE.

PKCE means there's no client secret to protect, which matters because every user brings their
own Spotify app: a secret would have to live in their config in plain text for no benefit.

Spotify caps a development-mode app at five authenticated users and reserves extended quota for
businesses with 250k+ monthly actives, so one shared Pot app can't work. Each user registers
their own at developer.spotify.com; `pot music login` does the rest.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-private",
    "playlist-read-private",
)
# Refresh a little early: a token that expires mid-request just becomes a confusing 401.
EXPIRY_MARGIN_SECONDS = 60
LOGIN_TIMEOUT_SECONDS = 300


class SpotifyAuthError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_MARGIN_SECONDS


class TokenStore:
    """Where the refresh token lives.

    A file with 0600 permissions today; Pass 2 moves this to the OS keychain, which is why
    everything goes through this class rather than touching the path directly.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Token | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
            return Token(data["access_token"], data["refresh_token"], float(data["expires_at"]))
        except (KeyError, ValueError, OSError):
            log.warning("could not read the Spotify token at %s, treating it as absent", self.path)
            return None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "access_token": token.access_token,
                    "refresh_token": token.refresh_token,
                    "expires_at": token.expires_at,
                },
                indent=2,
            )
        )
        self.path.chmod(0o600)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def make_verifier() -> str:
    """A high-entropy string; Spotify requires 43-128 characters."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")


def make_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorize_url(client_id: str, redirect_uri: str, verifier: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "code_challenge_method": "S256",
            "code_challenge": make_challenge(verifier),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


class SpotifyAuth:
    def __init__(
        self,
        client_id: str,
        redirect_port: int,
        store: TokenStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.redirect_port = redirect_port
        self.store = store
        self._transport = transport
        self._token = store.load()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    async def access_token(self, *, force_refresh: bool = False) -> str:
        if self._token is None:
            raise SpotifyAuthError("not logged in to Spotify; run `pot music login`")
        if force_refresh or self._token.expired:
            await self._refresh()
        return self._token.access_token

    async def _refresh(self) -> None:
        assert self._token is not None
        async with self._http() as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._token.refresh_token,
                    "client_id": self.client_id,
                },
            )
        if response.status_code != 200:
            raise SpotifyAuthError(
                f"refreshing the Spotify token failed ({response.status_code}); run `pot music login` again"
            )
        self._token = _token_from_response(response.json(), fallback_refresh=self._token.refresh_token)
        self.store.save(self._token)

    async def login(self, *, open_browser: bool = True) -> None:
        """Run the browser flow and store the result."""
        if not self.client_id:
            raise SpotifyAuthError(
                "no Spotify client id configured. Create an app at developer.spotify.com, add "
                f"{self.redirect_uri} as a redirect URI, and put the client id in pot.toml"
            )
        verifier = make_verifier()
        state = secrets.token_urlsafe(16)
        url = authorize_url(self.client_id, self.redirect_uri, verifier, state)

        catcher = _RedirectCatcher(self.redirect_port, state)
        with catcher:
            if open_browser:
                webbrowser.open(url)
            print(f"Opening Spotify to authorise Pot. If nothing opened, visit:\n{url}\n")
            code = catcher.wait(LOGIN_TIMEOUT_SECONDS)

        async with self._http() as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": verifier,
                },
            )
        if response.status_code != 200:
            raise SpotifyAuthError(f"Spotify rejected the login ({response.status_code}): {response.text}")
        self._token = _token_from_response(response.json())
        self.store.save(self._token)

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self._transport)

    def logout(self) -> None:
        self.store.clear()
        self._token = None


def _token_from_response(payload: dict, fallback_refresh: str = "") -> Token:
    refresh = payload.get("refresh_token") or fallback_refresh
    if not payload.get("access_token") or not refresh:
        raise SpotifyAuthError("Spotify's token response was missing a token")
    return Token(
        access_token=payload["access_token"],
        refresh_token=refresh,
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
    )


class _RedirectCatcher:
    """A one-request local web server that catches Spotify's redirect."""

    def __init__(self, port: int, state: str) -> None:
        self.port = port
        self.state = state
        self.code: str | None = None
        self.error: str | None = None
        self._done = threading.Event()
        self._server: http.server.HTTPServer | None = None

    def __enter__(self) -> _RedirectCatcher:
        catcher = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("state", [""])[0] != catcher.state:
                    catcher.error = "the login response didn't match this request"
                elif "error" in query:
                    catcher.error = query["error"][0]
                else:
                    catcher.code = query.get("code", [""])[0] or None
                    if not catcher.code:
                        catcher.error = "no authorisation code came back"
                body = (
                    "Pot is connected to Spotify. You can close this tab."
                    if catcher.code
                    else f"Login failed: {catcher.error}"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())
                catcher._done.set()

            def log_message(self, *args: object) -> None:
                return None

        self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def wait(self, timeout: float) -> str:
        if not self._done.wait(timeout):
            raise SpotifyAuthError("timed out waiting for the Spotify login to come back")
        if self.error or not self.code:
            raise SpotifyAuthError(self.error or "the Spotify login did not return a code")
        return self.code

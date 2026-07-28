"""A thin async wrapper over the bits of the Spotify Web API Pot uses.

spotipy would be the obvious choice, but it's synchronous, so every call would need a thread
hop inside an event loop that's already streaming audio. This is a couple of hundred lines
instead, over the httpx that openai already pulls in.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from pot.skills.music.spotify.auth import SpotifyAuth

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
# Podcasts are searched as episodes and shows, and play through the same player endpoints.
SEARCH_KINDS = {"track", "album", "artist", "playlist", "episode", "show"}
REPEAT_MODES = {"off", "track", "context"}


class SpotifyError(RuntimeError):
    pass


class NoActiveDevice(SpotifyError):
    """Spotify has nowhere to play: every device is asleep or gone."""


class PremiumRequired(SpotifyError):
    """Playback control is a Premium-only feature, and the account doesn't have it."""


class RestrictionViolated(SpotifyError):
    """Spotify refused the command in the current state: pausing while paused, skipping past
    the end of the queue, and similar."""


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    kind: str
    is_active: bool
    volume_percent: int | None
    supports_volume: bool


@dataclass(frozen=True)
class NowPlaying:
    title: str
    artists: str
    is_playing: bool
    device_name: str | None = None
    volume_percent: int | None = None
    position_ms: int | None = None
    duration_ms: int | None = None
    shuffle: bool = False
    repeat: str = "off"
    uri: str = ""

    @property
    def position(self) -> str:
        """How far in, spoken as a clock, e.g. "1:04 of 3:52"."""
        if self.position_ms is None or self.duration_ms is None:
            return ""
        return f"{clock(self.position_ms)} of {clock(self.duration_ms)}"

    def describe(self) -> str:
        verb = "playing" if self.is_playing else "paused"
        where = f" on {self.device_name}" if self.device_name else ""
        parts = [f"{verb}: {self.title} by {self.artists}{where}"]
        if self.position:
            parts.append(self.position)
        if self.shuffle:
            parts.append("shuffled")
        if self.repeat != "off":
            parts.append("repeating this track" if self.repeat == "track" else "repeating")
        return ", ".join(parts)


@dataclass(frozen=True)
class Playable:
    """Something Spotify can be told to play."""

    name: str
    uri: str
    kind: str
    artists: str = ""

    @property
    def is_track(self) -> bool:
        return self.kind == "track"

    @property
    def plays_alone(self) -> bool:
        """Whether this goes in `uris` rather than as a context.

        A track or a single podcast episode is one thing to play; an album, artist, playlist or
        whole show is a context that brings its own queue.
        """
        return self.kind in {"track", "episode"}

    def describe(self) -> str:
        return f"{self.name} by {self.artists}" if self.artists else self.name


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._auth = auth
        self._client = httpx.AsyncClient(base_url=API_BASE, timeout=15, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def devices(self) -> list[Device]:
        payload = await self._request("GET", "/me/player/devices")
        return [_device(item) for item in (payload or {}).get("devices", [])]

    async def now_playing(self) -> NowPlaying | None:
        payload = await self._request("GET", "/me/player")
        if not payload:
            return None
        item = payload.get("item") or {}
        device = payload.get("device") or {}
        return NowPlaying(
            title=item.get("name", "something"),
            artists=_artists(item),
            is_playing=bool(payload.get("is_playing")),
            device_name=device.get("name"),
            volume_percent=device.get("volume_percent"),
            position_ms=payload.get("progress_ms"),
            duration_ms=item.get("duration_ms"),
            shuffle=bool(payload.get("shuffle_state")),
            repeat=payload.get("repeat_state") or "off",
            uri=item.get("uri", ""),
        )

    async def search(self, query: str, kind: str, limit: int = 1) -> list[Playable]:
        if kind not in SEARCH_KINDS:
            raise SpotifyError(f"can't search for {kind!r}")
        payload = await self._request("GET", "/search", params={"q": query, "type": kind, "limit": limit})
        items = ((payload or {}).get(f"{kind}s") or {}).get("items") or []
        return [
            Playable(name=item.get("name", "unknown"), uri=item["uri"], kind=kind, artists=_artists(item))
            for item in items
            if item and item.get("uri")
        ]

    async def play(
        self, playables: Sequence[Playable] | None = None, *, device_id: str | None = None
    ) -> None:
        """Start playback. Without anything to play this resumes whatever was queued.

        Tracks go in as a list of uris so "skip" has somewhere to land; an album, artist or
        playlist is a context and brings its own queue.
        """
        body: dict[str, Any] = {}
        if playables:
            first = playables[0]
            if first.plays_alone:
                body = {"uris": [p.uri for p in playables if p.plays_alone]}
            else:
                body = {"context_uri": first.uri}
        await self._request("PUT", "/me/player/play", params=_device_param(device_id), json=body or None)

    async def pause(self, *, device_id: str | None = None) -> None:
        await self._request("PUT", "/me/player/pause", params=_device_param(device_id))

    async def next_track(self, *, device_id: str | None = None) -> None:
        await self._request("POST", "/me/player/next", params=_device_param(device_id))

    async def previous_track(self, *, device_id: str | None = None) -> None:
        await self._request("POST", "/me/player/previous", params=_device_param(device_id))

    async def set_volume(self, percent: int, *, device_id: str | None = None) -> None:
        params = {"volume_percent": max(0, min(100, int(percent)))}
        params.update(_device_param(device_id))
        await self._request("PUT", "/me/player/volume", params=params)

    async def transfer(self, device_id: str, *, play: bool = True) -> None:
        await self._request("PUT", "/me/player", json={"device_ids": [device_id], "play": play})

    async def seek(self, position_ms: int, *, device_id: str | None = None) -> None:
        params = {"position_ms": max(0, int(position_ms))}
        params.update(_device_param(device_id))
        await self._request("PUT", "/me/player/seek", params=params)

    async def set_shuffle(self, on: bool, *, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"state": "true" if on else "false"}
        params.update(_device_param(device_id))
        await self._request("PUT", "/me/player/shuffle", params=params)

    async def set_repeat(self, mode: str, *, device_id: str | None = None) -> None:
        if mode not in REPEAT_MODES:
            raise SpotifyError(f"repeat must be one of {', '.join(sorted(REPEAT_MODES))}")
        params: dict[str, Any] = {"state": mode}
        params.update(_device_param(device_id))
        await self._request("PUT", "/me/player/repeat", params=params)

    async def add_to_queue(self, uri: str, *, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"uri": uri}
        params.update(_device_param(device_id))
        await self._request("POST", "/me/player/queue", params=params)

    async def queue(self, limit: int = 5) -> list[Playable]:
        payload = await self._request("GET", "/me/player/queue")
        items = (payload or {}).get("queue") or []
        return [_playable(item) for item in items[:limit] if item and item.get("uri")]

    async def recently_played(self, limit: int = 5) -> list[Playable]:
        payload = await self._request("GET", "/me/player/recently-played", params={"limit": limit})
        items = (payload or {}).get("items") or []
        return [
            _playable(entry["track"]) for entry in items if entry and (entry.get("track") or {}).get("uri")
        ]

    async def saved(self, track_id: str) -> bool:
        payload = await self._request("GET", "/me/tracks/contains", params={"ids": track_id})
        # This endpoint answers with a bare JSON array, where the rest of the API sends an object.
        return bool(isinstance(payload, list) and payload and payload[0])

    async def save(self, track_id: str, *, remove: bool = False) -> None:
        method = "DELETE" if remove else "PUT"
        await self._request(method, "/me/tracks", params={"ids": track_id})

    async def playlists(self, limit: int = 50) -> list[Playable]:
        """The user's own playlists, which a public search won't find."""
        payload = await self._request("GET", "/me/playlists", params={"limit": limit})
        items = (payload or {}).get("items") or []
        return [
            Playable(name=item.get("name", "unknown"), uri=item["uri"], kind="playlist")
            for item in items
            if item and item.get("uri")
        ]

    async def add_to_playlist(self, playlist_id: str, uri: str) -> None:
        await self._request("POST", f"/playlists/{playlist_id}/tracks", json={"uris": [uri]})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retrying: bool = False,
    ) -> dict | list | None:
        token = await self._auth.access_token()
        response = await self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code == 401 and not retrying:
            # The token died early, e.g. the account signed out elsewhere. One retry, then give up.
            await self._auth.access_token(force_refresh=True)
            return await self._request(method, path, params=params, json=json, retrying=True)

        return _interpret(response)


def _interpret(response: httpx.Response) -> dict | list | None:
    if response.status_code == 204 or not response.content:
        return None
    if response.status_code == 404 and "device" in response.text.lower():
        raise NoActiveDevice("Spotify has no active device to play on")
    if response.status_code == 403:
        message = _error_message(response)
        if "premium" in message.lower():
            raise PremiumRequired("Spotify playback control needs a Premium account")
        if "restriction violated" in message.lower():
            raise RestrictionViolated(message)
        raise SpotifyError(message)
    if response.status_code == 429:
        raise SpotifyError("Spotify is rate limiting; try again shortly")
    if response.status_code >= 400:
        raise SpotifyError(f"Spotify said {response.status_code}: {_error_message(response)}")
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or payload)[:200]


def _device_param(device_id: str | None) -> dict:
    return {"device_id": device_id} if device_id else {}


def _device(item: dict) -> Device:
    return Device(
        id=item.get("id") or "",
        name=item.get("name") or "unknown",
        kind=item.get("type") or "unknown",
        is_active=bool(item.get("is_active")),
        volume_percent=item.get("volume_percent"),
        supports_volume=bool(item.get("supports_volume", True)),
    )


def _playable(item: dict) -> Playable:
    kind = item.get("type") or "track"
    return Playable(
        name=item.get("name", "unknown"),
        uri=item["uri"],
        kind=kind,
        artists=_artists(item) or (item.get("show") or {}).get("name", ""),
    )


def clock(milliseconds: int) -> str:
    """Milliseconds as a spoken clock, e.g. 1:04."""
    total = max(0, milliseconds) // 1000
    return f"{total // 60}:{total % 60:02d}"


def _artists(item: dict) -> str:
    artists = item.get("artists")
    if isinstance(artists, list):
        names = [artist.get("name", "") for artist in artists if isinstance(artist, dict)]
        return ", ".join(name for name in names if name)
    owner = item.get("owner")
    if isinstance(owner, dict):
        return owner.get("display_name", "")
    return ""

import asyncio
import base64
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx


ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")
PATH_RE = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist)/([A-Za-z0-9]{22})(?:/|$)")
API_ROOT = "https://api.spotify.com/v1"


class SpotifyError(ValueError):
    pass


def parse_spotify_url(value: str) -> tuple[str, str]:
    value = value.strip()
    uri = URI_RE.fullmatch(value)
    if uri:
        return uri.group(1), uri.group(2)
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise SpotifyError("Paste a valid Spotify link.") from exc
    if parsed.scheme != "https" or hostname not in {
        "open.spotify.com", "www.open.spotify.com"
    }:
        raise SpotifyError("Paste a Spotify track, album, or playlist link.")
    match = PATH_RE.fullmatch(parsed.path)
    if not match or not ID_RE.fullmatch(match.group(2)):
        raise SpotifyError("That Spotify link is not a supported track, album, or playlist.")
    return match.group(1), match.group(2)


def _artists(items: list[dict]) -> str:
    return ", ".join(a.get("name", "") for a in items if a.get("name")) or "Unknown artist"


def normalize_track(track: dict, position: int) -> dict:
    album = track.get("album") or {}
    return {
        "position": position,
        "spotify_id": track.get("id"),
        "title": track.get("name") or "Unknown title",
        "artist": _artists(track.get("artists") or []),
        "album": album.get("name") or "Unknown album",
        "duration_ms": track.get("duration_ms"),
        "isrc": (track.get("external_ids") or {}).get("isrc"),
        "explicit": bool(track.get("explicit", False)),
        "artwork": next((i.get("url") for i in album.get("images", []) if i.get("url")), None),
        "track_number": track.get("track_number"),
        "disc_number": track.get("disc_number"),
    }


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.app_token: str | None = None
        self.app_expiry = 0.0
        self.sessions: dict[str, dict] = {}
        self.states: dict[str, float] = {}
        self.metadata_cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=20, follow_redirects=False)

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def oauth_enabled(self) -> bool:
        return self.enabled and bool(self.redirect_uri)

    async def close(self) -> None:
        await self.http.aclose()

    def login_url(self) -> tuple[str, str]:
        if not self.oauth_enabled:
            raise SpotifyError("Spotify OAuth is not configured.")
        now = time.time()
        self.states = {key: expiry for key, expiry in self.states.items() if expiry > now}
        state = secrets.token_urlsafe(32)
        self.states[state] = now + 600
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": "playlist-read-private playlist-read-collaborative",
            "state": state,
        }
        return "https://accounts.spotify.com/authorize?" + urlencode(params), state

    async def finish_login(self, code: str, state: str) -> str:
        expiry = self.states.pop(state, 0)
        if not expiry or expiry < time.time():
            raise SpotifyError("Spotify sign-in expired. Please try again.")
        token = await self._token_request({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri
        })
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = {
            **token, "expires_at": time.time() + int(token.get("expires_in", 3600))
        }
        return session_id

    async def logout(self, session_id: str | None) -> None:
        if session_id:
            self.sessions.pop(session_id, None)

    async def _token_request(self, data: dict) -> dict:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            response = await self.http.post(
                "https://accounts.spotify.com/api/token", data=data,
                headers={"Authorization": f"Basic {basic}"},
            )
        except httpx.HTTPError as exc:
            raise SpotifyError("Could not connect to Spotify authentication.") from exc
        if response.is_error:
            raise SpotifyError("Spotify sign-in failed. Check the app credentials and callback URL.")
        return response.json()

    async def user_token(self, session_id: str | None) -> str | None:
        if not session_id or session_id not in self.sessions:
            return None
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            if session["expires_at"] - time.time() < 90 and session.get("refresh_token"):
                token = await self._token_request({
                    "grant_type": "refresh_token", "refresh_token": session["refresh_token"]
                })
                session.update(token)
                session["expires_at"] = time.time() + int(token.get("expires_in", 3600))
            return session["access_token"]

    async def _app_access_token(self) -> str:
        if not self.enabled:
            raise SpotifyError("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to enable Spotify lookup.")
        async with self._lock:
            if self.app_token and self.app_expiry > time.time() + 30:
                return self.app_token
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            try:
                response = await self.http.post(
                    "https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                    headers={"Authorization": f"Basic {basic}"},
                )
            except httpx.HTTPError as exc:
                raise SpotifyError("Could not connect to Spotify authentication.") from exc
            if response.is_error:
                raise SpotifyError("Spotify authentication failed. Verify the configured credentials.")
            data = response.json()
            self.app_token = data["access_token"]
            self.app_expiry = time.time() + int(data.get("expires_in", 3600))
            return self.app_token

    async def _get(self, path: str, token: str) -> dict:
        for attempt in range(3):
            try:
                response = await self.http.get(
                    API_ROOT + path, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise SpotifyError("Could not connect to Spotify.") from exc
            if response.status_code == 429 and attempt < 2:
                try:
                    delay = min(5.0, max(0.1, float(response.headers.get("Retry-After", "1"))))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(delay)
                continue
            if response.status_code in (401, 403):
                raise SpotifyError("Spotify did not allow this item. Connect an account for private playlists.")
            if response.status_code == 404:
                raise SpotifyError("Spotify could not find that item.")
            if response.is_error:
                raise SpotifyError("Spotify could not load metadata right now.")
            return response.json()
        raise SpotifyError("Spotify rate limit reached. Please try again shortly.")

    async def _pages(self, url: str, token: str, cap: int) -> list[dict]:
        result = []
        next_url = url
        while next_url and len(result) < cap:
            if not next_url.startswith(API_ROOT + "/"):
                raise SpotifyError("Spotify returned an unexpected pagination link.")
            try:
                response = await self.http.get(
                    next_url, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise SpotifyError("Could not connect to Spotify.") from exc
            if response.status_code == 429:
                try:
                    delay = min(5.0, max(0.1, float(response.headers.get("Retry-After", "1"))))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(delay)
                response = await self.http.get(next_url, headers={"Authorization": f"Bearer {token}"})
            if response.is_error:
                raise SpotifyError("Spotify could not load all playlist tracks.")
            page = response.json()
            result.extend(page.get("items", []))
            if len(result) > cap:
                raise SpotifyError(f"This Spotify collection exceeds the {cap}-track limit.")
            next_url = page.get("next")
        if next_url and len(result) >= cap:
            raise SpotifyError(f"This Spotify collection exceeds the {cap}-track limit.")
        return result[:cap]

    async def resolve(self, value: str, session_id: str | None, max_tracks: int) -> dict:
        kind, item_id = parse_spotify_url(value)
        cache_key = f"{kind}:{item_id}"
        cached = self.metadata_cache.get(cache_key) if not session_id else None
        if cached and cached[0] > time.time():
            return json.loads(json.dumps(cached[1]))
        token = await self.user_token(session_id)
        if token is None and not self.enabled:
            from .spotify_embed import fetch_public_embed
            result = await fetch_public_embed(self.http, kind, item_id, max_tracks)
            self.metadata_cache[cache_key] = (time.time() + 300, result)
            return json.loads(json.dumps(result))
        if token is None:
            token = await self._app_access_token()
        if kind == "track":
            raw = await self._get(f"/tracks/{item_id}", token)
            tracks = [normalize_track(raw, 1)]
            name, images = raw.get("name", "Track"), (raw.get("album") or {}).get("images", [])
        elif kind == "album":
            album = await self._get(f"/albums/{item_id}", token)
            raw_tracks = await self._pages(f"{API_ROOT}/albums/{item_id}/tracks?limit=50", token, max_tracks)
            tracks = []
            for i, raw in enumerate(raw_tracks, 1):
                raw["album"] = {"name": album.get("name"), "images": album.get("images", [])}
                raw["external_ids"] = {}
                tracks.append(normalize_track(raw, i))
            name, images = album.get("name", "Album"), album.get("images", [])
        else:
            playlist = await self._get(f"/playlists/{item_id}", token)
            raw_tracks = await self._pages(f"{API_ROOT}/playlists/{item_id}/tracks?limit=100", token, max_tracks)
            tracks = []
            for item in raw_tracks:
                raw = item.get("track")
                if not raw or not raw.get("id"):
                    continue
                track = normalize_track(raw, len(tracks) + 1)
                tracks.append(track)
            name, images = playlist.get("name", "Playlist"), playlist.get("images", [])
        for i, track in enumerate(tracks, 1):
            track["position"] = i
        artwork = next((i.get("url") for i in images if i.get("url")), None)
        result = {"kind": kind, "name": name or "Spotify collection", "artwork": artwork, "tracks": tracks}
        if not session_id:
            self.metadata_cache[cache_key] = (time.time() + 600, result)
            if len(self.metadata_cache) > 1000:
                self.metadata_cache = {key: value for key, value in self.metadata_cache.items()
                                       if value[0] > time.time()}
        return json.loads(json.dumps(result))

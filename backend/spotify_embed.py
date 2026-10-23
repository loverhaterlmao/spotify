from html.parser import HTMLParser
import json
import re
from urllib.parse import urlparse

import httpx

from .spotify import SpotifyError


MAX_EMBED_BYTES = 5 * 1024 * 1024
ART_HOSTS = {"i.scdn.co", "mosaic.scdn.co"}


class _EmbedParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entity_title = ""
        self.subtitle = ""
        self.page_title = ""
        self.artwork = None
        self.rows = []
        self.row = None
        self.capture = None
        self.capture_parts = []
        self.next_data = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        testid = values.get("data-testid", "")
        if tag == "li" and testid.startswith("tracklist-row-"):
            self.row = {"position": len(self.rows) + 1, "title": "", "artist": "", "duration_ms": None}
        if tag == "link" and values.get("as") == "image":
            self._art(values.get("href"))
        if tag == "img":
            self._art(values.get("src"))
        if tag == "script" and values.get("id") == "__NEXT_DATA__":
            self._begin(tag, "next_data")
        elif tag == "title":
            self._begin(tag, "page_title")
        elif tag == "h1" and testid == "entity-title":
            self._begin(tag, "entity_title")
        elif tag == "h2" and testid == "subtitle":
            self._begin(tag, "subtitle")
        elif self.row is not None and tag == "h3":
            self._begin(tag, "track_title")
        elif self.row is not None and tag == "h4":
            self._begin(tag, "track_artist")
        elif self.row is not None and tag == "div" and testid == "duration-cell":
            self._begin(tag, "duration")

    def _art(self, value):
        if not value or self.artwork:
            return
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname in ART_HOSTS:
            self.artwork = value

    def _begin(self, tag, field):
        self.capture = (tag, field)
        self.capture_parts = []

    def handle_data(self, data):
        if self.capture:
            self.capture_parts.append(data)

    def handle_endtag(self, tag):
        if self.capture and self.capture[0] == tag:
            field = self.capture[1]
            value = " ".join("".join(self.capture_parts).split())
            if field == "next_data":
                try:
                    self.next_data = json.loads(value)
                except (TypeError, ValueError):
                    self.next_data = None
            elif field == "page_title":
                self.page_title = value
            elif field == "entity_title":
                self.entity_title = value
            elif field == "subtitle":
                self.subtitle = value
            elif self.row is not None and field == "track_title":
                self.row["title"] = value
            elif self.row is not None and field == "track_artist":
                self.row["artist"] = value
            elif self.row is not None and field == "duration":
                self.row["duration_ms"] = _duration_ms(value)
            self.capture = None
            self.capture_parts = []
        if tag == "li" and self.row is not None:
            if self.row["title"]:
                self.rows.append(self.row)
            self.row = None


def _duration_ms(value):
    parts = value.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds * 1000 if seconds > 0 else None


def parse_embed_html(kind: str, item_id: str, body: str, max_tracks: int = 300) -> dict:
    parser = _EmbedParser()
    parser.feed(body)
    parser.close()
    name = parser.entity_title or parser.page_title.rsplit(" | Spotify", 1)[0] or "Spotify collection"
    artwork = parser.artwork
    rows = parser.rows
    entity = None
    try:
        entity = parser.next_data["props"]["pageProps"]["state"]["data"]["entity"]
    except (TypeError, KeyError):
        pass
    if isinstance(entity, dict):
        name = str(entity.get("name") or entity.get("title") or name)[:300]
        cover_sources = (entity.get("coverArt") or {}).get("sources") or []
        for cover in cover_sources:
            if isinstance(cover, dict):
                parser._art(cover.get("url"))
                if parser.artwork:
                    artwork = parser.artwork
                    break
        embed_rows = entity.get("trackList")
        if isinstance(embed_rows, list):
            normalized_rows = []
            for row in embed_rows:
                if not isinstance(row, dict) or row.get("entityType") not in {None, "track"}:
                    continue
                title = str(row.get("title") or "").strip()
                if not title:
                    continue
                try:
                    duration = int(row.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0
                uri = str(row.get("uri") or "")
                track_match = re.fullmatch(r"spotify:track:([A-Za-z0-9]{22})", uri)
                normalized_rows.append({
                    "position": len(normalized_rows) + 1,
                    "title": title[:300],
                    "artist": str(row.get("subtitle") or "Unknown artist")[:300],
                    "duration_ms": duration if 0 < duration <= 4 * 60 * 60 * 1000 else None,
                    "spotify_id": track_match.group(1) if track_match else None,
                    "explicit": bool(row.get("isExplicit", False)),
                })
            if normalized_rows:
                rows = normalized_rows
    if len(rows) > max_tracks:
        raise SpotifyError(f"This public Spotify embed exceeds the {max_tracks}-track limit supported by this deployment. Split it into smaller playlists.")
    if not rows and kind == "track" and parser.entity_title:
        rows = [{
            "position": 1,
            "title": parser.entity_title,
            "artist": parser.subtitle or "Unknown artist",
            "duration_ms": None,
        }]
    if not rows:
        raise SpotifyError("Spotify did not expose track details for this public embed. Import a CSV instead.")
    if len(rows) > max_tracks:
        raise SpotifyError(f"This public Spotify embed exceeds the {max_tracks}-track limit. Import a CSV instead.")
    tracks = []
    for index, row in enumerate(rows, 1):
        tracks.append({
            "position": index,
            "spotify_id": row.get("spotify_id") or (item_id if kind == "track" else None),
            "title": row["title"] or "Unknown title",
            "artist": row["artist"] or parser.subtitle or "Unknown artist",
            "album": name if kind == "album" else "",
            "duration_ms": row.get("duration_ms"),
            "isrc": None,
            "explicit": bool(row.get("explicit", False)),
            "artwork": artwork,
            "track_number": index,
            "disc_number": 1,
        })
    return {
        "kind": kind,
        "name": name,
        "artwork": artwork,
        "tracks": tracks,
        "metadata_note": "Public embeds may show only a preview subset. Import CSV if the track count looks incomplete.",
    }


async def fetch_public_embed(client: httpx.AsyncClient, kind: str, item_id: str, max_tracks: int) -> dict:
    url = f"https://open.spotify.com/embed/{kind}/{item_id}?utm_source=generator&theme=0"
    try:
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise SpotifyError("Spotify could not load this public embed. Try a CSV export instead.")
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                raise SpotifyError("Spotify returned an unexpected embed response.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_EMBED_BYTES:
                    raise SpotifyError("Spotify returned an oversized embed response.")
    except httpx.HTTPError as exc:
        raise SpotifyError("Could not connect to Spotify's public embed.") from exc
    try:
        html = bytes(body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpotifyError("Spotify returned invalid embed metadata.") from exc
    return parse_embed_html(kind, item_id, html, min(max_tracks, 300))

import csv
import io
import re
from pathlib import Path

from .spotify import normalize_track


class PlaylistImportError(ValueError):
    pass


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _field(row: dict, aliases: tuple[str, ...]) -> tuple[str, str]:
    values = {_header(key): value for key, value in row.items() if key is not None}
    for alias in aliases:
        value = values.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip(), alias
    return "", ""


def _duration_ms(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if ":" in value:
        try:
            parts = [int(part) for part in value.split(":")]
            if len(parts) == 2:
                seconds = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                return None
            return seconds * 1000 if seconds > 0 else None
        except ValueError:
            return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number <= 0:
        return None
    return int(number if number >= 10000 else number * 1000)


def _number(value: str) -> int | None:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def _spotify_id(row: dict) -> str | None:
    value, _ = _field(row, (
        "spotifytrackid", "trackid", "spotifyid", "spotifyuri", "trackuri",
        "spotifyurl", "trackurl", "spotifylink", "tracklink", "url", "uri", "link",
    ))
    match = re.search(r"(?:spotify:track:|/track/)([A-Za-z0-9]{22})(?:[?/#]|$)", value)
    if match:
        return match.group(1)
    return value if re.fullmatch(r"[A-Za-z0-9]{22}", value) else None


def parse_playlist_csv(data: bytes, filename: str, max_tracks: int) -> dict:
    if not data:
        raise PlaylistImportError("The selected CSV file is empty.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlaylistImportError("Save the playlist export as a UTF-8 CSV file and try again.") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise PlaylistImportError("The CSV file needs a header row.")
        tracks = []
        for row in reader:
            title, _ = _field(row, ("trackname", "songname", "title", "track", "song", "name"))
            artist, _ = _field(row, ("artistnames", "artistname", "artists", "artist", "trackartist"))
            if not title and not artist:
                continue
            if not title or not artist:
                raise PlaylistImportError("Every non-empty row needs both a track title and artist.")
            position = len(tracks) + 1
            album, _ = _field(row, ("albumname", "album", "release"))
            duration, _ = _field(row, ("durationms", "trackdurationms", "duration", "trackduration"))
            isrc, _ = _field(row, ("isrc", "trackisrc"))
            explicit, _ = _field(row, ("explicit", "isexplicit"))
            artwork, _ = _field(row, (
                "albumartwork", "albumimage", "albumcover", "coverurl", "imageurl", "artwork",
            ))
            track_number, _ = _field(row, ("tracknumber", "trackno", "tracknumberwithinalbum"))
            disc_number, _ = _field(row, ("discnumber", "discno"))
            explicit_value = explicit.lower() in {"true", "yes", "1", "explicit"}
            images = [{"url": artwork}] if artwork else []
            tracks.append(normalize_track({
                "id": _spotify_id(row),
                "name": title,
                "artists": [{"name": artist}],
                "album": {"name": album or "Unknown album", "images": images},
                "duration_ms": _duration_ms(duration),
                "external_ids": {"isrc": isrc} if isrc else {},
                "explicit": explicit_value,
                "track_number": _number(track_number),
                "disc_number": _number(disc_number),
            }, position))
            if len(tracks) > max_tracks:
                raise PlaylistImportError(
                    f"This playlist has more than the {max_tracks}-track limit. Export a smaller selection."
                )
    except csv.Error as exc:
        raise PlaylistImportError("The CSV file could not be read.") from exc
    if not tracks:
        raise PlaylistImportError("No tracks with titles and artists were found in that CSV.")
    name = Path(filename or "").stem.strip() or "Imported playlist"
    return {
        "name": name[:120],
        "kind": "playlist",
        "artwork": next((track["artwork"] for track in tracks if track.get("artwork")), None),
        "tracks": tracks,
    }

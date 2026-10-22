import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import aiofiles
import httpx

from .audio import ALLOWED_INPUT_EXTENSIONS


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".aiff", ".aif"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
LICENSE_PATH_RE = re.compile(
    r"^/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)/?$",
    re.IGNORECASE,
)
PUBLIC_DOMAIN_PATHS = {
    "/publicdomain/zero/1.0/": "CC0 1.0",
    "/publicdomain/mark/1.0/": "Public Domain Mark 1.0",
}


class ArchiveSourceError(ValueError):
    pass


def supported_license(value: object) -> tuple[str, str] | None:
    """Accept only explicit CC0, CC BY, CC BY-SA, or Public Domain Mark declarations."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname != "creativecommons.org":
        return None
    path = parsed.path
    path = path.lower()
    if path in PUBLIC_DOMAIN_PATHS:
        label = PUBLIC_DOMAIN_PATHS[path]
        return label, "https://creativecommons.org" + path
    match = LICENSE_PATH_RE.fullmatch(path)
    if not match:
        return None
    kind, version = match.groups()
    label = f"CC {'BY-SA' if kind.lower() == 'by-sa' else 'BY'} {version}"
    return label, "https://creativecommons.org" + path


def _first(value: object) -> str:
    if isinstance(value, list):
        value = next((item for item in value if item), "")
    return str(value or "").strip()


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 4 * 60 * 60 else None


def _license(metadata: dict) -> tuple[str, str] | None:
    value = metadata.get("licenseurl") or metadata.get("license_url")
    return supported_license(_first(value))


def _safe_identifier(value: object) -> str:
    identifier = str(value or "")
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise ArchiveSourceError("The source returned an invalid item identifier.")
    return identifier


def _safe_filename(value: object) -> str:
    filename = str(value or "")
    if (
        not filename or len(filename) > 255 or filename in {".", ".."} or
        "/" in filename or "\\" in filename or ".." in filename or
        Path(filename).name != filename
    ):
        raise ArchiveSourceError("The source returned an unsafe audio filename.")
    return filename


def _duration(file_info: dict, item_metadata: dict) -> float | None:
    for value in (file_info.get("length"), file_info.get("duration"), item_metadata.get("length")):
        if isinstance(value, str) and ":" in value:
            try:
                parts = [float(part) for part in value.split(":")]
                duration = 0.0
                for part in parts:
                    duration = duration * 60 + part
                if 0 < duration <= 4 * 60 * 60:
                    return duration
            except ValueError:
                pass
        result = _positive_number(value)
        if result is not None:
            return result
    return None


def _file_size(file_info: dict) -> int | None:
    try:
        size = int(file_info.get("size", 0))
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


class InternetArchiveSource:
    """Search and download only Internet Archive files with an explicit supported license."""

    name = "internet-archive"
    base_url = "https://archive.org"
    max_results = 10

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.http = client or self._new_client()
        self._owns_client = client is None
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._min_request_interval = 0.35

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=6.0),
            headers={"User-Agent": "Soundloom/0.2 licensed-audio-search"},
            follow_redirects=False,
        )

    async def open(self) -> None:
        if self.http.is_closed:
            self.http = self._new_client()
            self._owns_client = True

    async def close(self) -> None:
        if self._owns_client and not self.http.is_closed:
            await self.http.aclose()

    async def _pace(self) -> None:
        async with self._request_lock:
            now = asyncio.get_running_loop().time()
            delay = self._last_request + self._min_request_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = asyncio.get_running_loop().time()

    @staticmethod
    def _phrase(value: str) -> str:
        safe = "".join(char if char.isalnum() or char in " '-_" else " " for char in value)
        safe = " ".join(safe.split())[:160].strip()
        return '"' + safe.replace('"', "") + '"'

    async def _json(
        self,
        url: str,
        params: dict | list[tuple[str, str]] | None = None,
        *,
        cache: bool = True,
    ) -> dict:
        if urlparse(url).hostname != "archive.org" or urlparse(url).scheme != "https":
            raise ArchiveSourceError("The audio source request was blocked.")
        params_key = tuple(sorted(params.items())) if isinstance(params, dict) else tuple(params or [])
        cache_key = (url, params_key)
        now = time.time()
        cached = self._cache.get(cache_key)
        if cache and cached and now - cached[0] < 600:
            return cached[1]
        if cached:
            self._cache.pop(cache_key, None)
        for attempt in range(3):
            try:
                await self._pace()
                async with self.http.stream("GET", url, params=params, follow_redirects=False) as response:
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        try:
                            delay = min(4.0, max(0.0, float(response.headers.get("Retry-After", ""))))
                        except ValueError:
                            delay = 0.4 * (2 ** attempt)
                        retry = True
                    elif response.status_code != 200:
                        raise ArchiveSourceError("The licensed audio catalog could not answer the search.")
                    else:
                        retry = False
                        content_length = response.headers.get("Content-Length")
                        if content_length and content_length.isdigit() and int(content_length) > 5 * 1024 * 1024:
                            raise ArchiveSourceError("The licensed audio catalog returned oversized metadata.")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 5 * 1024 * 1024:
                                raise ArchiveSourceError("The licensed audio catalog returned oversized metadata.")
                        try:
                            payload = json.loads(body)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise ArchiveSourceError("The licensed audio catalog returned invalid metadata.") from exc
                        if not isinstance(payload, dict):
                            raise ArchiveSourceError("The licensed audio catalog returned invalid metadata.")
                        if cache:
                            if len(self._cache) >= 512:
                                self._cache.pop(next(iter(self._cache)))
                            self._cache[cache_key] = (time.time(), payload)
                        return payload
                if retry:
                    await asyncio.sleep(delay)
                    continue
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise ArchiveSourceError("The licensed audio catalog is temporarily unavailable.") from exc
                await asyncio.sleep(0.4 * (2 ** attempt))
        raise ArchiveSourceError("The licensed audio catalog is temporarily unavailable.")

    async def _search_ids(self, track: dict, include_artist: bool) -> list[str]:
        title = self._phrase(str(track.get("title") or ""))
        if not title.strip('"'):
            return []
        clauses = [f"title:{title}", "mediatype:audio"]
        artist = self._phrase(str(track.get("artist") or "")) if include_artist else ""
        if artist.strip('"'):
            clauses.insert(1, f"creator:{artist}")
        params = [
            ("q", " AND ".join(clauses)),
            ("fl[]", "identifier"),
            ("fl[]", "licenseurl"),
            ("rows", str(self.max_results * 2)),
            ("page", "1"),
            ("output", "json"),
        ]
        payload = await self._json(f"{self.base_url}/advancedsearch.php", params=params)
        response = payload.get("response", {})
        docs = response.get("docs", []) if isinstance(response, dict) else []
        identifiers = []
        for doc in (docs if isinstance(docs, list) else []):
            if not isinstance(doc, dict):
                continue
            # Skip records whose indexed license is known to fall outside the allowlist.
            # Blank or missing fields remain unknown and still get authoritative metadata checks.
            declared_license = _first(doc.get("licenseurl") or doc.get("license_url"))
            if declared_license and not _license(doc):
                continue
            try:
                identifier = _safe_identifier(doc.get("identifier"))
            except ArchiveSourceError:
                continue
            if identifier not in identifiers:
                identifiers.append(identifier)
        return identifiers

    async def search(self, track: dict, max_file_bytes: int) -> list[dict]:
        identifiers = await self._search_ids(track, include_artist=True)
        if len(identifiers) < 3:
            for identifier in await self._search_ids(track, include_artist=False):
                if identifier not in identifiers:
                    identifiers.append(identifier)
                if len(identifiers) >= self.max_results:
                    break
        identifiers = identifiers[:self.max_results]
        metadata_slots = asyncio.Semaphore(4)

        async def fetch_metadata(identifier: str) -> dict:
            async with metadata_slots:
                return await self._json(f"{self.base_url}/metadata/{quote(identifier, safe='')}")

        # Bound detail lookups and overlap network latency while the shared host pacer
        # continues to respect the catalog request interval.
        payloads = await asyncio.gather(*(fetch_metadata(identifier) for identifier in identifiers))
        candidates: list[dict] = []
        seen: set[str] = set()
        for identifier, payload in zip(identifiers, payloads):
            item_metadata = payload.get("metadata", {})
            if not isinstance(item_metadata, dict) or not isinstance(payload.get("files"), list):
                continue
            license_value = _license(item_metadata)
            if not license_value:
                continue
            license_name, license_url = license_value
            item_title = _first(item_metadata.get("title"))
            creator = _first(item_metadata.get("artist") or item_metadata.get("creator"))
            album = _first(item_metadata.get("album"))
            isrc = _first(item_metadata.get("isrc") or item_metadata.get("ISRC"))
            audio_files = []
            for file_info in payload["files"]:
                if not isinstance(file_info, dict) or str(file_info.get("private", "")).lower() == "true":
                    continue
                try:
                    filename = _safe_filename(file_info.get("name"))
                except ArchiveSourceError:
                    continue
                extension = Path(filename).suffix.lower()
                size = _file_size(file_info)
                if extension not in AUDIO_EXTENSIONS or extension not in ALLOWED_INPUT_EXTENSIONS:
                    continue
                if not size or size > max_file_bytes:
                    continue
                audio_files.append((file_info, filename, extension, size))
            for file_info, filename, extension, size in audio_files:
                candidate_id = hashlib.sha256(f"{identifier}\0{filename}".encode()).hexdigest()[:32]
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                stem = Path(filename).stem
                file_title = _first(file_info.get("title"))
                title = file_title or (item_title if len(audio_files) == 1 else stem)
                file_creator = _first(file_info.get("artist") or file_info.get("creator")) or creator
                candidates.append({
                    "id": candidate_id,
                    "identifier": identifier,
                    "file_name": filename,
                    "title": title[:200],
                    "artist": file_creator[:300],
                    "album": album[:200] or item_title[:200],
                    "duration_seconds": _duration(file_info, item_metadata),
                    "isrc": isrc[:32],
                    "size": size,
                    "extension": extension,
                    "source_name": self.name,
                    "record_url": f"{self.base_url}/details/{quote(identifier, safe='')}",
                    "license_name": license_name,
                    "license_url": license_url,
                    "attribution": file_creator or creator or item_title,
                })
                if len(candidates) >= self.max_results:
                    return candidates
        return candidates

    async def _verify_candidate(self, candidate: dict, max_bytes: int) -> tuple[str, int]:
        identifier = _safe_identifier(candidate.get("identifier"))
        filename = _safe_filename(candidate.get("file_name"))
        payload = await self._json(
            f"{self.base_url}/metadata/{quote(identifier, safe='')}", cache=False
        )
        metadata = payload.get("metadata", {})
        current_license = _license(metadata) if isinstance(metadata, dict) else None
        if not current_license or current_license[1] != candidate.get("license_url"):
            raise ArchiveSourceError("The source license changed or could not be verified.")
        files = payload.get("files", [])
        file_info = next(
            (item for item in files if isinstance(item, dict) and item.get("name") == filename),
            None,
        )
        if not file_info or str(file_info.get("private", "")).lower() == "true":
            raise ArchiveSourceError("The selected source file is no longer available.")
        size = _file_size(file_info)
        extension = Path(filename).suffix.lower()
        if not size or size > max_bytes or extension not in AUDIO_EXTENSIONS:
            raise ArchiveSourceError("The selected source exceeds the allowed audio file limits.")
        return identifier, size

    async def download(
        self,
        candidate: dict,
        destination: Path,
        max_bytes: int,
        on_progress,
    ) -> int:
        identifier, expected_size = await self._verify_candidate(candidate, max_bytes)
        filename = _safe_filename(candidate.get("file_name"))
        url = f"{self.base_url}/download/{quote(identifier, safe='')}/{quote(filename, safe='')}"
        temporary = destination.with_suffix(destination.suffix + ".downloading")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        completed = False
        current_url = url
        try:
            for attempt in range(3):
                current_url = url
                retry_request = False
                retry_delay = 0.4 * (2 ** attempt)
                total = 0
                try:
                    for _ in range(4):
                        parsed = urlparse(current_url)
                        host = parsed.hostname or ""
                        if parsed.scheme != "https" or not (host == "archive.org" or host.endswith(".archive.org")):
                            raise ArchiveSourceError("A source redirect was blocked.")
                        await self._pace()
                        async with self.http.stream("GET", current_url, follow_redirects=False) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("Location")
                                if not location:
                                    raise ArchiveSourceError("The source redirect was invalid.")
                                next_url = urljoin(current_url, location)
                                next_parsed = urlparse(next_url)
                                next_host = next_parsed.hostname or ""
                                if next_parsed.scheme != "https" or not (
                                    next_host == "archive.org" or next_host.endswith(".archive.org")
                                ):
                                    raise ArchiveSourceError("A source redirect was blocked.")
                                current_url = next_url
                                continue
                            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                                try:
                                    retry_delay = min(4.0, max(0.0, float(response.headers.get("Retry-After", ""))))
                                except ValueError:
                                    pass
                                retry_request = True
                                break
                            if response.status_code != 200:
                                raise ArchiveSourceError("The licensed audio file could not be downloaded.")
                            content_length = response.headers.get("Content-Length")
                            if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                                raise ArchiveSourceError("The licensed audio file exceeds the size limit.")
                            async with aiofiles.open(temporary, "wb") as output:
                                async for chunk in response.aiter_bytes(1024 * 1024):
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise ArchiveSourceError("The licensed audio file exceeds the size limit.")
                                    await output.write(chunk)
                                    await on_progress(min(99.0, total * 100 / expected_size))
                            if total == 0:
                                raise ArchiveSourceError("The licensed audio file was empty.")
                            if total != expected_size:
                                raise ArchiveSourceError("The licensed audio file was incomplete.")
                            os.replace(temporary, destination)
                            completed = True
                            await on_progress(100.0)
                            return total
                        if retry_request:
                            break
                    else:
                        raise ArchiveSourceError("The source used too many redirects.")
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        raise ArchiveSourceError("The licensed audio file could not be downloaded.") from exc
                    retry_request = True
                if retry_request:
                    temporary.unlink(missing_ok=True)
                    if attempt == 2:
                        raise ArchiveSourceError("The licensed audio source is rate limited or unavailable.")
                    await asyncio.sleep(retry_delay)
                    continue
            raise ArchiveSourceError("The licensed audio source is temporarily unavailable.")
        finally:
            if not completed:
                temporary.unlink(missing_ok=True)

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from .sources import SearchableAudioSource


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


class YouTubeSourceError(ValueError):
    pass


class YouTubeSearchSource(SearchableAudioSource):
    """Search only operator-licensed YouTube channels; never uses cookies or playlists."""

    name = "YouTube (licensed channels)"
    provider = "youtube-licensed"

    def __init__(
        self,
        *,
        enabled: bool,
        rights_acknowledged: bool,
        licensed_channel_ids: set[str],
        license_name: str,
        license_url: str,
        ydl_factory=None,
    ):
        self.configured = bool(
            enabled
            and rights_acknowledged
            and licensed_channel_ids
            and license_name.strip()
            and _https_url(license_url)
        )
        self.channel_ids = {
            value for value in licensed_channel_ids if CHANNEL_ID_RE.fullmatch(value)
        }
        self.license_name = license_name.strip()[:120]
        self.license_url = license_url.strip()
        self._ydl_factory = ydl_factory

    @property
    def enabled(self) -> bool:
        return self.configured and bool(self.channel_ids)

    def _client(self, options):
        if self._ydl_factory:
            return self._ydl_factory(options)
        try:
            import yt_dlp
        except ImportError as exc:
            raise YouTubeSourceError("The authorized YouTube search adapter is unavailable.") from exc
        return yt_dlp.YoutubeDL(options)

    def _extract_search(self, query: str) -> dict:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 1,
            "cachedir": False,
        }
        with self._client(options) as ydl:
            result = ydl.extract_info("ytsearch8:" + query, download=False)
        return result if isinstance(result, dict) else {}

    async def search(self, track: dict, max_file_bytes: int) -> list[dict]:
        if not self.enabled:
            return []
        title = " ".join(str(track.get("title") or "").split())[:160]
        artist = " ".join(str(track.get("artist") or "").split())[:160]
        if not title or not artist:
            return []
        try:
            result = await asyncio.to_thread(self._extract_search, title + " " + artist)
        except Exception as exc:
            raise YouTubeSourceError("The authorized audio source search failed.") from exc
        entries = result.get("entries") or []
        candidates = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "")
            channel_id = str(entry.get("channel_id") or entry.get("uploader_id") or "")
            if (
                not VIDEO_ID_RE.fullmatch(video_id)
                or channel_id not in self.channel_ids
                or video_id in seen
            ):
                continue
            seen.add(video_id)
            try:
                duration = float(entry.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            if not 0 < duration <= 4 * 60 * 60:
                continue
            try:
                size = int(entry.get("filesize_approx") or entry.get("filesize") or max_file_bytes)
            except (TypeError, ValueError):
                size = max_file_bytes
            if size <= 0 or size > max_file_bytes:
                continue
            channel = str(entry.get("channel") or entry.get("uploader") or channel_id)[:200]
            candidates.append({
                "id": video_id,
                "provider": self.provider,
                "title": str(entry.get("title") or "")[:300],
                "artist": channel,
                "album": "",
                "duration_seconds": duration,
                "isrc": "",
                "size": size,
                "extension": ".webm",
                "source_name": self.name,
                "record_url": "https://www.youtube.com/watch?v=" + video_id,
                "license_name": self.license_name,
                "license_url": self.license_url,
                "attribution": channel,
            })
            if len(candidates) == 8:
                break
        return candidates

    def _download_sync(self, video_id: str, destination: Path, max_bytes: int, progress_hook) -> int:
        temp_root = Path(tempfile.mkdtemp(prefix="soundloom-ytdlp-", dir=destination.parent))
        target = temp_root / "audio.webm"
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio[ext=webm]",
            "outtmpl": str(target),
            "max_filesize": max_bytes,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "cachedir": False,
            "progress_hooks": [progress_hook],
        }
        try:
            with self._client(options) as ydl:
                ydl.download(["https://www.youtube.com/watch?v=" + video_id])
            files = [
                path for path in temp_root.iterdir()
                if path.is_file() and path.suffix.lower() == ".webm"
            ]
            if len(files) != 1:
                raise YouTubeSourceError("The selected authorized source did not produce one audio file.")
            size = files[0].stat().st_size
            if size <= 0 or size > max_bytes or destination.suffix.lower() != ".webm":
                raise YouTubeSourceError("The selected authorized audio exceeds configured limits.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(files[0]), destination)
            return size
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    async def download(
        self,
        candidate: dict,
        destination: Path,
        max_bytes: int,
        on_progress: Callable[[float], Awaitable[None]],
    ) -> int:
        video_id = str(candidate.get("id") or "")
        if not self.enabled or not VIDEO_ID_RE.fullmatch(video_id):
            raise YouTubeSourceError("The selected source is not in the configured licensed catalog.")
        if candidate.get("provider") != self.provider:
            raise YouTubeSourceError("The selected source provider is invalid.")
        loop = asyncio.get_running_loop()

        def hook(data):
            if data.get("status") != "downloading":
                return
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes")
            if total and downloaded:
                percent = min(99.0, max(0.0, float(downloaded) * 100 / float(total)))
                try:
                    asyncio.run_coroutine_threadsafe(on_progress(percent), loop).result(timeout=2)
                except Exception:
                    pass

        try:
            size = await asyncio.to_thread(self._download_sync, video_id, destination, max_bytes, hook)
        except YouTubeSourceError:
            raise
        except Exception as exc:
            raise YouTubeSourceError("The selected authorized source could not be downloaded.") from exc
        await on_progress(100.0)
        return size


def _https_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)

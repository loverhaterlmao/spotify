import asyncio
import json
import logging
import os
import secrets
import shutil
import time
import uuid
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlparse

import aiofiles
import httpx
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio import ALLOWED_INPUT_EXTENSIONS, AudioError, convert_audio, probe_audio, safe_filename
from .config import settings
from .spotify import SpotifyClient, SpotifyError
from .store import Store
from .sources import SuppliedAudio, UserUploadSource
from .archive_source import ArchiveSourceError, InternetArchiveSource
from .youtube_source import YouTubeSearchSource, YouTubeSourceError
from .matching import rank_candidates
from .playlist_import import PlaylistImportError, parse_playlist_csv


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("spotify-audio")
analyses: dict[str, tuple[float, dict]] = {}
track_tasks: dict[str, asyncio.Task] = {}
archive_locks: dict[str, asyncio.Lock] = {}
storage_lock: asyncio.Lock | None = None
storage_reservations: dict[str, int] = defaultdict(int)
request_windows: dict[str, deque] = defaultdict(deque)
store: Store | None = None
spotify = SpotifyClient(settings.spotify_client_id, settings.spotify_client_secret, settings.spotify_redirect_uri)
archive_source = InternetArchiveSource()
youtube_source = YouTubeSearchSource(
    enabled=settings.youtube_source_enabled,
    rights_acknowledged=settings.youtube_rights_acknowledged,
    licensed_channel_ids=settings.youtube_licensed_channel_ids,
    license_name=settings.youtube_license_name,
    license_url=settings.youtube_license_url,
)
source_adapters = {
    archive_source.name: archive_source,
    youtube_source.provider: youtube_source,
}
encoder_slots: asyncio.Semaphore | None = None
source_slots: asyncio.Semaphore | None = None
source_search_tasks: dict[str, asyncio.Task] = {}
source_download_tasks: dict[str, asyncio.Task] = {}


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class CreateJobRequest(BaseModel):
    analysis_id: str = Field(min_length=20, max_length=80)
    selected_indices: list[int] = Field(min_length=1, max_length=300)
    format: Literal["original", "mp3", "aac", "flac", "wav", "alac", "opus"] = "original"
    bitrate: Literal["128", "192", "256", "320"] | None = "320"


def database() -> Store:
    if store is None:
        raise HTTPException(503, "The job database is starting.")
    return store


def require_job(job_id: str, token: str | None) -> dict:
    if not token:
        raise HTTPException(404, "Job not found.")
    job = database().authorized_job(job_id, token)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


def throttle(request: Request, key: str, limit: int, window: int = 60) -> None:
    now = time.monotonic()
    client = request.client.host if request.client else "unknown"
    queue = request_windows[f"{client}:{key}"]
    while queue and queue[0] < now - window:
        queue.popleft()
    if len(queue) >= limit:
        raise HTTPException(429, "Too many requests. Please wait and try again.")
    queue.append(now)
    if len(request_windows) > 10000:
        for old_key, old_queue in list(request_windows.items()):
            while old_queue and old_queue[0] < now - window:
                old_queue.popleft()
            if not old_queue:
                request_windows.pop(old_key, None)


def track_identity(track: dict) -> str:
    spotify_id = track.get("spotify_id")
    if spotify_id:
        return f"spotify:{spotify_id}"
    fields = (track.get("title"), track.get("artist"), track.get("album"))
    normalized = [" ".join(str(value or "").casefold().split()) for value in fields]
    return "metadata:" + "|".join(normalized)


def _track_candidates(track: dict) -> list[dict]:
    try:
        candidates = json.loads(track.get("source_candidates") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, dict)][:5]


def track_view(track: dict, token: str, job_id: str, rows: list[dict] | None = None) -> dict:
    output = resolve_output(job_id, track["id"], rows)
    downloadable = output is not None
    ext = output.suffix if output else ""
    source = None
    if track.get("source_name"):
        source = {
            "name": track["source_name"], "url": track.get("source_url"),
            "license": track.get("source_license"), "license_url": track.get("source_license_url"),
            "attribution": track.get("source_attribution"),
            "match_score": track.get("source_confidence"),
            "auto_matched": bool(track.get("source_auto")),
        }
    return {
        "id": track["id"], "position": track["position"], "spotify_id": track["spotify_id"],
        "title": track["title"], "artist": track["artist"], "album": track["album"],
        "duration_ms": track["duration_ms"], "track_number": track["track_number"],
        "disc_number": track["disc_number"], "isrc": track["isrc"], "status": track["status"],
        "progress": round(float(track["progress"] or 0), 1), "error": track["error"],
        "source_bitrate": track["source_bitrate"], "lossless_from_source": track["lossless_from_source"],
        "source": source, "source_candidates": _track_candidates(track),
        "duplicate": track["status"] == "duplicate",
        "download_url": f"/api/jobs/{job_id}/tracks/{track['id']}/download?{urlencode({'token': token})}" if downloadable else None,
        "filename": f"{track['position']:02d} - {safe_filename(track['artist'])} - {safe_filename(track['title'])}{ext}" if downloadable else None,
    }


def job_view(job: dict, token: str) -> dict:
    tracks = mark_missing_outputs(job["id"], database().tracks(job["id"]))
    total = len(tracks)
    done_states = {"completed", "failed", "duplicate", "cancelled", "skipped"}
    settled = sum(1 for t in tracks if t["status"] in done_states)
    progress = round(100 * sum(
        100 if t["status"] in done_states else float(t["progress"] or 0) for t in tracks
    ) / max(1, total), 1)
    states = (
        "awaiting_upload", "uploading", "searching", "downloading", "needs_review",
        "queued", "processing", "completed", "failed", "duplicate", "cancelled",
    )
    counts = {state: sum(1 for t in tracks if t["status"] == state) for state in states}
    active_states = {"awaiting_upload", "uploading", "searching", "downloading", "needs_review", "queued", "processing"}
    active = any(t["status"] in active_states for t in tracks)
    busy = counts["searching"] or counts["downloading"] or counts["processing"] or counts["queued"]
    if not active and counts["failed"]:
        status = "partial" if counts["completed"] else "failed"
    elif not active and counts["completed"] + counts["duplicate"] > 0:
        status = "completed"
    elif not active:
        status = "cancelled"
    else:
        status = "processing" if busy else "awaiting_action"
    database().set_job_status(job["id"], status)
    return {
        "id": job["id"], "name": job["name"], "kind": job["kind"], "artwork": job["artwork"],
        "format": job["output_format"], "bitrate": job["bitrate"], "created_at": job["created_at"],
        "status": status, "progress": progress, "settled": settled, "total": total,
        "counts": counts, "active": active,
        "zip_url": f"/api/jobs/{job['id']}/archive.zip?{urlencode({'token': token})}" if counts["completed"] else None,
        "tracks": [track_view(t, token, job["id"], tracks) for t in tracks],
    }


async def refresh_job(job_id: str) -> None:
    job = database().job(job_id)
    if job:
        rows = database().tracks(job_id)
        active_states = {"awaiting_upload", "uploading", "searching", "downloading", "needs_review", "queued", "processing"}
        active = any(t["status"] in active_states for t in rows)
        failed = any(t["status"] == "failed" for t in rows)
        completed = any(t["status"] == "completed" for t in rows)
        busy = any(t["status"] in {"searching", "downloading", "queued", "processing"} for t in rows)
        status = ("processing" if busy else "awaiting_action") if active else (
            "partial" if failed and completed else "failed" if failed else "completed" if completed else "cancelled"
        )
        database().set_job_status(job_id, status)


def stored_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) if folder.exists() else 0


def estimate_output_bytes(source: Path, info: dict, output_format: str, bitrate: str | None) -> int:
    duration = info["duration"]
    if output_format == "original":
        return source.stat().st_size
    if output_format == "mp3":
        rate = int(bitrate or "320") * 1000
    elif output_format == "aac":
        rate = 256000
    elif output_format == "opus":
        rate = 160000
    elif output_format == "alac":
        rate = 900000
    elif output_format == "flac":
        rate = 1200000
    else:
        return int(duration * max(1, info.get("sample_rate", 44100)) * max(1, info.get("channels", 2)) * 2 + 65536)
    return int(duration * rate / 8 * 1.2 + 65536)


async def run_track(job_id: str, track_id: str) -> None:
    reserved = 0
    try:
        if encoder_slots is None:
            raise RuntimeError("encoder queue is not ready")
        async with encoder_slots:
            job = database().job(job_id)
            track = database().track(job_id, track_id)
            if not job or not track or track["status"] not in {"queued", "processing"}:
                return
            supplied_source = await UserUploadSource({
                track_id: SuppliedAudio(Path(track["input_path"]), Path(track["input_path"]).name)
            }).acquire(track_id)
            source = supplied_source.path
            if not source.is_file():
                database().set_track(track_id, status="failed", progress=0, error="Uploaded audio is missing.")
                return
            source_info = await probe_audio(source)
            estimate = estimate_output_bytes(source, source_info, job["output_format"], job["bitrate"])
            job_root = settings.data_dir / "jobs" / job_id
            if storage_lock is None:
                raise RuntimeError("storage guard is not ready")
            async with storage_lock:
                used = stored_bytes(job_root)
                free = shutil.disk_usage(settings.data_dir).free
                pending = storage_reservations[job_id]
                global_pending = storage_reservations["__global__"]
                if (used + pending + estimate > settings.max_uploads_per_job_bytes
                        or stored_bytes(settings.data_dir) + global_pending + estimate > settings.max_total_storage_bytes
                        or estimate > free - global_pending):
                    raise AudioError("This job does not have enough temporary storage for the requested output.")
                storage_reservations[job_id] += estimate
                storage_reservations["__global__"] += estimate
                reserved = estimate
            database().set_track(track_id, status="processing", progress=1, error=None)
            folder = settings.data_dir / "jobs" / job_id / "audio"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / track_id
            async def on_progress(value: float) -> None:
                database().set_track(track_id, progress=value)
            result = await convert_audio(
                source, target, job["output_format"], job["bitrate"],
                {"title": track["title"], "artist": track["artist"], "album": track["album"],
                 "track": str(track["track_number"] or track["position"]),
                 "disc": str(track["disc_number"] or 1)},
                settings.ffmpeg_timeout_seconds, on_progress,
                source_info=source_info,
            )
            if stored_bytes(job_root) > settings.max_uploads_per_job_bytes:
                Path(result["output_path"]).unlink(missing_ok=True)
                raise AudioError("The encoded output exceeded this job's temporary storage limit.")
            database().set_track(
                track_id, status="completed", progress=100, output_path=result["output_path"],
                source_bitrate=result["bitrate"], lossless_from_source=int(result["lossless_from_source"]),
                error=None,
            )
    except asyncio.CancelledError:
        track = database().track(job_id, track_id)
        if track and track["status"] == "processing":
            database().set_track(track_id, status="cancelled", progress=0, error=None)
        raise
    except Exception as exc:
        logger.info("audio processing failed job=%s track=%s reason=%s", job_id, track_id, type(exc).__name__)
        database().set_track(
            track_id, status="failed", progress=0,
            error=str(exc) if isinstance(exc, AudioError) else "Audio processing failed.",
        )
    finally:
        task = asyncio.current_task()
        if reserved:
            storage_reservations[job_id] = max(0, storage_reservations[job_id] - reserved)
            storage_reservations["__global__"] = max(0, storage_reservations["__global__"] - reserved)
        if track_tasks.get(track_id) is task:
            track_tasks.pop(track_id, None)
        await refresh_job(job_id)


def start_track(job_id: str, track_id: str) -> None:
    existing = track_tasks.get(track_id)
    if existing and not existing.done():
        return
    track_tasks[track_id] = asyncio.create_task(run_track(job_id, track_id))


def start_source_search(job_id: str, track_id: str) -> None:
    existing = source_search_tasks.get(track_id)
    if existing and not existing.done():
        return
    source_search_tasks[track_id] = asyncio.create_task(search_source_track(job_id, track_id))


async def search_source_track(job_id: str, track_id: str) -> None:
    try:
        track = database().track(job_id, track_id)
        if not track or track["status"] != "searching":
            return
        if source_slots is None:
            raise ArchiveSourceError("The licensed audio search queue is not ready.")
        sources = [archive_source]
        if youtube_source.enabled:
            sources.append(youtube_source)

        async def search_one(source):
            async with source_slots:
                return await source.search(track, settings.max_upload_bytes)

        results = await asyncio.gather(
            *(search_one(source) for source in sources),
            return_exceptions=True,
        )
        raw = []
        for source, result in zip(sources, results):
            if isinstance(result, BaseException):
                logger.info("audio source search failed provider=%s reason=%s", source.name, type(result).__name__)
                continue
            for candidate in result:
                candidate.setdefault("provider", source.name)
                raw.append(candidate)
        ranked = rank_candidates(track, raw, settings.auto_match_threshold)
        candidates = [candidate for candidate in ranked if candidate["score"] >= 0.45][:5]
        database().set_track(
            track_id, source_candidates=json.dumps(candidates, ensure_ascii=False),
            progress=0, error=None,
        )
        if candidates and candidates[0]["auto_match"]:
            start_candidate_download(job_id, track_id, candidates[0]["id"], automatic=True)
        elif candidates:
            database().set_track(
                track_id, status="needs_review",
                error="Review the licensed matches and choose one, or attach your own audio.",
            )
        else:
            database().set_track(
                track_id, status="awaiting_upload",
                error="No clearly matching, explicitly licensed recording was found. Retry or attach your own audio.",
            )
        await refresh_job(job_id)
    except asyncio.CancelledError:
        raise
    except ArchiveSourceError:
        logger.info("authorized source search failed job=%s track=%s", job_id, track_id)
        track = database().track(job_id, track_id)
        if track and track["status"] == "searching":
            database().set_track(
                track_id, status="awaiting_upload", progress=0,
                error="The licensed source catalog is unavailable. Retry the search or attach your own audio.",
            )
    except Exception as exc:
        logger.info("authorized source matching failed job=%s track=%s reason=%s", job_id, track_id, type(exc).__name__)
        track = database().track(job_id, track_id)
        if track and track["status"] == "searching":
            database().set_track(
                track_id, status="awaiting_upload", progress=0,
                error="Source matching could not finish. Retry the search or attach your own audio.",
            )
    finally:
        task = asyncio.current_task()
        if source_search_tasks.get(track_id) is task:
            source_search_tasks.pop(track_id, None)
        await refresh_job(job_id)


def start_candidate_download(job_id: str, track_id: str, candidate_id: str, automatic: bool = False) -> None:
    existing = source_download_tasks.get(track_id)
    if existing and not existing.done():
        return
    track = database().track(job_id, track_id)
    if not track:
        return
    candidate = next((item for item in _track_candidates(track) if item.get("id") == candidate_id), None)
    if not candidate:
        return
    database().set_track(track_id, status="downloading", progress=0, error=None)
    source_download_tasks[track_id] = asyncio.create_task(
        download_source_candidate(job_id, track_id, candidate, automatic)
    )


async def download_source_candidate(job_id: str, track_id: str, candidate: dict, automatic: bool) -> None:
    reserved = 0
    destination = None
    try:
        track = database().track(job_id, track_id)
        if not track or track["status"] != "downloading":
            return
        extension = str(candidate.get("extension") or "").lower()
        expected_size = int(candidate.get("size") or 0)
        if extension not in ALLOWED_INPUT_EXTENSIONS or expected_size <= 0 or expected_size > settings.max_upload_bytes:
            raise ArchiveSourceError("The selected licensed audio file exceeds the configured limits.")
        job_root = settings.data_dir / "jobs" / job_id
        input_dir = job_root / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        previous_path = Path(track["input_path"]) if track.get("input_path") else None
        old_size = previous_path.stat().st_size if previous_path and previous_path.is_file() else 0
        if storage_lock is None or source_slots is None:
            raise ArchiveSourceError("The audio source queue is not ready.")
        async with storage_lock:
            job_used = max(0, stored_bytes(job_root) - old_size)
            global_used = max(0, stored_bytes(settings.data_dir) - old_size)
            free = shutil.disk_usage(settings.data_dir).free
            if (job_used + storage_reservations[job_id] + expected_size > settings.max_uploads_per_job_bytes
                    or global_used + storage_reservations["__global__"] + expected_size > settings.max_total_storage_bytes
                    or expected_size > free - storage_reservations["__global__"]):
                raise AudioError("There is not enough temporary storage for this licensed source.")
            storage_reservations[job_id] += expected_size
            storage_reservations["__global__"] += expected_size
            reserved = expected_size
        destination = input_dir / f"{track_id}.source{extension}"

        async def on_progress(value: float) -> None:
            current = database().track(job_id, track_id)
            if current and current["status"] == "downloading":
                database().set_track(track_id, progress=value)

        provider = str(candidate.get("provider") or archive_source.name)
        source = source_adapters.get(provider)
        if source is None:
            raise ArchiveSourceError("The selected source provider is not enabled.")
        async with source_slots:
            actual_size = await source.download(
                candidate, destination, settings.max_upload_bytes, on_progress
            )
        current = database().track(job_id, track_id)
        if not current or current["status"] != "downloading":
            destination.unlink(missing_ok=True)
            return
        source_info = await probe_audio(destination)
        expected_duration = (track.get("duration_ms") or 0) / 1000
        if automatic and expected_duration and candidate.get("duration_seconds"):
            delta = abs(source_info["duration"] - expected_duration)
            if delta > max(2.5, expected_duration * 0.025):
                raise AudioError("The downloaded recording duration did not verify. Choose another match.")
        job_bytes = stored_bytes(job_root) - old_size
        global_bytes = stored_bytes(settings.data_dir) - old_size
        if (actual_size > settings.max_upload_bytes
                or job_bytes + storage_reservations[job_id] - reserved + actual_size > settings.max_uploads_per_job_bytes
                or global_bytes + storage_reservations["__global__"] - reserved + actual_size > settings.max_total_storage_bytes):
            raise AudioError("The downloaded source exceeded the available storage quota.")
        if previous_path and previous_path.is_file() and previous_path != destination:
            previous_path.unlink(missing_ok=True)
        database().set_track(
            track_id, status="queued", progress=0, input_path=str(destination), error=None,
            source_name=candidate.get("source_name"), source_url=candidate.get("record_url"),
            source_license=candidate.get("license_name"), source_license_url=candidate.get("license_url"),
            source_attribution=candidate.get("attribution"), source_confidence=candidate.get("score"),
            source_auto=int(automatic),
        )
        start_track(job_id, track_id)
    except asyncio.CancelledError:
        if destination:
            destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if destination:
            destination.unlink(missing_ok=True)
        logger.info("authorized source download failed job=%s track=%s reason=%s", job_id, track_id, type(exc).__name__)
        current = database().track(job_id, track_id)
        if current and current["status"] == "downloading":
            next_status = "needs_review" if _track_candidates(current) else "awaiting_upload"
            message = str(exc) if isinstance(exc, (ArchiveSourceError, AudioError)) else "The selected licensed audio could not be fetched."
            database().set_track(track_id, status=next_status, progress=0, error=message)
    finally:
        if reserved:
            storage_reservations[job_id] = max(0, storage_reservations[job_id] - reserved)
            storage_reservations["__global__"] = max(0, storage_reservations["__global__"] - reserved)
        task = asyncio.current_task()
        if source_download_tasks.get(track_id) is task:
            source_download_tasks.pop(track_id, None)
        await refresh_job(job_id)


def mark_missing_outputs(job_id: str, rows: list[dict]) -> list[dict]:
    """Keep completed job state aligned with the files that can actually be downloaded."""
    changed = False
    audio_root = (settings.data_dir / "jobs" / job_id / "audio").resolve()
    for row in rows:
        if row["status"] != "completed" or not row.get("output_path"):
            continue
        path = Path(row["output_path"]).resolve()
        try:
            valid = path.parent == audio_root and path.is_file() and path.stat().st_size > 0
        except OSError:
            valid = False
        if not valid:
            database().set_track(
                row["id"], status="failed", progress=0,
                error="The audio output is missing or empty. Retry this track.",
            )
            changed = True
    return database().tracks(job_id) if changed else rows


def resolve_output(job_id: str, track_id: str, rows: list[dict] | None = None) -> Path | None:
    rows = rows if rows is not None else database().tracks(job_id)
    row = next((t for t in rows if t["id"] == track_id), None)
    if not row:
        return None
    if row["status"] == "duplicate":
        row = next((t for t in rows if t["id"] == row["duplicate_of"]), None)
    if not row or row["status"] != "completed" or not row["output_path"]:
        return None
    path = Path(row["output_path"]).resolve()
    audio_root = (settings.data_dir / "jobs" / job_id / "audio").resolve()
    try:
        if path.parent != audio_root or not path.is_file() or path.stat().st_size <= 0:
            return None
    except OSError:
        return None
    return path


def build_archive(job_id: str, job: dict, tracks: list[dict], cover: tuple[bytes, str] | None = None) -> Path:
    root = settings.data_dir / "jobs" / job_id
    root.mkdir(parents=True, exist_ok=True)
    final_path = root / "playlist.zip"
    temp_path = root / "playlist.partial.zip"
    folder_name = safe_filename(job["name"])
    manifest = {
        "name": job["name"], "kind": job["kind"], "format": job["output_format"],
        "created_at": job["created_at"], "tracks": [],
    }
    playlist_lines = ["#EXTM3U"]
    source_credits = []
    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for row in tracks:
            item = {
                "position": row["position"], "title": row["title"], "artist": row["artist"],
                "album": row["album"], "track_number": row["track_number"],
                "disc_number": row["disc_number"], "spotify_id": row["spotify_id"], "isrc": row["isrc"],
                "status": row["status"], "error": row["error"],
                "source_bitrate": row["source_bitrate"],
                "lossless_from_source": bool(row["lossless_from_source"]) if row["lossless_from_source"] is not None else None,
                "source": ({
                    "name": row.get("source_name"), "url": row.get("source_url"),
                    "license": row.get("source_license"), "license_url": row.get("source_license_url"),
                    "attribution": row.get("source_attribution"), "match_score": row.get("source_confidence"),
                    "auto_matched": bool(row.get("source_auto")),
                } if row.get("source_name") else None),
                "filename": None,
            }
            path = resolve_output(job_id, row["id"], tracks)
            if path:
                filename = f"{row['position']:02d} - {safe_filename(row['artist'])} - {safe_filename(row['title'])}{path.suffix}"
                archive.write(path, f"{folder_name}/{filename}")
                duration = max(0, int((row["duration_ms"] or 0) / 1000))
                playlist_lines.append(f"#EXTINF:{duration},{safe_filename(row['artist'])} - {safe_filename(row['title'])}")
                playlist_lines.append(filename)
                item["filename"] = filename
                item["status"] = "completed" if row["status"] == "duplicate" else row["status"]
            elif row["status"] == "completed":
                item["status"] = "failed"
                item["error"] = item["error"] or "The audio output is missing or empty."
                database().set_track(
                    row["id"], status="failed", progress=0, error=item["error"]
                )
            if item["source"]:
                source_credits.append(
                    f"{item['position']:02d}. {item['source'].get('attribution') or item['artist']} — "
                    f"{item['source'].get('url') or ''} — {item['source'].get('license') or ''} "
                    f"{item['source'].get('license_url') or ''}"
                )
            manifest["tracks"].append(item)
        if source_credits:
            archive.writestr(
                f"{folder_name}/SOURCES.txt",
                "Source credits and license declarations (as listed by the provider). Check attribution and share-alike terms before reuse:\n\n" +
                "\n".join(source_credits) + "\n",
            )
        if cover:
            archive.writestr(f"{folder_name}/cover{cover[1]}", cover[0])
        archive.writestr(f"{folder_name}/playlist.m3u8", "\n".join(playlist_lines) + "\n")
        archive.writestr(f"{folder_name}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    try:
        with zipfile.ZipFile(temp_path, "r") as built:
            audio_entries = [
                item for item in built.infolist()
                if Path(item.filename).suffix.lower() in ALLOWED_INPUT_EXTENSIONS
            ]
            if not audio_entries or any(item.file_size <= 0 for item in audio_entries) or built.testzip():
                raise AudioError("The playlist ZIP contains an empty or damaged audio file.")
    except (OSError, zipfile.BadZipFile):
        temp_path.unlink(missing_ok=True)
        raise AudioError("The playlist ZIP failed integrity verification.")
    except AudioError:
        temp_path.unlink(missing_ok=True)
        raise
    os.replace(temp_path, final_path)
    return final_path


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        if store is None:
            continue
        cutoff = time.time() - settings.retention_days * 86400
        for old in store.old_jobs(cutoff):
            shutil.rmtree(settings.data_dir / "jobs" / old["id"], ignore_errors=True)
            store.delete_job(old["id"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    global store, encoder_slots, source_slots, storage_lock
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "jobs.sqlite3")
    encoder_slots = asyncio.Semaphore(settings.max_encoders)
    source_slots = asyncio.Semaphore(settings.archive_search_concurrency)
    storage_lock = asyncio.Lock()
    if spotify.http.is_closed:
        spotify.http = httpx.AsyncClient(timeout=20, follow_redirects=False)
    await archive_source.open()
    for partial in (settings.data_dir / "jobs").rglob("*.downloading"):
        partial.unlink(missing_ok=True)
    for partial in (settings.data_dir / "jobs").rglob("*.uploading"):
        partial.unlink(missing_ok=True)
    for row in store.recoverable():
        if row["status"] in {"searching", "downloading"}:
            store.set_track(row["id"], status="searching", progress=0, error=None)
            start_source_search(row["job_id"], row["id"])
        elif row["status"] == "uploading":
            if row.get("input_path") and Path(row["input_path"]).is_file():
                store.set_track(row["id"], status="queued", progress=0)
                start_track(row["job_id"], row["id"])
            else:
                store.set_track(row["id"], status="awaiting_upload", progress=0, error=None)
        elif row.get("input_path") and Path(row["input_path"]).is_file():
            store.set_track(row["id"], status="queued", progress=0)
            start_track(row["job_id"], row["id"])
        else:
            store.set_track(row["id"], status="failed", progress=0, error="Uploaded audio is missing.")
    storage_reservations.clear()
    now = time.time()
    for old in store.old_jobs(now - settings.retention_days * 86400):
        shutil.rmtree(settings.data_dir / "jobs" / old["id"], ignore_errors=True)
        store.delete_job(old["id"])
    cleaner = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleaner.cancel()
        await asyncio.gather(cleaner, return_exceptions=True)
        tasks = [*track_tasks.values(), *source_search_tasks.values(), *source_download_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if store:
            store.close()
        store = None
        await spotify.close()
        await archive_source.close()


app = FastAPI(title="Spotify Playlist Audio", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://i.scdn.co; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response


@app.get("/health")
async def health():
    return {"status": "ok", "spotify_configured": spotify.enabled}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    session_id = request.cookies.get("spotify_session")
    return {"configured": spotify.oauth_enabled, "connected": bool(session_id and session_id in spotify.sessions)}


@app.get("/api/auth/login")
async def auth_login(request: Request):
    throttle(request, "auth", 10)
    try:
        login_url, _ = spotify.login_url()
    except SpotifyError as exc:
        raise HTTPException(503, str(exc)) from exc
    return RedirectResponse(login_url, status_code=303)


@app.get("/api/auth/callback")
async def auth_callback(code: str | None = None, state: str | None = None):
    if not code or not state:
        raise HTTPException(400, "Spotify sign-in was cancelled.")
    try:
        session_id = await spotify.finish_login(code, state)
    except SpotifyError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = RedirectResponse(settings.public_base_url + "/" if settings.public_base_url else "/", status_code=303)
    response.set_cookie(
        "spotify_session", session_id, httponly=True,
        secure=not settings.spotify_redirect_uri.startswith(("http://localhost", "http://127.0.0.1")),
        samesite="lax", max_age=30 * 86400, path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    session_id = request.cookies.get("spotify_session")
    await spotify.logout(session_id)
    response = JSONResponse({"connected": False})
    response.delete_cookie("spotify_session", path="/")
    return response


def cache_analysis(result: dict) -> dict:
    if not result.get("tracks"):
        raise HTTPException(422, "No tracks were found in this playlist.")
    analysis_id = secrets.token_urlsafe(32)
    result = {
        **result,
        "tracks": [{**track, "index": i} for i, track in enumerate(result["tracks"], 1)],
    }
    now = time.time()
    for key, (created, _) in list(analyses.items()):
        if now - created > 3600:
            analyses.pop(key, None)
    analyses[analysis_id] = (now, result)
    if len(analyses) > 2000:
        for stale_id in list(analyses)[:len(analyses) - 2000]:
            analyses.pop(stale_id, None)
    return {"analysis_id": analysis_id, **result}


@app.post("/api/analyze")
async def analyze(body: AnalyzeRequest, request: Request):
    throttle(request, "analyze", 20)
    session_id = request.cookies.get("spotify_session")
    try:
        result = await spotify.resolve(body.url, session_id, settings.max_tracks)
    except SpotifyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result["tracks"]:
        raise HTTPException(422, "This Spotify item has no available tracks.")
    return cache_analysis(result)


@app.post("/api/analyze/csv")
async def analyze_csv(request: Request, file: UploadFile = File(...)):
    throttle(request, "analyze", 20)
    if Path(file.filename or "").suffix.lower() != ".csv":
        await file.close()
        raise HTTPException(415, "Choose a .csv playlist export.")
    try:
        raw = await file.read(2 * 1024 * 1024 + 1)
    finally:
        await file.close()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "The playlist CSV must be no larger than 2 MiB.")
    try:
        result = parse_playlist_csv(raw, file.filename or "", settings.max_tracks)
    except PlaylistImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    return cache_analysis(result)


@app.post("/api/jobs")
async def create_job(body: CreateJobRequest, request: Request):
    throttle(request, "create", 10)
    cached = analyses.pop(body.analysis_id, None)
    if not cached or time.time() - cached[0] > 3600:
        raise HTTPException(400, "The preview expired. Analyze the Spotify link again.")
    result = cached[1]
    selected = set(body.selected_indices)
    if len(selected) != len(body.selected_indices):
        raise HTTPException(422, "Remove duplicate track selections.")
    source_tracks = [t for t in result["tracks"] if t["index"] in selected]
    if len(source_tracks) != len(selected) or len(source_tracks) > settings.max_tracks:
        raise HTTPException(422, "The selection contains an invalid track.")
    pending_tracks = 0
    pending_track_keys = set()
    for track in source_tracks:
        identity = track_identity(track)
        if identity in pending_track_keys:
            continue
        pending_track_keys.add(identity)
        pending_tracks += 1
    active_source_tasks = sum(not task.done() for task in source_search_tasks.values()) + sum(
        not task.done() for task in source_download_tasks.values()
    )
    if active_source_tasks + pending_tracks > settings.max_pending_source_tasks:
        raise HTTPException(503, "The source search queue is busy. Try this job again shortly.")
    job_id = uuid.uuid4().hex
    job_token = secrets.token_urlsafe(32)
    rows = []
    seen: dict[str, str] = {}
    for track in source_tracks:
        track_id = uuid.uuid4().hex
        duplicate_key = track_identity(track)
        duplicate_of = seen.get(duplicate_key)
        if not duplicate_of:
            seen[duplicate_key] = track_id
        rows.append({
            **track, "id": track_id, "position": track["position"],
            "status": "duplicate" if duplicate_of else "searching",
            "duplicate_of": duplicate_of,
        })
    job = {
        "id": job_id, "name": result["name"], "artwork": result["artwork"], "kind": result["kind"],
        "format": body.format, "bitrate": body.bitrate if body.format == "mp3" else None,
        "created_at": time.time(),
    }
    database().create_job(job, job_token, rows)
    (settings.data_dir / "jobs" / job_id / "input").mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row["status"] == "searching":
            start_source_search(job_id, row["id"])
    return {"token": job_token, "job": job_view(database().job(job_id), job_token)}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, x_job_token: str | None = Header(default=None)):
    job = require_job(job_id, x_job_token)
    return job_view(job, x_job_token)


@app.put("/api/jobs/{job_id}/tracks/{track_id}/audio")
async def upload_audio(
    job_id: str, track_id: str, file: UploadFile = File(...),
    x_job_token: str | None = Header(default=None),
):
    job = require_job(job_id, x_job_token)
    track = database().track(job_id, track_id)
    if not track:
        raise HTTPException(404, "Track not found.")
    if track["status"] not in {"awaiting_upload", "needs_review", "failed"}:
        raise HTTPException(409, "This track cannot accept another upload in its current state.")
    previous_status = track["status"]
    previous_path = track.get("input_path")
    database().set_track(track_id, status="uploading")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_INPUT_EXTENSIONS:
        database().set_track(track_id, status=previous_status)
        await file.close()
        raise HTTPException(415, "Choose an audio file such as MP3, M4A, FLAC, WAV, AAC, OGG, or Opus.")
    job_root = settings.data_dir / "jobs" / job_id
    input_dir = job_root / "input"
    existing_bytes = stored_bytes(job_root)
    total_storage_at_start = stored_bytes(settings.data_dir)
    declared_size = min(file.size if file.size is not None else settings.max_upload_bytes, settings.max_upload_bytes)
    if total_storage_at_start + storage_reservations["__global__"] + declared_size > settings.max_total_storage_bytes:
        database().set_track(track_id, status=previous_status)
        await file.close()
        raise HTTPException(413, "The server has reached its temporary storage limit.")
    old_input_bytes = Path(previous_path).stat().st_size if previous_path and Path(previous_path).is_file() else 0
    existing_bytes = max(0, existing_bytes - old_input_bytes)
    if existing_bytes + storage_reservations[job_id] >= settings.max_uploads_per_job_bytes:
        database().set_track(track_id, status=previous_status)
        await file.close()
        raise HTTPException(413, "This job has reached its temporary storage limit.")
    destination = input_dir / f"{track_id}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".uploading")
    total = 0
    try:
        async with aiofiles.open(temporary, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if (total > settings.max_upload_bytes or existing_bytes + total + storage_reservations[job_id] > settings.max_uploads_per_job_bytes
                        or total_storage_at_start + total + storage_reservations["__global__"] > settings.max_total_storage_bytes
                        or total > shutil.disk_usage(settings.data_dir).free - storage_reservations["__global__"]):
                    raise HTTPException(413, "The audio file exceeds the upload limit.")
                await output.write(chunk)
        if total == 0:
            raise HTTPException(400, "The uploaded file is empty.")
        await probe_audio(temporary)
        current = database().track(job_id, track_id)
        if not current or current["status"] != "uploading":
            raise HTTPException(409, "This upload was cancelled.")
        os.replace(temporary, destination)
    except HTTPException:
        temporary.unlink(missing_ok=True)
        current = database().track(job_id, track_id)
        if current and current["status"] == "uploading":
            database().set_track(track_id, status=previous_status)
        raise
    except AudioError as exc:
        temporary.unlink(missing_ok=True)
        current = database().track(job_id, track_id)
        if current and current["status"] == "uploading":
            database().set_track(track_id, status=previous_status)
        raise HTTPException(415, str(exc)) from exc
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        current = database().track(job_id, track_id)
        if current and current["status"] == "uploading":
            database().set_track(track_id, status=previous_status)
        logger.info("upload failed job=%s track=%s reason=%s", job_id, track_id, type(exc).__name__)
        raise HTTPException(500, "Audio upload could not be saved.") from exc
    finally:
        await file.close()
    if previous_path and Path(previous_path) != destination:
        Path(previous_path).unlink(missing_ok=True)
    database().set_track(
        track_id, input_path=str(destination), status="queued", progress=0, error=None,
        source_candidates="[]", source_name=None, source_url=None, source_license=None,
        source_license_url=None, source_attribution=None, source_confidence=None, source_auto=None,
    )
    database().set_job_status(job_id, "processing")
    start_track(job_id, track_id)
    return {"accepted": True, "track_id": track_id, "job": job_view(job, x_job_token)}


@app.post("/api/jobs/{job_id}/tracks/{track_id}/sources/search")
async def search_track_sources(
    job_id: str, track_id: str, request: Request,
    x_job_token: str | None = Header(default=None),
):
    require_job(job_id, x_job_token)
    throttle(request, "source-search", 30)
    track = database().track(job_id, track_id)
    if not track:
        raise HTTPException(404, "Track not found.")
    if track["status"] not in {"awaiting_upload", "needs_review", "failed"}:
        raise HTTPException(409, "This track cannot be searched in its current state.")
    active_source_tasks = sum(not task.done() for task in source_search_tasks.values()) + sum(
        not task.done() for task in source_download_tasks.values()
    )
    if active_source_tasks >= settings.max_pending_source_tasks:
        raise HTTPException(503, "The source search queue is busy. Try again shortly.")
    database().set_track(
        track_id, status="searching", progress=0, error=None, source_candidates="[]"
    )
    start_source_search(job_id, track_id)
    return job_view(database().job(job_id), x_job_token)


class SelectSourceRequest(BaseModel):
    candidate_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


@app.post("/api/jobs/{job_id}/tracks/{track_id}/source")
async def select_track_source(
    job_id: str, track_id: str, body: SelectSourceRequest,
    x_job_token: str | None = Header(default=None),
):
    require_job(job_id, x_job_token)
    track = database().track(job_id, track_id)
    if not track:
        raise HTTPException(404, "Track not found.")
    if track["status"] not in {"awaiting_upload", "needs_review", "failed"}:
        raise HTTPException(409, "This track cannot accept a source selection in its current state.")
    active_source_tasks = sum(not task.done() for task in source_search_tasks.values()) + sum(
        not task.done() for task in source_download_tasks.values()
    )
    if active_source_tasks >= settings.max_pending_source_tasks:
        raise HTTPException(503, "The source download queue is busy. Try again shortly.")
    candidate = next(
        (item for item in _track_candidates(track) if item.get("id") == body.candidate_id),
        None,
    )
    if not candidate:
        raise HTTPException(404, "That source candidate is no longer available. Search again.")
    start_candidate_download(job_id, track_id, body.candidate_id, automatic=False)
    return job_view(database().job(job_id), x_job_token)


@app.post("/api/jobs/{job_id}/retry")
async def retry_failed(job_id: str, x_job_token: str | None = Header(default=None)):
    require_job(job_id, x_job_token)
    rows = database().retry_failed(job_id)
    for track in rows:
        start_track(job_id, track["id"])
    for track in database().tracks(job_id):
        if track["status"] == "failed" and not track.get("input_path"):
            database().set_track(track["id"], status="searching", progress=0, error=None)
            start_source_search(job_id, track["id"])
    await refresh_job(job_id)
    return job_view(database().job(job_id), x_job_token)


@app.delete("/api/jobs/{job_id}/tracks/{track_id}")
async def cancel_track(job_id: str, track_id: str, x_job_token: str | None = Header(default=None)):
    require_job(job_id, x_job_token)
    track = database().track(job_id, track_id)
    if not track:
        raise HTTPException(404, "Track not found.")
    if track["status"] in {"queued", "awaiting_upload", "processing", "uploading", "searching", "downloading", "needs_review"}:
        database().set_track(track_id, status="cancelled", progress=0)
        for tasks in (track_tasks, source_search_tasks, source_download_tasks):
            task = tasks.get(track_id)
            if task and not task.done():
                task.cancel()
    await refresh_job(job_id)
    return job_view(database().job(job_id), x_job_token)


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str, x_job_token: str | None = Header(default=None)):
    require_job(job_id, x_job_token)
    for track in database().tracks(job_id):
        if track["status"] in {"queued", "awaiting_upload", "processing", "uploading", "searching", "downloading", "needs_review"}:
            database().set_track(track["id"], status="cancelled", progress=0)
            for tasks in (track_tasks, source_search_tasks, source_download_tasks):
                task = tasks.get(track["id"])
                if task and not task.done():
                    task.cancel()
    await refresh_job(job_id)
    return job_view(database().job(job_id), x_job_token)


@app.get("/api/jobs/{job_id}/tracks/{track_id}/download")
async def download_track(
    job_id: str, track_id: str, token: str | None = Query(default=None),
    x_job_token: str | None = Header(default=None),
):
    require_job(job_id, x_job_token or token)
    rows = mark_missing_outputs(job_id, database().tracks(job_id))
    path = resolve_output(job_id, track_id, rows)
    if not path:
        raise HTTPException(409, "This track has no verified audio file ready to download.")
    track = database().track(job_id, track_id)
    filename = f"{track['position']:02d} - {safe_filename(track['artist'])} - {safe_filename(track['title'])}{path.suffix}"
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@app.get("/api/jobs/{job_id}/archive.zip")
async def download_archive(job_id: str, token: str = Query(...)):
    job = require_job(job_id, token)
    rows = mark_missing_outputs(job_id, database().tracks(job_id))
    output_paths = [resolve_output(job_id, t["id"], rows) for t in rows]
    if not any(output_paths):
        raise HTTPException(409, "No completed tracks are ready to export.")
    lock = archive_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        cover = await fetch_artwork(job.get("artwork"))
        root = settings.data_dir / "jobs" / job_id
        used = stored_bytes(root)
        old_size = (root / "playlist.zip").stat().st_size if (root / "playlist.zip").is_file() else 0
        archive_size = sum(path.stat().st_size for path in output_paths if path) + 1024 * (len(rows) + 2) + (len(cover[0]) if cover else 0)
        if storage_lock is None:
            raise HTTPException(503, "Storage manager is starting.")
        async with storage_lock:
            pending = storage_reservations[job_id]
            global_pending = storage_reservations["__global__"]
            free = shutil.disk_usage(settings.data_dir).free
            if (used - old_size + pending + archive_size > settings.max_uploads_per_job_bytes
                    or stored_bytes(settings.data_dir) + global_pending + archive_size > settings.max_total_storage_bytes
                    or archive_size > free - global_pending):
                raise HTTPException(413, "There is not enough temporary storage to build this archive.")
            storage_reservations[job_id] += archive_size
            storage_reservations["__global__"] += archive_size
        try:
            path = await asyncio.to_thread(build_archive, job_id, job, rows, cover)
        except (OSError, zipfile.BadZipFile, AudioError):
            logger.exception("archive build failed job=%s", job_id)
            raise HTTPException(500, "The archive could not be prepared because an audio file failed verification.")
        finally:
            async with storage_lock:
                storage_reservations[job_id] = max(0, storage_reservations[job_id] - archive_size)
                storage_reservations["__global__"] = max(0, storage_reservations["__global__"] - archive_size)
    filename = safe_filename(job["name"]) + ".zip"
    return FileResponse(path, filename=filename, media_type="application/zip")


@app.get("/api/jobs/{job_id}/tracks/{track_id}/download-info")
async def download_info(job_id: str, track_id: str, x_job_token: str | None = Header(default=None)):
    require_job(job_id, x_job_token)
    path = resolve_output(job_id, track_id)
    track = database().track(job_id, track_id)
    if not path or not track:
        raise HTTPException(409, "This track is not ready to download.")
    return {"source_bitrate": track["source_bitrate"], "lossless_from_source": bool(track["lossless_from_source"]),
            "format": path.suffix[1:]}


async def fetch_artwork(url: str | None) -> tuple[bytes, str] | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "i.scdn.co" or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type)
                if not extension:
                    return None
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes(65536):
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                if extension == ".jpg" and not data.startswith(b"\xff\xd8"):
                    return None
                if extension == ".png" and not data.startswith(b"\x89PNG"):
                    return None
                if extension == ".webp" and not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
                    return None
                return data, extension
    except httpx.HTTPError:
        return None


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "frontend", html=True), name="frontend")

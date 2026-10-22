import asyncio
import json
import shutil
import subprocess
from pathlib import Path
import zipfile

import httpx

import pytest
from fastapi.testclient import TestClient

from backend.audio import AudioError, convert_audio, output_options, probe_audio, safe_filename
from backend.spotify import API_ROOT, SpotifyClient, SpotifyError, normalize_track, parse_spotify_url
from backend.sources import SuppliedAudio, UserUploadSource
from backend.store import Store


def test_parse_spotify_links_and_uris():
    assert parse_spotify_url("https://open.spotify.com/track/0123456789012345678901?si=abc") == (
        "track", "0123456789012345678901"
    )
    assert parse_spotify_url("https://open.spotify.com/intl-en/playlist/0123456789012345678901") == (
        "playlist", "0123456789012345678901"
    )
    assert parse_spotify_url("spotify:album:0123456789012345678901") == (
        "album", "0123456789012345678901"
    )


@pytest.mark.parametrize("value", [
    "https://evil.example/playlist/0123456789012345678901",
    "https://open.spotify.com.evil.example/track/0123456789012345678901",
    "file:///etc/passwd",
    "spotify:artist:0123456789012345678901",
    "https://open.spotify.com/track/not-an-id",
])
def test_parse_rejects_unsupported_and_hostile_urls(value):
    with pytest.raises(SpotifyError):
        parse_spotify_url(value)


def test_track_metadata_normalization():
    normalized = normalize_track({
        "id": "0123456789012345678901", "name": "A song", "duration_ms": 201000,
        "artists": [{"name": "Lead"}, {"name": "Guest"}],
        "album": {"name": "Record", "images": [{"url": "https://i.scdn.co/image"}]},
        "external_ids": {"isrc": "USABC123"}, "explicit": True, "track_number": 3,
    }, 7)
    assert normalized["artist"] == "Lead, Guest"
    assert normalized["album"] == "Record"
    assert normalized["position"] == 7
    assert normalized["isrc"] == "USABC123"
    assert normalized["explicit"] is True


def test_audio_options_and_safe_unicode_filenames():
    assert output_options("mp3", "192") == (["-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "3"], ".mp3")
    assert output_options("original", None) == ([], "")
    assert output_options("flac", None)[1] == ".flac"
    with pytest.raises(AudioError):
        output_options("not-a-format", None)
    name = safe_filename("../../été: live?.mp3")
    assert "/" not in name and "\\" not in name and not name.startswith("..")


def test_store_authorization_retries_and_recovery(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    job = {"id": "job", "name": "Set", "kind": "playlist", "format": "mp3",
           "created_at": 1, "artwork": None, "bitrate": "320"}
    track = {"id": "track", "position": 1, "title": "Song", "artist": "Artist",
             "album": "Album", "status": "awaiting_upload", "spotify_id": "id"}
    store.create_job(job, "secret", [track])
    assert store.authorized_job("job", "secret")["name"] == "Set"
    assert store.authorized_job("job", "wrong") is None
    store.set_track("track", input_path="/tmp/song.mp3", status="failed", error="broken")
    assert store.retry_failed("job")[0]["status"] == "queued"
    assert store.recoverable()[0]["id"] == "track"
    store.close()


def test_archive_is_ordered_and_contains_manifest(tmp_path, monkeypatch):
    from backend import main
    data_root = tmp_path / "data"
    monkeypatch.setattr(main.settings, "data_dir", data_root)
    local_store = Store(data_root / "jobs.sqlite3")
    monkeypatch.setattr(main, "store", local_store)
    job_id = "job"
    audio_dir = data_root / "jobs" / job_id / "audio"
    audio_dir.mkdir(parents=True)
    source = audio_dir / "first.mp3"
    source.write_bytes(b"synthetic test audio bytes")
    job = {"id": job_id, "name": "../Trip: set", "kind": "playlist", "output_format": "mp3",
           "created_at": 10}
    tracks = [
        {"id": "first", "position": 1, "spotify_id": "same", "title": "First", "artist": "Artist",
         "album": "Album", "duration_ms": 30000, "isrc": None, "status": "completed",
         "output_path": str(source), "source_bitrate": 192000, "lossless_from_source": 0, "error": None},
        {"id": "copy", "position": 2, "spotify_id": "same", "title": "First", "artist": "Artist",
         "album": "Album", "duration_ms": 30000, "isrc": None, "status": "duplicate",
         "duplicate_of": "first", "output_path": None, "source_bitrate": None,
         "lossless_from_source": None, "error": None},
    ]
    local_store.create_job(job, "secret", tracks)
    local_store.set_track("first", output_path=str(source), status="completed", source_bitrate=192000,
                          lossless_from_source=0)
    archive = main.build_archive(job_id, job, local_store.tracks(job_id))
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert names[0].endswith("01 - Artist - First.mp3")
        assert names[1].endswith("02 - Artist - First.mp3")
        manifest = json.loads(zf.read(next(name for name in names if name.endswith("manifest.json"))))
        assert len(manifest["tracks"]) == 2
        playlist = zf.read(next(name for name in names if name.endswith("playlist.m3u8"))).decode()
        assert "#EXTM3U" in playlist and "02 - Artist - First.mp3" in playlist
    local_store.close()


def test_api_rejects_invalid_spotify_link_without_network(tmp_path, monkeypatch):
    from backend import main
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "api-data")
    with TestClient(main.app) as client:
        response = client.post("/api/analyze", json={"url": "https://not-spotify.example/track/abc"})
        assert response.status_code == 400
        assert "Spotify" in response.json()["detail"]


def test_spotify_pagination_keeps_order_and_rejects_truncation():
    def handler(request):
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={
                "items": [{"id": "a"}, {"id": "b"}],
                "next": API_ROOT + "/playlists/list/tracks?limit=2&offset=2",
            })
        return httpx.Response(200, json={"items": [{"id": "c"}, {"id": "d"}], "next": None})

    async def exercise():
        client = SpotifyClient("", "", "")
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        page = await client._pages(API_ROOT + "/playlists/list/tracks?limit=2", "token", 4)
        assert [item["id"] for item in page] == ["a", "b", "c", "d"]
        with pytest.raises(SpotifyError):
            await client._pages(API_ROOT + "/playlists/list/tracks?limit=2", "token", 3)
        await client.close()

    asyncio.run(exercise())


def test_user_upload_source_returns_only_the_selected_file(tmp_path):
    audio = tmp_path / "owned.mp3"
    audio.write_bytes(b"synthetic")
    source = UserUploadSource({"track": SuppliedAudio(audio, audio.name)})
    result = asyncio.run(source.acquire("track"))
    assert result.path == audio
    with pytest.raises(LookupError):
        asyncio.run(source.acquire("other"))


def test_frontend_contains_mobile_upload_and_job_restore_flow():
    root = Path(__file__).resolve().parents[1] / "frontend"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    assert 'id="spotify-url"' in html
    assert 'id="format-select"' in html
    assert 'localStorage.setItem("soundloom-job"' in js
    assert "/audio" in js and "/archive.zip" in js and "/retry" in js


def test_synthetic_wav_is_encoded_and_verified(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for the synthetic conversion integration test.")
    source = tmp_path / "tone.wav"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", str(source),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    progress = []

    async def run():
        async def on_progress(value):
            progress.append(value)
        return await convert_audio(
            source, tmp_path / "converted" / "tone", "mp3", "128",
            {"title": "Synthetic tone", "artist": "Test", "album": "Fixture"},
            30, on_progress,
        )

    result = asyncio.run(run())
    output = Path(result["output_path"])
    assert output.is_file() and output.suffix == ".mp3"
    inspected = asyncio.run(probe_audio(output))
    assert inspected["codec"] == "mp3"
    assert 0.9 <= inspected["duration"] <= 1.2
    assert result["lossless_from_source"] is False
    assert progress[-1] == 100

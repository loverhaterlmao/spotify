import asyncio
import json
import shutil
import subprocess
from pathlib import Path
import zipfile

import httpx

import pytest
from fastapi.testclient import TestClient

import backend.audio as audio
from backend.audio import AudioError, convert_audio, output_options, probe_audio, safe_filename
from backend.archive_source import ArchiveSourceError, InternetArchiveSource, supported_license
from backend.matching import rank_candidates, score_candidate
from backend.spotify import API_ROOT, SpotifyClient, SpotifyError, normalize_track, parse_spotify_url
from backend.sources import SuppliedAudio, UserUploadSource
from backend.playlist_import import PlaylistImportError, parse_playlist_csv
from backend.store import Store
from backend.spotify import SpotifyError

from backend.spotify_embed import parse_embed_html
from backend.youtube_source import YouTubeSearchSource


def test_public_spotify_embed_parses_playlist_rows_without_api_credentials():
    html = """
    <title>Mix name | Spotify</title>
    <link rel="preload" as="image" href="https://mosaic.scdn.co/300/cover">
    <h1 data-testid="entity-title">Mix name</h1>
    <ol><li data-testid="tracklist-row-0">
      <h3>Song One</h3><h4>Artist One</h4>
      <div data-testid="duration-cell">03:01</div>
    </li></ol>
    """
    result = parse_embed_html("playlist", "0123456789012345678901", html)
    assert result["name"] == "Mix name"
    assert result["artwork"] == "https://mosaic.scdn.co/300/cover"
    assert result["tracks"][0]["title"] == "Song One"
    assert result["tracks"][0]["artist"] == "Artist One"
    assert result["tracks"][0]["duration_ms"] == 181000
    assert result["tracks"][0]["spotify_id"] is None



def test_public_embed_next_data_preserves_spotify_ids_and_explicit_flag():
    payload = {
        "props": {"pageProps": {"state": {"data": {"entity": {
            "name": "Public Mix",
            "coverArt": {"sources": [{"url": "https://i.scdn.co/image/cover"}]},
            "trackList": [{
                "uri": "spotify:track:0123456789012345678901",
                "title": "One",
                "subtitle": "Artist",
                "duration": 123000,
                "isExplicit": True,
                "entityType": "track",
            }],
        }}}}},
    }
    html = (
        '<h1 data-testid="entity-title">Public Mix</h1>'
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + '</script>'
    )
    result = parse_embed_html("playlist", "1123456789012345678901", html)
    assert result["tracks"][0]["spotify_id"] == "0123456789012345678901"
    assert result["tracks"][0]["explicit"] is True
    assert result["artwork"] == "https://i.scdn.co/image/cover"

def test_public_spotify_embed_enforces_track_cap():
    html = """
    <h1 data-testid="entity-title">Mix</h1>
    <li data-testid="tracklist-row-0"><h3>One</h3><h4>A</h4></li>
    <li data-testid="tracklist-row-1"><h3>Two</h3><h4>B</h4></li>
    """
    with pytest.raises(SpotifyError, match="track limit"):
        parse_embed_html("playlist", "0123456789012345678901", html, max_tracks=1)


def test_public_spotify_embed_accepts_more_than_thirty_tracks():
    rows = "".join(
        f'<li data-testid="tracklist-row-{index}"><h3>Song {index}</h3><h4>Artist</h4></li>'
        for index in range(31)
    )
    result = parse_embed_html(
        "playlist", "0123456789012345678901",
        f'<h1 data-testid="entity-title">Mix</h1>{rows}',
        max_tracks=100,
    )
    assert len(result["tracks"]) == 31
    assert result["tracks"][-1]["title"] == "Song 30"


def test_youtube_source_requires_explicit_rights_and_channel_allowlist():
    source = YouTubeSearchSource(
        enabled=True,
        rights_acknowledged=False,
        licensed_channel_ids={"UC0123456789012345678901"},
        license_name="Music license",
        license_url="https://example.com/license",
    )
    assert not source.enabled




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


def test_playlist_csv_import_normalizes_tunemymusic_export():
    data = (
        "Track Name,Artist Name(s),Album Name,Duration (ms),Track URI,Track Number,ISRC,Explicit\n"
        'See You Again (feat. Kali Uchis),"Tyler, The Creator; Kali Uchis",Flower Boy,180386,'
        "spotify:track:7KA4W4McWYRpgf0fWsJZWB,10,USQX91701234,false\n"
    ).encode()
    result = parse_playlist_csv(data, "my-playlist.csv", 300)
    track = result["tracks"][0]
    assert result["name"] == "my-playlist"
    assert result["kind"] == "playlist"
    assert track["title"] == "See You Again (feat. Kali Uchis)"
    assert track["artist"] == "Tyler, The Creator; Kali Uchis"
    assert track["album"] == "Flower Boy"
    assert track["duration_ms"] == 180386
    assert track["spotify_id"] == "7KA4W4McWYRpgf0fWsJZWB"
    assert track["position"] == 1


def test_playlist_csv_import_rejects_missing_columns_and_track_limit():
    with pytest.raises(PlaylistImportError, match="title and artist"):
        parse_playlist_csv(b"Track Name,Duration\nSong,123000\n", "x.csv", 10)
    data = b"Track Name,Artist\nOne,Artist\nTwo,Artist\n"
    with pytest.raises(PlaylistImportError, match="track limit"):
        parse_playlist_csv(data, "x.csv", 1)


def test_audio_options_and_safe_unicode_filenames():
    assert output_options("mp3", "192") == (["-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "3"], ".mp3")
    assert output_options("original", None) == ([], "")
    assert output_options("flac", None)[1] == ".flac"
    assert output_options("aac", None)[1] == ".m4a"
    assert output_options("alac", None)[1] == ".m4a"
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
    local_store.set_track(
        "first", output_path=str(source), status="completed", source_bitrate=192000,
        lossless_from_source=0, source_name="internet-archive", source_url="https://archive.org/details/item",
        source_license="CC BY 4.0", source_license_url="https://creativecommons.org/licenses/by/4.0/",
        source_attribution="Artist", source_confidence=0.97, source_auto=1,
    )
    archive = main.build_archive(job_id, job, local_store.tracks(job_id))
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert names[0].endswith("01 - Artist - First.mp3")
        assert names[1].endswith("02 - Artist - First.mp3")
        manifest = json.loads(zf.read(next(name for name in names if name.endswith("manifest.json"))))
        assert len(manifest["tracks"]) == 2
        assert manifest["tracks"][0]["source"]["license"] == "CC BY 4.0"
        assert any(name.endswith("SOURCES.txt") for name in names)
        playlist = zf.read(next(name for name in names if name.endswith("playlist.m3u8"))).decode()
        assert "#EXTM3U" in playlist and "02 - Artist - First.mp3" in playlist
    source.write_bytes(b"")
    assert main.resolve_output(job_id, "first") is None
    with pytest.raises(AudioError, match="empty or damaged audio"):
        main.build_archive(job_id, job, local_store.tracks(job_id))
    assert local_store.track(job_id, "first")["status"] == "failed"
    local_store.close()


def test_csv_analysis_endpoint_works_without_spotify_credentials(tmp_path, monkeypatch):
    from backend import main
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "csv-api-data")
    data = b"""Track Name,Artist Name(s),Album Name
Example Song,Example Artist,Example Album
"""
    with TestClient(main.app) as client:
        response = client.post(
            "/api/analyze/csv",
            files={"file": ("playlist.csv", data, "text/csv")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "playlist"
        assert payload["tracks"][0]["title"] == "Example Song"
        assert payload["tracks"][0]["artist"] == "Example Artist"


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
    assert 'id="playlist-csv"' in html and "/api/analyze/csv" in js
    assert 'id="format-select"' in html
    assert 'id="terms-gate"' in html
    assert 'id="terms-accept-checkbox"' in html
    assert "/api/terms/accept" in js
    assert "terms_accepted" not in js
    assert 'localStorage.setItem("soundloom-job"' in js
    assert "/audio" in js and "/archive.zip" in js and "/retry" in js
    assert ".mp3,.m4a,.mp4,.aac,.flac,.wav,.ogg,.opus" in js
    assert "FLAC" in html and "MP3" in html and "M4A" in html




def test_empty_and_silent_audio_are_rejected(tmp_path):
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    with pytest.raises(AudioError, match="empty"):
        asyncio.run(probe_audio(empty))

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for the silent-audio validation test.")
    silence = tmp_path / "silence.wav"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo", "-t", "1", "-c:a", "pcm_s16le", str(silence),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with pytest.raises(AudioError, match="silent"):
        asyncio.run(probe_audio(silence, verify_signal=True))


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
            source_info=await probe_audio(source),
        )

    result = asyncio.run(run())
    output = Path(result["output_path"])
    assert output.is_file() and output.suffix == ".mp3"
    inspected = asyncio.run(probe_audio(output, verify_signal=True))
    assert inspected["codec"] == "mp3"
    assert 0.9 <= inspected["duration"] <= 1.2
    assert result["lossless_from_source"] is False
    assert progress[-1] == 100


def test_license_allowlist_rejects_unlicensed_and_restrictive_records():
    assert supported_license("https://creativecommons.org/licenses/by/4.0/")[0] == "CC BY 4.0"
    assert supported_license("http://creativecommons.org/publicdomain/zero/1.0/")[0] == "CC0 1.0"
    assert supported_license("https://creativecommons.org/licenses/by-nc/4.0/") is None
    assert supported_license("https://example.org/license") is None


def test_matching_requires_strong_metadata_and_blocks_version_substitution():
    track = {
        "title": "Sample Song", "artist": "The Example", "album": "One Record",
        "duration_ms": 180000, "isrc": "USABC1234567",
    }
    exact = {
        "title": "Sample Song", "artist": "The Example", "album": "One Record",
        "duration_seconds": 180.2, "isrc": "USABC1234567", "size": 1000,
    }
    result = score_candidate(track, exact)
    assert result["auto_match"] is True
    assert result["match_score"] >= 94
    remix = score_candidate(track, {**exact, "title": "Sample Song Remix"})
    assert remix["auto_match"] is False
    mismatch = score_candidate(track, {**exact, "isrc": "USXYZ9999999"})
    assert mismatch["auto_match"] is False


def test_internet_archive_search_filters_licenses_and_returns_audio_candidates():
    calls = []

    def handler(request):
        calls.append(request.url)
        if request.url.path == "/advancedsearch.php":
            return httpx.Response(200, json={"response": {"docs": [
                {"identifier": "licensed-item", "licenseurl": "http://creativecommons.org/licenses/by/4.0/"},
                {"identifier": "unlicensed-item", "licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/"},
            ]}})
        identifier = request.url.path.rsplit("/", 1)[-1]
        metadata = {
            "licensed-item": {
                "title": "Sample Song", "artist": "The Example", "album": "One Record",
                "isrc": "USABC1234567", "licenseurl": "http://creativecommons.org/licenses/by/4.0/",
            },
            "unlicensed-item": {
                "title": "Sample Song", "artist": "The Example", "licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/",
            },
        }[identifier]
        return httpx.Response(200, json={
            "metadata": metadata,
            "files": [{"name": "Sample Song.mp3", "size": "1000", "length": "180.2", "format": "MP3"}],
        })

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        source = InternetArchiveSource(client)
        try:
            candidates = await source.search({
                "title": "Sample Song", "artist": "The Example", "album": "One Record",
                "duration_ms": 180000, "isrc": "USABC1234567",
            }, max_file_bytes=2000)
            ranked = rank_candidates({
                "title": "Sample Song", "artist": "The Example", "album": "One Record",
                "duration_ms": 180000, "isrc": "USABC1234567",
            }, candidates)
            assert len(candidates) == 1
            assert candidates[0]["license_name"] == "CC BY 4.0"
            assert ranked[0]["auto_match"] is True
            assert sum(url.path.startswith("/metadata/") for url in calls) == 1
            assert all(url.host == "archive.org" for url in calls)
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_internet_archive_download_blocks_redirect_outside_allowlist(tmp_path):
    metadata = {
        "title": "Sample Song", "artist": "The Example",
        "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
    }

    def handler(request):
        if request.url.path.startswith("/metadata/"):
            return httpx.Response(200, json={
                "metadata": metadata,
                "files": [{"name": "song.mp3", "size": "4", "format": "MP3"}],
            })
        return httpx.Response(302, headers={"Location": "https://evil.example/audio.mp3"})

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        source = InternetArchiveSource(client)
        candidate = {
            "identifier": "licensed-item", "file_name": "song.mp3",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        }
        async def on_progress(_value):
            return None
        try:
            with pytest.raises(ArchiveSourceError, match="redirect was blocked"):
                await source.download(candidate, tmp_path / "song.mp3", 100, on_progress)
            assert not (tmp_path / "song.mp3").exists()
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_job_creation_starts_authorized_source_search(tmp_path, monkeypatch):
    from backend import main
    import time
    monkeypatch.setattr(main.settings, "data_dir", tmp_path / "api-data")
    started = []
    monkeypatch.setattr(main, "start_source_search", lambda job_id, track_id: started.append((job_id, track_id)))
    analysis_id = "a" * 40
    main.analyses[analysis_id] = (time.time(), {
        "name": "Example playlist", "artwork": None, "kind": "playlist",
        "tracks": [{
            "index": 1, "position": 1, "spotify_id": "0123456789012345678901",
            "title": "Sample Song", "artist": "The Example", "album": "One Record",
            "duration_ms": 180000, "track_number": 1, "disc_number": 1, "isrc": "USABC1234567",
        }],
    })
    with TestClient(main.app) as client:
        request = {
            "analysis_id": analysis_id, "selected_indices": [1],
            "format": "mp3", "bitrate": "192",
        }
        response = client.get("/api/terms/status")
        assert response.status_code == 200
        assert response.json()["accepted"] is False
        response = client.post("/api/jobs", json=request)
        assert response.status_code == 403
        assert "terms of use" in response.json()["detail"]
        accepted = client.post("/api/terms/accept", json={"accepted": True})
        assert accepted.status_code == 200
        assert accepted.cookies.get("soundloom_terms")
        stored_acceptance = main.store.terms_acceptance(client.cookies.get("soundloom_terms"))
        assert stored_acceptance["terms_version"] == main.TERMS_VERSION
        response = client.get("/api/terms/status")
        assert response.json()["accepted"] is True
        response = client.post("/api/jobs", json=request)
        assert response.status_code == 200
        payload = response.json()
        assert payload["job"]["tracks"][0]["status"] == "searching"
        job_id = payload["job"]["id"]
        track_id = payload["job"]["tracks"][0]["id"]
        stored_job = main.store.job(job_id)
        assert stored_job["terms_version"] == main.TERMS_VERSION
        assert stored_job["terms_accepted_at"] == stored_acceptance["accepted_at"]
        assert started == [(job_id, track_id)]
        candidate = {
            "id": "b" * 32, "title": "Sample Song", "artist": "The Example",
            "match_score": 98, "license_name": "CC BY 4.0",
            "record_url": "https://archive.org/details/licensed-item",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        }
        main.store.set_track(
            track_id, status="needs_review",
            source_candidates=json.dumps([candidate]),
        )
        selected = []
        monkeypatch.setattr(
            main, "start_candidate_download",
            lambda selected_job, selected_track, candidate_id, automatic=False:
                selected.append((selected_job, selected_track, candidate_id, automatic)),
        )
        response = client.post(
            f"/api/jobs/{job_id}/tracks/{track_id}/source",
            headers={"X-Job-Token": payload["token"]},
            json={"candidate_id": "b" * 32},
        )
        assert response.status_code == 200
        assert selected == [(job_id, track_id, "b" * 32, False)]

# Soundloom

Soundloom is a mobile-first Spotify metadata and authorized-audio preparation app. Spotify supplies track and playlist metadata; it does not supply downloadable audio. Soundloom never extracts Spotify audio or bypasses DRM, paywalls, or sign-in restrictions. It searches Internet Archive for files explicitly declared as CC0, CC BY, CC BY-SA, or public domain, then lets users attach their own audio when no eligible match is found.

## What works

- Spotify track, album, public playlist, and Spotify URI parsing, with paginated playlist metadata (up to the configured track limit), ordering, artwork previews, and duplicate detection.
- Optional Spotify OAuth for private and collaborative playlists using the documented Web API scopes playlist-read-private and playlist-read-collaborative.
- Automatic Internet Archive search and metadata matching against explicitly licensed audio. High-confidence, unique title/artist/duration matches (or strong ISRC matches) download automatically; uncertain matches are held for review, and mismatched live, remix, acoustic, cover, or other version markers block automatic selection.
- Per-track match review and replacement selection, retryable source searches, manual per-track audio upload, and FFmpeg checks that reject empty, undecodable, or effectively silent output before a track is downloadable. Playlist ZIP files are CRC-checked and cannot contain zero-byte audio entries.
- Original, MP3 (128/192/256/320 kbps), AAC/M4A, ALAC/M4A, FLAC, WAV, and Opus output, plus a separate ZIP export of completed tracks. The app reports source bitrate and whether a lossless output came from a detected lossless source. Transcoding does not restore missing fidelity.
- Individual downloads and a disk-built ZIP with safe numbered filenames, Spotify cover artwork when available, playlist.m3u8, and manifest.json. ZIP creation does not buffer all audio in RAM.
- Mobile-responsive PWA interface, reduced-motion support, installable home-screen entry, a progress view that survives page refresh, and static-only offline caching.
- Upload, playlist-size, FFmpeg-timeout, encoder-concurrency, per-job upload-storage, and job-retention limits.

Source search is limited to Internet Archive records with an explicit CC0, CC BY, CC BY-SA, or Public Domain Mark license URL in the item's metadata. That license declaration is supplied by the uploader; Soundloom records the item link, attribution, and license in the job manifest and ZIP, and users should inspect the source page before relying on it. CC BY requires attribution; CC BY-SA may require adaptations to carry the same or a compatible license. Soundloom does not search streaming services or general video sites. Many commercial Spotify tracks will have no authorized match. Matching scores are similarity measures, not probabilities; anything ambiguous waits for user review. New authorized providers can implement the SearchableAudioSource protocol in **backend/sources.py**.

## Local development

Requires Python 3.12, FFmpeg/FFprobe, and a Spotify developer app for metadata lookup.

1. Create an environment and install pinned dependencies:

   ~~~sh
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ~~~

2. Copy .env.example to .env and set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET. For private playlists, also set SPOTIFY_REDIRECT_URI and PUBLIC_BASE_URL, then add that exact callback URL to the Spotify developer app.
3. Set DATA_DIR to a writable directory and start the app:

   ~~~sh
   export DATA_DIR=./data
   uvicorn backend.main:app --env-file .env --reload --port 8000 --no-access-log
   ~~~

4. Visit http://localhost:8000. GET /health reports service readiness.

Spotify Web API access depends on the developer account, app mode, granted scopes, and Spotify's current endpoint availability and rate limits. Credentials stay on the server. Private-playlist OAuth sessions are held in memory and must reconnect after a service restart.

## Deploy on Render

1. In Render, choose **New + → Blueprint**, connect this GitHub repository, and apply the included `render.yaml`. It creates a Docker web service with FFmpeg, a health check, and a persistent 25 GB disk mounted at `/data`. The disk requires a paid Render plan.
2. In the Render service environment, set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` from your Spotify developer app.
3. After Render assigns the service URL, set `PUBLIC_BASE_URL` to that HTTPS origin and `SPOTIFY_REDIRECT_URI` to `<PUBLIC_BASE_URL>/api/auth/callback`. Register that exact callback URL in the Spotify developer dashboard, then redeploy.
4. Keep one service instance. SQLite state, temporary audio, and the in-process worker queue require a single instance with the persistent disk attached.
5. Wait for the `/health` check to pass and open the Render service URL on your phone.

Render's `PORT` is used by the included Dockerfile. The browser can leave while source searches, downloads, and audio processing run server-side. Keep the service and disk online until downloads finish. Jobs and temporary files expire after `RETENTION_DAYS`; cleanup runs at startup and hourly. SQLite is not object storage, so set storage limits to fit the disk. Protect the service and disk as private user data.

## Use on iPhone

1. Open the deployed HTTPS URL in Safari.
2. Paste an open.spotify.com track, album, or playlist link, or a Spotify URI. If the item is private, tap **Connect Spotify** and grant playlist access.
3. Choose the output format and preview the ordered track list.
4. Soundloom searches licensed sources automatically. Review any suggested match that needs approval and choose **Use match**, or tap **Add audio** to select a file you own or may use from Files.
5. Leave the page if needed; the server keeps processing. Return in Safari on the same device to see saved job progress and download individual songs or the ZIP.
6. To add the app icon, use Safari's Share button, then **Add to Home Screen**.

Large exports are served as a file response from disk rather than loaded as a browser-side Blob. The job's unguessable access token is included in download URLs so Safari can download files without custom request headers; avoid sharing those URLs. Uvicorn access logging is disabled to keep tokens out of application request logs.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET | empty | Server-side Web API credentials |
| SPOTIFY_REDIRECT_URI | empty | Exact OAuth callback registered with Spotify |
| PUBLIC_BASE_URL | empty | Public app origin used after OAuth |
| DATA_DIR | /data | SQLite database and temporary job audio |
| MAX_UPLOAD_BYTES | 200 MiB | Maximum per uploaded track |
| MAX_TRACKS_PER_JOB | 300 | Maximum resolved tracks in one job |
| MAX_ENCODERS | 2 | Concurrent FFmpeg encoders (1–4) |
| ARCHIVE_SEARCH_CONCURRENCY | 2 | Concurrent Internet Archive requests (1–4) |
| MAX_PENDING_SOURCE_TASKS | 600 | Bound queued searches and downloads across jobs |
| AUTO_MATCH_THRESHOLD | 0.94 | Minimum match score for automatic download (0.80–0.99) |
| MAX_JOB_STORAGE_BYTES | 8 GiB | Per-job total temporary storage cap |
| MAX_TOTAL_STORAGE_BYTES | 20 GiB | Service-wide temporary storage cap |
| FFMPEG_TIMEOUT_SECONDS | 1800 | Conversion timeout per track |
| RETENTION_DAYS | 7 | Remove old jobs during service startup |
| LOG_LEVEL | INFO | Application log level |

## Tests

Install developer checks with pip install -r requirements-dev.txt, then run DATA_DIR=/tmp/soundloom-test-data pytest. Tests use synthetic audio and mocked provider metadata only; no commercial recordings or live provider requests are required. Audio conversion integration requires FFmpeg installed. The shell workspace used to author this commit was unavailable, so the repository tests and Docker build have not been run here.

## Current boundaries

- Automatic source availability depends on Internet Archive catalog results and explicit license metadata. An uploader's declaration is not an independent rights audit; inspect the item and license before reuse. No streaming-service or video-site downloads are implemented.
- Spotify API access and private playlist visibility depend on the configured Spotify developer app. Spotify may limit access to certain endpoints or accounts.
- OAuth sessions are process-memory sessions, while jobs and uploaded audio persist in SQLite and the mounted volume. One Render service instance is required.
- Source download and FFmpeg progress are reported; search has no ETA because provider response times and catalog results vary.
- Archive artwork is not embedded into each converted file. Original format preserves the original file, including its existing metadata and artwork; converted formats receive Spotify title, artist, album, and track tags.

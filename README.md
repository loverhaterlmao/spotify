# Soundloom

Soundloom is a mobile-first Spotify metadata preview and authorized-audio preparation app. Spotify supplies track and playlist metadata; it does not supply downloadable audio. Soundloom never extracts Spotify audio or bypasses DRM, paywalls, or sign-in restrictions. A user attaches a local audio file they own or are authorized to use to each selected Spotify track.

## What works

- Spotify track, album, public playlist, and Spotify URI parsing, with paginated playlist metadata (up to the configured track limit), ordering, artwork previews, and duplicate detection.
- Optional Spotify OAuth for private and collaborative playlists using the documented Web API scopes playlist-read-private and playlist-read-collaborative.
- Manual per-track audio upload, decodable-audio verification, bounded asynchronous FFmpeg work, real encoder progress, cancellation, retry of failures, and job recovery from a persistent SQLite database after a process restart.
- Original, MP3 (128/192/256/320 kbps), AAC/M4A, FLAC, WAV, ALAC, and Opus output. The app reports source bitrate and whether a lossless output came from a detected lossless source. Transcoding does not restore missing fidelity.
- Individual downloads and a disk-built ZIP with safe numbered filenames, Spotify cover artwork when available, playlist.m3u8, and manifest.json. ZIP creation does not buffer all audio in RAM.
- Mobile-responsive PWA interface, reduced-motion support, installable home-screen entry, a progress view that survives page refresh, and static-only offline caching.
- Upload, playlist-size, FFmpeg-timeout, encoder-concurrency, per-job upload-storage, and job-retention limits.

There is no automatic third-party audio search or source matching in this deployment. Every audio file is selected by the user for an exact track row, which avoids silently substituting a cover, remix, or live version. A future source adapter should be connected only to a provider with explicit authorization to download and redistribute the selected audio. The AudioSource protocol in **backend/sources.py** is the extension point.

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

## Railway

1. Push this repository to GitHub and create a Railway service from the repository. The included Dockerfile installs FFmpeg and runs the app as an unprivileged user. railway.toml declares the /data volume and /health health check.
2. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI, and PUBLIC_BASE_URL in the Railway service. The redirect URI should be https://<your-domain>/api/auth/callback and must match the Spotify developer dashboard exactly. Railway provides PORT.
3. Attach a persistent volume at /data. Use a single replica: SQLite state, local temporary audio, and the in-process worker queue are designed for one service instance.
4. Deploy, wait for /health to pass, then open the Railway public domain on the phone.

The browser can leave while uploaded tracks are being processed because work runs on the server. Keep the service and /data volume online until downloads finish. Jobs and temporary files are removed after RETENTION_DAYS; cleanup runs at service startup and hourly. SQLite is not object storage, so set storage limits to suit the volume. Protect the service and its volume as private user data.

## Use on iPhone

1. Open the deployed HTTPS URL in Safari.
2. Paste an open.spotify.com track, album, or playlist link, or a Spotify URI. If the item is private, tap **Connect Spotify** and grant playlist access.
3. Choose the output format and preview the ordered track list.
4. For each selected track, tap **Add audio** and choose an authorized file in Files. Repeat for the tracks you want.
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
| MAX_JOB_STORAGE_BYTES | 8 GiB | Per-job total temporary storage cap |
| MAX_TOTAL_STORAGE_BYTES | 20 GiB | Service-wide temporary storage cap |
| FFMPEG_TIMEOUT_SECONDS | 1800 | Conversion timeout per track |
| RETENTION_DAYS | 7 | Remove old jobs during service startup |
| LOG_LEVEL | INFO | Application log level |

## Tests

Install developer checks with pip install -r requirements-dev.txt, then run DATA_DIR=/tmp/soundloom-test-data pytest. Tests use synthetic bytes and metadata only; no commercial recordings or network calls are required. Audio conversion integration requires FFmpeg installed. The shell workspace used to author this commit was unavailable, so the repository tests and Docker build have not been run here.

## Current boundaries

- Users provide the audio. The app does not search public sites, download from streaming services, or infer a source match.
- Spotify API access and private playlist visibility depend on the configured Spotify developer app. Spotify may limit access to certain endpoints or accounts.
- OAuth sessions are process-memory sessions, while jobs and uploaded audio persist in SQLite and the mounted volume. One Railway replica is required.
- Upload progress and FFmpeg progress are reported; there is no ETA because the workload and storage performance vary.
- Archive artwork is not embedded into each converted file. Original format preserves the original file, including its existing metadata and artwork; converted formats receive Spotify title, artist, album, and track tags.

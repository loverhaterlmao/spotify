# Soundloom

Soundloom is a mobile-first Spotify playlist-to-audio app. Spotify supplies metadata, while configured audio providers supply files the operator is authorized to download. Soundloom does not extract Spotify audio or bypass DRM, paywalls, or sign-in restrictions. Public Spotify embed pages support small-link previews without app credentials; CSV import handles larger playlists. Audio matching uses Internet Archive's declared Creative Commons/public-domain records and an optional, operator-configured YouTube channel allowlist covered by the operator's download rights.

## What works

- Spotify track, album, public playlist, and Spotify URI parsing. Public embeds provide credential-free rows up to the configured 50-row cap, but Spotify may expose only a preview subset; the UI flags this and CSV import is the complete-list fallback. Configured Spotify Web API access supports pagination, richer metadata, and private playlists.
- Playlist CSV import for mobile users who cannot create a Spotify developer app. Export the playlist through TuneMyMusic using Spotify sign-in, then upload the CSV; Soundloom does not store the CSV after parsing.
- Optional Spotify OAuth for private and collaborative playlists using the documented Web API scopes playlist-read-private and playlist-read-collaborative.
- Automatic matching across enabled audio adapters: Internet Archive's declared license metadata and optional YouTube searches restricted to operator-licensed channel IDs. High-confidence, unique title/artist/duration matches (or strong ISRC matches) download automatically; uncertain matches are held for review, and mismatched live, remix, acoustic, cover, or other version markers block automatic selection.
- Per-track match review and replacement selection, retryable source searches, manual per-track audio upload, and FFmpeg checks that reject empty, undecodable, or effectively silent output before a track is downloadable. Playlist ZIP files are CRC-checked and cannot contain zero-byte audio entries.
- Original, MP3 (128/192/256/320 kbps), AAC/M4A, ALAC/M4A, FLAC, WAV, and Opus output, plus a separate ZIP export of completed tracks. The app reports source bitrate and whether a lossless output came from a detected lossless source. Transcoding does not restore missing fidelity.
- Individual downloads and a disk-built ZIP with safe numbered filenames, Spotify cover artwork when available, playlist.m3u8, and manifest.json. ZIP creation does not buffer all audio in RAM.
- Mobile-responsive PWA interface, reduced-motion support, installable home-screen entry, a progress view that survives page refresh, and static-only offline caching.
- Upload, playlist-size, FFmpeg-timeout, encoder-concurrency, per-job upload-storage, and job-retention limits.

The YouTube adapter is disabled by default. To enable it, configure explicit licensed channel IDs, a rights acknowledgement, and the applicable license reference in server environment variables. Search results are restricted to those channel IDs; the adapter does not use browser cookies or playlists. The operator is responsible for confirming that both the audio rights and the source's download terms permit the configured use. Internet Archive license declarations come from uploaders and are not independently verified. Soundloom records source, attribution, and license details in job manifests. Matching scores are similarity measures, not probabilities; ambiguous matches wait for review. Many Spotify recordings will not exist in an enabled downloadable catalog. Matching scores are similarity measures, not probabilities; anything ambiguous waits for user review. New authorized providers can implement the SearchableAudioSource protocol in **backend/sources.py**.

## Local development

Requires Python 3.12, FFmpeg/FFprobe, and optionally yt-dlp for the licensed-channel adapter. Public Spotify links up to 50 tracks can be previewed without a Spotify app; use CSV for larger playlists. Spotify credentials remain optional for richer metadata and private playlists.

1. Create an environment and install pinned dependencies:

   ~~~sh
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ~~~

2. Copy .env.example to .env. Spotify credentials are optional for public embeds. For private playlists, set Spotify credentials plus SPOTIFY_REDIRECT_URI and PUBLIC_BASE_URL, then register that exact callback URL in Spotify. To use YouTube search, enable it only after configuring the licensed channel IDs and rights reference described below.
3. Set DATA_DIR to a writable directory and start the app:

   ~~~sh
   export DATA_DIR=./data
   uvicorn backend.main:app --env-file .env --reload --port 8000 --no-access-log
   ~~~

4. Visit http://localhost:8000. GET /health reports service readiness.

Spotify Web API access depends on the developer account, app mode, granted scopes, and Spotify's current endpoint availability and rate limits. Credentials stay on the server. Private-playlist OAuth sessions are held in memory and must reconnect after a service restart.

## Free local use on iPhone

For a zero-hosting-bill setup, run the Docker app on a computer you own and open it from Safari on the same Wi-Fi network. This keeps the job database and audio in a Docker named volume, and the host machine performs FFmpeg work. It costs no app-hosting fee, but the computer must stay on and the app is only reachable on your local network.

1. Install Docker Desktop on the host computer and copy `.env.example` to `.env`. Add Spotify credentials if you want metadata lookup; public playlists work without Spotify OAuth.
2. Start Soundloom with `docker compose up --build -d`.
3. Find the computer's local Wi-Fi address and open `http://<computer-LAN-IP>:8000` in iPhone Safari.
4. Use `docker compose logs -f` to inspect the service. `docker compose down` preserves job data; `docker compose down -v` deletes the named data volume.

Do not expose this local HTTP service directly to the public internet. Private-playlist OAuth requires a registered HTTPS callback; use public playlists on this LAN setup unless you configure a secure HTTPS reverse proxy.

## Performance and cost

The app keeps processing on one bounded server process and avoids a paid Redis dependency. Track conversion reuses the source's FFprobe result instead of probing the same file twice. Licensed-catalog lookups first inspect the indexed license field, fetch detailed metadata only for potentially eligible items, and overlap up to four detail requests per track while preserving the shared Internet Archive request pace. These are workload optimizations, not a universal speed ranking; audio-source rate limits and FFmpeg encoding remain the main throughput limits.

Spotify's Web API and Internet Archive access do not require a per-download fee. The optional yt-dlp dependency adds no service fee, but YouTube availability, channel coverage, and host resources constrain throughput. Reliable public background processing and persistent storage do require a paid host or storage plan: Render's free service has no persistent disk and can sleep or restart, which can interrupt jobs and erase downloads. The optional `render-free.yaml` is a small, non-durable preview profile; use the regular `render.yaml` for durable server-side jobs.

## Deploy on Render

1. In Render, choose **New + → Blueprint**, connect this GitHub repository, and apply the included `render.yaml`. It creates a Docker web service with FFmpeg, a health check, and a persistent 25 GB disk mounted at `/data`. The disk requires a paid Render plan.
2. In the Render service environment, set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` from your Spotify developer app.
3. After Render assigns the service URL, set `PUBLIC_BASE_URL` to that HTTPS origin and `SPOTIFY_REDIRECT_URI` to `<PUBLIC_BASE_URL>/api/auth/callback`. Register that exact callback URL in the Spotify developer dashboard, then redeploy.
4. Keep one service instance. SQLite state, temporary audio, and the in-process worker queue require a single instance with the persistent disk attached.
5. Wait for the `/health` check to pass and open the Render service URL on your phone.

Render's `PORT` is used by the included Dockerfile. The browser can leave while source searches, downloads, and audio processing run server-side. Keep the service and disk online until downloads finish. Jobs and temporary files expire after `RETENTION_DAYS`; cleanup runs at startup and hourly. SQLite is not object storage, so set storage limits to fit the disk. Protect the service and disk as private user data.

## Use on iPhone

1. Open the deployed HTTPS URL in Safari.
2. Paste a public Spotify link to preview the rows Spotify exposes. Compare the shown count with the playlist; if it is incomplete or over 50 tracks, use [TuneMyMusic's Spotify-to-file exporter](https://www.tunemymusic.com/transfer/spotify-to-file), sign in through its authorization page, and export a CSV. Private playlists still need the configured Spotify OAuth connection.
3. Return to Soundloom, tap **Import CSV**, select the exported file from Files, then choose tracks and an output format.
4. Soundloom searches enabled authorized sources. Review uncertain matches before downloading; tracks without a permitted match stay unavailable. You can also tap **Add audio** to select a file you own or may use from Files.
5. Leave the page if needed; the server keeps processing. Return in Safari on the same device to see saved job progress and download individual files or the ZIP.
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
| YOUTUBE_SOURCE_ENABLED | false | Enable the licensed channel adapter |
| YOUTUBE_RIGHTS_ACKNOWLEDGED | false | Operator acknowledgement of download rights |
| YOUTUBE_LICENSED_CHANNEL_IDS | empty | Comma-separated YouTube channel IDs covered by those rights |
| YOUTUBE_LICENSE_NAME / YOUTUBE_LICENSE_URL | empty | License name and HTTPS reference attached to downloads |
| MAX_JOB_STORAGE_BYTES | 8 GiB | Per-job total temporary storage cap |
| MAX_TOTAL_STORAGE_BYTES | 20 GiB | Service-wide temporary storage cap |
| FFMPEG_TIMEOUT_SECONDS | 1800 | Conversion timeout per track |
| RETENTION_DAYS | 7 | Remove old jobs during service startup |
| LOG_LEVEL | INFO | Application log level |

## Tests

Install developer checks with pip install -r requirements-dev.txt, then run DATA_DIR=/tmp/soundloom-test-data pytest. Tests use synthetic audio and mocked provider metadata only; no commercial recordings or live provider requests are required. Audio conversion integration requires FFmpeg installed. The shell workspace used to author this commit was unavailable, so the repository tests and Docker build have not been run here.

## Current boundaries

- Automatic source availability depends on enabled catalogs and the operator's rights configuration. The optional YouTube adapter searches only configured channels and may return no candidate; it does not use cookies or download playlists. Verify the rights and source terms before enabling it.
- Spotify API access and private playlist visibility depend on the configured Spotify developer app. Spotify may limit access to certain endpoints or accounts.
- OAuth sessions are process-memory sessions, while jobs and uploaded audio persist in SQLite and the mounted volume. One Render service instance is required.
- Source download and FFmpeg progress are reported; search has no ETA because provider response times and catalog results vary.
- Archive artwork is not embedded into each converted file. Original format preserves the original file, including its existing metadata and artwork; converted formats receive Spotify title, artist, album, and track tags.

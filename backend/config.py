import os
from pathlib import Path


class Settings:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    spotify_redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    max_upload_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
    max_tracks = max(1, min(300, int(os.getenv("MAX_TRACKS_PER_JOB", "300"))))
    max_encoders = max(1, min(4, int(os.getenv("MAX_ENCODERS", "2"))))
    archive_search_concurrency = max(1, min(4, int(os.getenv("ARCHIVE_SEARCH_CONCURRENCY", "2"))))
    youtube_source_enabled = os.getenv("YOUTUBE_SOURCE_ENABLED", "false").lower() == "true"
    youtube_rights_acknowledged = os.getenv("YOUTUBE_RIGHTS_ACKNOWLEDGED", "false").lower() == "true"
    youtube_licensed_channel_ids = {
        item.strip() for item in os.getenv("YOUTUBE_LICENSED_CHANNEL_IDS", "").split(",") if item.strip()
    }
    youtube_license_name = os.getenv("YOUTUBE_LICENSE_NAME", "")
    youtube_license_url = os.getenv("YOUTUBE_LICENSE_URL", "")
    max_pending_source_tasks = max(10, min(2000, int(os.getenv("MAX_PENDING_SOURCE_TASKS", "600"))))
    auto_match_threshold = max(0.80, min(0.99, float(os.getenv("AUTO_MATCH_THRESHOLD", "0.94"))))
    retention_days = max(1, int(os.getenv("RETENTION_DAYS", "7")))
    ffmpeg_timeout_seconds = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "1800"))
    max_uploads_per_job_bytes = int(os.getenv("MAX_JOB_STORAGE_BYTES", str(8 * 1024 * 1024 * 1024)))
    max_total_storage_bytes = int(os.getenv("MAX_TOTAL_STORAGE_BYTES", str(20 * 1024 * 1024 * 1024)))

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def oauth_enabled(self) -> bool:
        return self.spotify_enabled and bool(self.spotify_redirect_uri)


settings = Settings()

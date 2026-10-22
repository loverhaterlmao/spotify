import hashlib
import hmac
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, name TEXT NOT NULL,
                    artwork TEXT, kind TEXT NOT NULL, output_format TEXT NOT NULL,
                    bitrate TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, spotify_id TEXT, title TEXT NOT NULL,
                    artist TEXT NOT NULL, album TEXT NOT NULL, duration_ms INTEGER, track_number INTEGER, disc_number INTEGER,
                    isrc TEXT, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                    input_path TEXT, output_path TEXT, source_bitrate INTEGER,
                    lossless_from_source INTEGER, error TEXT, duplicate_of TEXT,
                    source_candidates TEXT NOT NULL DEFAULT '[]', source_name TEXT, source_url TEXT,
                    source_license TEXT, source_license_url TEXT, source_attribution TEXT,
                    source_confidence REAL, source_auto INTEGER
                );
                CREATE INDEX IF NOT EXISTS tracks_job_position ON tracks(job_id, position);
            """)
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tracks)")}
            if "track_number" not in columns:
                self.db.execute("ALTER TABLE tracks ADD COLUMN track_number INTEGER")
            if "disc_number" not in columns:
                self.db.execute("ALTER TABLE tracks ADD COLUMN disc_number INTEGER")
            source_columns = {
                "source_candidates": "TEXT NOT NULL DEFAULT '[]'",
                "source_name": "TEXT", "source_url": "TEXT", "source_license": "TEXT",
                "source_license_url": "TEXT", "source_attribution": "TEXT",
                "source_confidence": "REAL", "source_auto": "INTEGER",
            }
            for name, declaration in source_columns.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE tracks ADD COLUMN {name} {declaration}")
            self.db.commit()

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_job(self, job: dict[str, Any], token: str, tracks: list[dict[str, Any]]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (job["id"], self.hash_token(token), job["name"], job.get("artwork"),
                 job["kind"], job["format"], job.get("bitrate"), "awaiting_upload", job["created_at"]),
            )
            self.db.executemany(
                """INSERT INTO tracks
                (id,job_id,position,spotify_id,title,artist,album,duration_ms,track_number,disc_number,isrc,status,duplicate_of)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(t["id"], job["id"], t["position"], t.get("spotify_id"), t["title"],
                  t["artist"], t["album"], t.get("duration_ms"), t.get("track_number"),
                  t.get("disc_number"), t.get("isrc"), t["status"], t.get("duplicate_of")) for t in tracks],
            )

    def authorized_job(self, job_id: str, token: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not hmac.compare_digest(row["token_hash"], self.hash_token(token)):
                return None
            return dict(row)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def tracks(self, job_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM tracks WHERE job_id=? ORDER BY position", (job_id,)
            ).fetchall()]

    def track(self, job_id: str, track_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tracks WHERE job_id=? AND id=?", (job_id, track_id)
            ).fetchone()
            return dict(row) if row else None

    def set_track(self, track_id: str, **values: Any) -> None:
        allowed = {"status", "progress", "input_path", "output_path", "source_bitrate",
                   "lossless_from_source", "error", "source_candidates", "source_name", "source_url",
                   "source_license", "source_license_url", "source_attribution", "source_confidence", "source_auto"}
        updates = {k: v for k, v in values.items() if k in allowed}
        if not updates:
            return
        sql = ", ".join(f"{k}=?" for k in updates)
        with self.lock, self.db:
            self.db.execute(f"UPDATE tracks SET {sql} WHERE id=?", (*updates.values(), track_id))

    def set_job_status(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status=?, error=? WHERE id=?", (status, error, job_id))

    def retry_failed(self, job_id: str) -> list[dict[str, Any]]:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE tracks SET status='queued',progress=0,error=NULL WHERE job_id=? AND status='failed' AND input_path IS NOT NULL",
                (job_id,),
            )
        return [t for t in self.tracks(job_id) if t["status"] == "queued" and t["input_path"]]

    def recoverable(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM tracks WHERE (status IN ('queued','processing') AND input_path IS NOT NULL) OR status IN ('uploading','searching','downloading')"
            ).fetchall()]

    def old_jobs(self, before_epoch: float) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id FROM jobs WHERE created_at < ?", (before_epoch,)
            ).fetchall()]

    def delete_job(self, job_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def close(self) -> None:
        with self.lock:
            self.db.close()

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SuppliedAudio:
    path: Path
    filename: str


class AudioSource(Protocol):
    """Pluggable interface for sources the operator is authorized to use."""

    name: str

    async def acquire(self, track_id: str) -> SuppliedAudio:
        """Return audio the caller has permission to use."""


class UserUploadSource:
    """Manual source adapter: uses only audio the user uploaded."""

    name = "user-upload"

    def __init__(self, uploads: dict[str, SuppliedAudio]):
        self.uploads = uploads

    async def acquire(self, track_id: str) -> SuppliedAudio:
        if track_id not in self.uploads:
            raise LookupError("No user-supplied audio is attached to this track.")
        return self.uploads[track_id]

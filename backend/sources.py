from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class SuppliedAudio:
    path: Path
    filename: str


class AudioSource(Protocol):
    """Pluggable interface for sources the operator is authorized to use."""

    name: str

    async def acquire(self, track_id: str) -> SuppliedAudio:
        """Return audio the caller has permission to use."""


class SearchableAudioSource(Protocol):
    """A source adapter that searches metadata and verifies download permissions."""

    name: str

    async def search(self, track: dict, max_file_bytes: int) -> list[dict]:
        """Return candidates with explicit licenses and stable source identifiers."""

    async def download(
        self,
        candidate: dict,
        destination: Path,
        max_bytes: int,
        on_progress: Callable[[float], Awaitable[None]],
    ) -> int:
        """Download a selected, revalidated candidate to a local file."""


class UserUploadSource:
    """Manual source adapter: uses only audio the user uploaded."""

    name = "user-upload"

    def __init__(self, uploads: dict[str, SuppliedAudio]):
        self.uploads = uploads

    async def acquire(self, track_id: str) -> SuppliedAudio:
        if track_id not in self.uploads:
            raise LookupError("No user-supplied audio is attached to this track.")
        return self.uploads[track_id]

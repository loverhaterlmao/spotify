import re
import unicodedata
from difflib import SequenceMatcher


_VERSION_MARKERS = {
    "live": re.compile(r"\b(?:live|concert|in concert)\b", re.I),
    "remix": re.compile(r"\bremix(?:ed)?\b", re.I),
    "instrumental": re.compile(r"\binstrumental\b", re.I),
    "acoustic": re.compile(r"\bacoustic\b", re.I),
    "cover": re.compile(r"\bcover(?:ed)?\b", re.I),
    "karaoke": re.compile(r"\bkaraoke\b", re.I),
    "slowed": re.compile(r"\bslowed(?:\s+and\s+reverb)?\b", re.I),
    "sped_up": re.compile(r"\b(?:sped\s+up|speed\s+up|nightcore)\b", re.I),
    "demo": re.compile(r"\bdemo\b", re.I),
    "radio_edit": re.compile(r"\bradio\s+edit\b", re.I),
    "remaster": re.compile(r"\bremaster(?:ed)?\b", re.I),
}


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _similarity(left: str | None, right: str | None) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_words, b_words = set(a.split()), set(b.split())
    token = 2 * len(a_words & b_words) / max(1, len(a_words) + len(b_words))
    return max(sequence, token)


def _primary_artist(value: str | None) -> str:
    text = str(value or "")
    for delimiter in (" feat. ", " feat ", " featuring ", ", ", "; ", " & "):
        if delimiter in text.casefold():
            index = text.casefold().index(delimiter)
            return text[:index].strip()
    return text.strip()


def _version_flags(value: str | None) -> set[str]:
    text = str(value or "")
    return {name for name, pattern in _VERSION_MARKERS.items() if pattern.search(text)}


def _duration_seconds(value: object, *, milliseconds: bool = False) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds:
        duration /= 1000
    return duration if 0 < duration <= 4 * 60 * 60 else None


def score_candidate(
    track: dict,
    candidate: dict,
    auto_threshold: float = 0.94,
) -> dict:
    """Return an explainable similarity score; it is not a probability of identity."""
    title_score = _similarity(track.get("title"), candidate.get("title"))
    artist_score = _similarity(_primary_artist(track.get("artist")), _primary_artist(candidate.get("artist")))
    track_duration = _duration_seconds(track.get("duration_ms"), milliseconds=True)
    candidate_duration = _duration_seconds(candidate.get("duration_seconds"))
    duration_score = 0.0
    duration_delta = None
    if track_duration is not None and candidate_duration is not None:
        duration_delta = abs(track_duration - candidate_duration)
        duration_score = max(0.0, 1.0 - duration_delta / max(5.0, track_duration * 0.08))

    components = [(title_score, 0.36), (artist_score, 0.32)]
    if duration_delta is not None:
        components.append((duration_score, 0.20))
    album_score = _similarity(track.get("album"), candidate.get("album"))
    if normalize_text(track.get("album")) and normalize_text(candidate.get("album")):
        components.append((album_score, 0.05))

    track_isrc = re.sub(r"[^A-Z0-9]", "", str(track.get("isrc") or "").upper())
    candidate_isrc = re.sub(r"[^A-Z0-9]", "", str(candidate.get("isrc") or "").upper())
    isrc_match = bool(track_isrc and candidate_isrc and track_isrc == candidate_isrc)
    isrc_conflict = bool(track_isrc and candidate_isrc and track_isrc != candidate_isrc)
    if track_isrc and candidate_isrc:
        components.append((1.0 if isrc_match else 0.0, 0.12))

    score = sum(value * weight for value, weight in components) / sum(weight for _, weight in components)
    track_versions = _version_flags(track.get("title"))
    candidate_versions = _version_flags(candidate.get("title"))
    version_conflict = track_versions != candidate_versions
    if isrc_conflict:
        score -= 0.35
    if version_conflict:
        score -= 0.22
    score = min(1.0, max(0.0, score))

    exact_metadata = (
        title_score >= 0.98 and artist_score >= 0.95 and
        duration_delta is not None and duration_delta <= 1.5
    )
    strong_isrc = isrc_match and title_score >= 0.82 and artist_score >= 0.75
    auto_match = (
        score >= auto_threshold and not version_conflict and not isrc_conflict and
        (exact_metadata or strong_isrc)
    )
    reasons = []
    if title_score >= 0.95:
        reasons.append("title matches")
    elif title_score >= 0.65:
        reasons.append("similar title")
    if artist_score >= 0.9:
        reasons.append("artist matches")
    elif artist_score < 0.45:
        reasons.append("artist differs")
    if duration_delta is not None:
        reasons.append(f"duration differs by {duration_delta:.1f}s")
    if isrc_match:
        reasons.append("ISRC matches")
    elif isrc_conflict:
        reasons.append("ISRC differs")
    if version_conflict:
        reasons.append("version markers differ")

    return {
        **candidate,
        "score": round(score, 4),
        "match_score": round(score * 100),
        "auto_match": auto_match,
        "match_reasons": reasons,
        "version_conflict": version_conflict,
        "isrc_conflict": isrc_conflict,
    }


def rank_candidates(track: dict, candidates: list[dict], auto_threshold: float = 0.94) -> list[dict]:
    ranked = [
        score_candidate(track, candidate, auto_threshold)
        for candidate in candidates
    ]
    ranked.sort(key=lambda candidate: (candidate["score"], candidate.get("size", 0)), reverse=True)
    if ranked:
        top = ranked[0]
        margin = top["score"] - ranked[1]["score"] if len(ranked) > 1 else 1.0
        top["auto_match"] = bool(top["auto_match"] and margin >= 0.06)
        top["score_margin"] = round(margin, 4)
    return ranked[:5]

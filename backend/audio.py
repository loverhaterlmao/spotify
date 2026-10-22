import asyncio
import json
import math
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Awaitable, Callable


ALLOWED_INPUT_EXTENSIONS = {
    ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".ogg", ".opus",
    ".aiff", ".aif", ".wma", ".m4b",
}
FORMAT_EXTENSIONS = {
    "mp3": ".mp3", "aac": ".m4a", "flac": ".flac", "wav": ".wav",
    "alac": ".m4a", "opus": ".opus",
}
LOSSLESS_CODECS = {"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "wavpack"}

class AudioError(ValueError):
    pass


def safe_filename(value: str, max_length: int = 120) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:max_length].strip(" .")
    return value or "untitled"


def output_options(output_format: str, bitrate: str | None) -> tuple[list[str], str]:
    if output_format == "original":
        return [], ""
    if output_format == "mp3":
        rate = bitrate if bitrate in {"128", "192", "256", "320"} else "320"
        return ["-c:a", "libmp3lame", "-b:a", rate + "k", "-id3v2_version", "3"], ".mp3"
    if output_format == "aac":
        return ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"], ".m4a"
    if output_format == "flac":
        return ["-c:a", "flac", "-compression_level", "5"], ".flac"
    if output_format == "wav":
        return ["-c:a", "pcm_s16le"], ".wav"
    if output_format == "alac":
        return ["-c:a", "alac", "-movflags", "+faststart"], ".m4a"
    if output_format == "opus":
        return ["-c:a", "libopus", "-b:a", "160k", "-vbr", "on"], ".opus"
    raise AudioError("Choose a supported output format.")


async def verify_audio_signal(path: Path, timeout_seconds: int = 120) -> None:
    """Reject streams with no decoded audio or only digital silence."""
    if not path.is_file() or path.stat().st_size == 0:
        raise AudioError("The audio file is empty and contains no audio.")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-xerror", "-loglevel", "info",
            "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a:0", "-vn",
            "-af", "volumedetect", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise AudioError("Audio content verification exceeded the time limit.") from exc
    except OSError as exc:
        raise AudioError("FFmpeg is unavailable or could not verify the audio content.") from exc
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise AudioError("The audio file could not be fully decoded.")
    report = stderr.decode("utf-8", errors="replace")
    levels = re.findall(r"max_volume:\s*(-inf|[+-]?\d+(?:\.\d+)?)\s*dB", report, re.IGNORECASE)
    if not levels:
        raise AudioError("The audio file did not yield any decoded sound samples.")
    level = levels[-1].casefold()
    if level == "-inf" or float(level) < -90:
        raise AudioError("The audio file is silent and contains no usable sound.")


async def probe_audio(path: Path, verify_signal: bool = False) -> dict:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise AudioError("The audio file is empty and contains no audio.")
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,bit_rate,format_name:stream=codec_type,codec_name,sample_rate,bit_rate,channels",
            "-of", "json", "-protocol_whitelist", "file,pipe", str(path), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except AudioError:
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        raise AudioError("Could not inspect this audio file.") from exc
    if proc.returncode != 0:
        raise AudioError("The audio file is not readable.")
    try:
        data = json.loads(stdout)
        audio = next(s for s in data.get("streams", []) if s.get("codec_type") == "audio")
        duration = float(data.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0 or duration > 4 * 60 * 60:
            raise ValueError("duration")
    except (ValueError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise AudioError("The file does not contain a supported audio stream.") from exc
    if verify_signal:
        await verify_audio_signal(path)
    format_info = data.get("format", {})
    bitrate = format_info.get("bit_rate") or audio.get("bit_rate")
    codec = str(audio.get("codec_name", "")).lower()
    return {
        "duration": duration,
        "bitrate": int(bitrate) if str(bitrate or "").isdigit() else None,
        "codec": codec,
        "sample_rate": int(audio.get("sample_rate", 0) or 0),
        "channels": int(audio.get("channels", 2) or 2),
        "lossless": codec in LOSSLESS_CODECS or codec.startswith("pcm_"),
    }


async def convert_audio(
    source: Path,
    destination: Path,
    output_format: str,
    bitrate: str | None,
    metadata: dict[str, str],
    timeout_seconds: int,
    on_progress: Callable[[float], Awaitable[None]],
    *,
    source_info: dict | None = None,
) -> dict:
    if source_info is None:
        source_info = await probe_audio(source, verify_signal=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "original":
        extension = source.suffix.lower()
        if extension not in ALLOWED_INPUT_EXTENSIONS:
            raise AudioError("The original file type is not supported for export.")
        temporary = destination.with_name(destination.stem + ".partial" + extension)
        try:
            await asyncio.to_thread(shutil.copyfile, source, temporary)
            await probe_audio(temporary, verify_signal=True)
            os.replace(temporary, destination.with_suffix(extension))
            await on_progress(100)
            return {**source_info, "output_path": str(destination.with_suffix(extension)),
                    "extension": extension, "lossless_from_source": source_info["lossless"]}
        finally:
            temporary.unlink(missing_ok=True)

    options, extension = output_options(output_format, bitrate)
    output = destination.with_suffix(extension)
    temporary = output.with_name(output.stem + ".partial" + extension)
    tags = []
    for key in ("title", "artist", "album", "track", "disc"):
        value = metadata.get(key)
        if value:
            tags.extend(["-metadata", f"{key}={value}"])
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:a:0", "-vn", *tags, *options,
        "-progress", "pipe:1", str(temporary),
    ]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        last_update = 0.0
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            if not line:
                break
            if line.startswith(b"out_time_ms="):
                try:
                    microseconds = int(line.split(b"=", 1)[1])
                    percent = min(99.0, max(1.0, 100 * microseconds / (source_info["duration"] * 1_000_000)))
                    now = asyncio.get_running_loop().time()
                    if now - last_update > 0.4:
                        await on_progress(percent)
                        last_update = now
                except (ValueError, ZeroDivisionError):
                    pass
        await asyncio.wait_for(process.wait(), timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
        if process.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
            raise AudioError("FFmpeg could not encode this file.")
        verified = await probe_audio(temporary, verify_signal=True)
        if abs(verified["duration"] - source_info["duration"]) > max(3.0, source_info["duration"] * 0.02):
            raise AudioError("The output audio failed duration verification.")
        os.replace(temporary, output)
        await on_progress(100)
        return {
            **source_info, "output_path": str(output), "extension": extension,
            "lossless_from_source": bool(
                (output_format in {"flac", "alac"} and source_info["codec"] in {"flac", "alac", "pcm_s16le", "pcm_s24le"})
                or (output_format == "wav" and source_info["codec"] == "pcm_s16le")
            ),
        }
    except asyncio.TimeoutError as exc:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise AudioError("Audio processing exceeded the time limit.") from exc
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    except OSError as exc:
        raise AudioError("FFmpeg is unavailable or could not start.") from exc
    finally:
        temporary.unlink(missing_ok=True)

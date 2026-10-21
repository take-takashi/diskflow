"""Read-only validation shared by normalization and AAC encoding."""

import hashlib
import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path

from diskflow.image import file_sha256


def check_wav(path: Path, frames: int, pcm_sha256: str | None = None) -> str:
    digest = hashlib.sha256()
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels(),
            audio.getsampwidth(),
            audio.getframerate(),
            audio.getnframes(),
            audio.getcomptype(),
        ) != (2, 2, 44100, frames, "NONE"):
            raise ValueError(f"Unexpected WAV format/frame count: {path.name}")
        remaining = frames
        while remaining:
            count = min(65536, remaining)
            data = audio.readframes(count)
            if len(data) != count * 4:
                raise OSError(f"Short WAV read: {path.name}")
            digest.update(data)
            remaining -= count
    actual = digest.hexdigest()
    if pcm_sha256 is not None and actual != pcm_sha256:
        raise ValueError(f"WAV PCM disagrees with split report: {path.name}")
    return actual


@dataclass(frozen=True)
class VerifiedWavs:
    directory: Path
    tracks: tuple[dict, ...]
    skipped: tuple[str, ...]
    manifest_sha256: str
    operation: str
    normalization: dict | None

    def verify_unchanged(self) -> None:
        if file_sha256(self.directory / "report.json") != self.manifest_sha256:
            raise ValueError(f"Source {self.operation} report changed during processing")
        for entry in self.tracks:
            if file_sha256(self.directory / entry["file"]) != entry["wav_sha256"]:
                raise ValueError(f"Source WAV changed during processing: {entry['file']}")


def load_wavs(directory: Path, *, allow_normalized: bool = True) -> VerifiedWavs:
    directory = directory.resolve()
    raw = (directory / "report.json").read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("Expected a completed WAV output report")
    operation = manifest.get("operation")
    normalized = (operation, manifest.get("status")) == ("normalize", "normalized")
    if (operation, manifest.get("status")) != ("split", "wav_written") and not (
        normalized and allow_normalized
    ):
        raise ValueError("Expected a completed split output (or normalized WAVs for encoding)")
    policy = None
    skipped = []
    if normalized:
        policy = manifest.get("normalization")
        if (
            not isinstance(policy, dict)
            or manifest.get("loudness_normalized") is not True
            or policy.get("method") != "track_gain_peak_limited"
        ):
            raise ValueError("Missing or invalid normalization policy")
        for key in ("target_lufs", "true_peak_dbtp", "peak_headroom_db"):
            value = policy.get(key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid normalization policy limits")
        if not (
            -70 < policy["target_lufs"] < 0
            and -20 <= policy["true_peak_dbtp"] <= 0
            and 0 <= policy["peak_headroom_db"] <= 6
        ):
            raise ValueError("Invalid normalization policy limits")
        skipped = manifest.get("skipped_files", [])
        if not isinstance(skipped, list) or any(name != "pregap.wav" for name in skipped):
            raise ValueError("Invalid skipped files in normalization report")
    entries = manifest.get("tracks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("WAV report contains no tracks")
    tracks = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "complete":
            raise ValueError("WAV report contains an incomplete WAV")
        number = entry.get("number")
        if entry.get("kind") == "pregap" and number == 0 and entry.get("file") == "pregap.wav":
            skipped.append("pregap.wav")
            continue
        if (
            type(number) is not int
            or not 1 <= number <= 99
            or entry.get("kind") != "track"
            or entry.get("file") != f"track-{number:02}.wav"
        ):
            raise ValueError("Invalid track number or filename in WAV report")
        if entry.get("pre_emphasis") is not False:
            raise ValueError(f"Track {number} needs pre-emphasis handling before audio processing")
        frames = entry.get("frames")
        if type(frames) is not int or frames <= 0 or entry.get("pcm_bytes") != frames * 4:
            raise ValueError(f"Invalid PCM frame count on track {number}")
        path = directory / entry["file"]
        if file_sha256(path) != entry.get("wav_sha256"):
            raise ValueError(f"WAV hash disagrees with split report: {path.name}")
        pcm_sha256 = entry.get("pcm_sha256")
        if not isinstance(pcm_sha256, str) or len(pcm_sha256) != 64:
            raise ValueError(f"Missing PCM hash on track {number}")
        check_wav(path, frames, pcm_sha256)
        tracks.append(entry)
    if not tracks or [e["number"] for e in tracks] != list(range(1, len(tracks) + 1)):
        raise ValueError("WAV track numbers must be unique and consecutive from 1")
    return VerifiedWavs(
        directory, tuple(tracks), tuple(skipped), hashlib.sha256(raw).hexdigest(), operation, policy
    )

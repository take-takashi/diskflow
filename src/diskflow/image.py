"""Validate a completed capture before deriving or comparing any audio files."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from diskflow.toc import SECTOR_BYTES, TOC, Track, parse_toc


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@dataclass(frozen=True)
class SavedImage:
    directory: Path
    toc: TOC
    raw_toc: bytes
    sha256: str
    size: int

    @property
    def path(self) -> Path:
        return self.directory / "audio.bin"

    def track_ranges(self) -> list[tuple[Track, int]]:
        ends = [track.start_lba for track in self.toc.tracks[1:]]
        ends.append(self.toc.audio_session().leadout_lba)
        return list(zip(self.toc.tracks, ends, strict=True))

    def verify_data(self) -> None:
        if self.path.stat().st_size != self.size or file_sha256(self.path) != self.sha256:
            raise ValueError("Saved audio.bin size or SHA-256 disagrees with capture report")

    @classmethod
    def load(cls, directory: Path) -> SavedImage:
        directory = directory.resolve()
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if not isinstance(report, dict) or (
            report.get("mode"),
            report.get("status"),
            report.get("data_file"),
        ) != ("rip", "reads_matched", "audio.bin"):
            raise ValueError("Expected a completed rip capture, not a probe or partial image")
        raw_toc = (directory / "toc.bin").read_bytes()
        if hashlib.sha256(raw_toc).hexdigest() != report.get("toc_sha256"):
            raise ValueError("Saved toc.bin SHA-256 disagrees with capture report")
        toc = parse_toc(raw_toc)
        size = toc.audio_session().leadout_lba * SECTOR_BYTES
        if report.get("expected_bytes") != size:
            raise ValueError("Capture report size disagrees with TOC")
        sha256 = report.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError("Capture report has no valid audio SHA-256")
        image = cls(directory, toc, raw_toc, sha256, size)
        image.verify_data()
        return image

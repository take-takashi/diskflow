"""Conservative capture: retain evidence, compare full passes, never conceal errors."""

import hashlib
import json
import os
import platform
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from diskflow.darwin import MAX_READ_SECTORS
from diskflow.toc import SECTOR_BYTES, TOC, parse_toc


class CDReader(Protocol):
    def read_toc(self) -> bytes: ...
    def read_audio(self, lba: int, sectors: int) -> bytes: ...


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_plan(toc: TOC, mode: str, chunk_sectors: int) -> list[tuple[int, int]]:
    session = toc.audio_session()
    if mode == "rip":
        return [
            (lba, min(chunk_sectors, session.leadout_lba - lba))
            for lba in range(0, session.leadout_lba, chunk_sectors)
        ]
    if mode != "probe":
        raise ValueError(f"Unknown mode: {mode}")
    # Sample both sides of each INDEX 01 boundary and the middle of each track.
    positions = set()
    ends = [t.start_lba for t in toc.tracks[1:]] + [session.leadout_lba]
    for track, end in zip(toc.tracks, ends, strict=True):
        positions.update((track.start_lba, (track.start_lba + end) // 2, end - 1))
    return [(lba, 1) for lba in sorted(positions)]


def _read(reader: CDReader, lba: int, count: int, pass_number: int, retries: int, log) -> bytes:
    for attempt in range(1, retries + 2):
        event = {
            "time": now(),
            "pass": pass_number,
            "lba": lba,
            "sectors": count,
            "attempt": attempt,
        }
        try:
            data = reader.read_audio(lba, count)
            if len(data) != count * SECTOR_BYTES:
                raise OSError(f"Short CDDA read at LBA {lba}: {len(data)} bytes")
        except OSError as exc:
            event.update(status="read_error", error=str(exc))
            log.write(json.dumps(event) + "\n")
            log.flush()
            if attempt == retries + 1:
                raise
        else:
            event.update(status="read_ok", sha256=hashlib.sha256(data).hexdigest())
            log.write(json.dumps(event) + "\n")
            log.flush()
            return data
    raise AssertionError("Unreachable")


def capture(
    reader: CDReader,
    output: Path,
    *,
    device: str,
    mode: str,
    chunk_sectors: int = 16,
    passes: int = 2,
    retries: int = 2,
    progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    if not 1 <= chunk_sectors <= MAX_READ_SECTORS or passes < 2 or retries < 0:
        raise ValueError("Use 1..75 sectors per chunk, >=2 passes, and >=0 retries")
    if mode == "probe":
        chunk_sectors = 1
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "report.json"
    report = {
        "status": "running",
        "archival_verified": False,
        "mode": mode,
        "device": device,
        "started_at": now(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "chunk_sectors": chunk_sectors,
        "requested_passes": passes,
        "retries": retries,
        "sector_bytes": SECTOR_BYTES,
        "sector_area": "CDDA user data only (no C2 or subchannels)",
        "pcm_format": "signed 16-bit little-endian stereo, 44100 Hz (macOS CDDA)",
        "verification_scope": "Repeated reads only; drive cache is not bypassed",
        "passes": [],
    }
    write_json(report_path, report)
    try:
        raw_toc = reader.read_toc()
        (output / "toc.bin").write_bytes(raw_toc)
        toc = parse_toc(raw_toc)
        write_json(output / "toc.json", toc.as_dict())
        plan = read_plan(toc, mode, chunk_sectors)
        total_sectors = sum(count for _, count in plan)
        report.update(
            toc_sha256=hashlib.sha256(raw_toc).hexdigest(),
            sectors_per_pass=total_sectors,
            expected_bytes=total_sectors * SECTOR_BYTES,
            coverage="LBA 0 up to (excluding) leadout" if mode == "rip" else "sampled sectors only",
        )
        name = "audio.bin" if mode == "rip" else "samples.bin"
        partial = output / (name + ".part")
        if mode == "probe":
            file_offset = 0
            samples = []
            for lba, count in plan:
                samples.append({"lba": lba, "sectors": count, "file_offset": file_offset})
                file_offset += count * SECTOR_BYTES
            write_json(output / "sample-map.json", {"samples": samples})
        write_json(report_path, report)
        with (
            (output / "reads.jsonl").open("x", encoding="utf-8") as log,
            partial.open("x+b") as image,
        ):
            for pass_number in range(1, passes + 1):
                image.seek(0)
                digest = hashlib.sha256()
                completed = 0
                pass_report = {"number": pass_number, "status": "running", "completed_sectors": 0}
                report["passes"].append(pass_report)
                for lba, count in plan:
                    data = _read(reader, lba, count, pass_number, retries, log)
                    if pass_number == 1:
                        image.write(data)
                    else:
                        expected = image.read(len(data))
                        if data != expected:
                            differing = next(
                                offset
                                for offset in range(0, len(data), SECTOR_BYTES)
                                if data[offset : offset + SECTOR_BYTES]
                                != expected[offset : offset + SECTOR_BYTES]
                            )
                            mismatch_lba = lba + differing // SECTOR_BYTES
                            event = {"status": "mismatch", "pass": pass_number, "lba": mismatch_lba}
                            log.write(json.dumps(event) + "\n")
                            raise OSError(
                                f"Read mismatch in pass {pass_number} at LBA {mismatch_lba}"
                            )
                    digest.update(data)
                    completed += count
                    pass_report["completed_sectors"] = completed
                    if progress:
                        progress(pass_number, completed, total_sectors)
                image.flush()
                os.fsync(image.fileno())
                if reader.read_toc() != raw_toc:
                    raise OSError(f"TOC changed during pass {pass_number}; disc may have changed")
                pass_report.update(status="complete", sha256=digest.hexdigest(), toc_unchanged=True)
                write_json(report_path, report)
        if mode == "rip":
            (output / "disc.cue").write_text(toc.cue(), encoding="utf-8")
        partial.rename(output / name)
        report.update(
            status="reads_matched",
            data_file=name,
            sha256=report["passes"][0]["sha256"],
            finished_at=now(),
        )
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=now())
        for pass_report in report["passes"]:
            if pass_report["status"] == "running":
                pass_report["status"] = "failed"
        write_json(report_path, report)
        raise
    write_json(report_path, report)
    return report

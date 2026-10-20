"""Full cddafs comparison and lossless WAV extraction from a validated image."""

import hashlib
import os
import plistlib
import re
import wave
from array import array
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from diskflow.aiff import read_pcm_info
from diskflow.archive import now, write_json
from diskflow.image import SavedImage, file_sha256
from diskflow.toc import SECTOR_BYTES

CHUNK_BYTES = 1024 * 1024  # A multiple of a stereo PCM frame (4 bytes).
Progress = Callable[[int, int, int], None]


@contextmanager
def _report(output: Path, operation: str, source: Path):
    output.mkdir(parents=True, exist_ok=False)
    path = output / "report.json"
    report = {
        "operation": operation,
        "status": "running",
        "started_at": now(),
        "source_directory": str(source.resolve()),
        "archival_verified": False,
        "tracks": [],
    }
    write_json(path, report)
    try:
        yield report
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        for track in report["tracks"]:
            if track["status"] == "running":
                track["status"] = "failed"
        raise
    finally:
        report["finished_at"] = now()
        write_json(path, report)


def _source_details(report: dict, image: SavedImage) -> None:
    report.update(
        source_sha256=image.sha256,
        source_bytes=image.size,
        toc_sha256=hashlib.sha256(image.raw_toc).hexdigest(),
    )


def _read_exact(source, count: int) -> bytes:
    data = source.read(count)
    if len(data) != count:
        raise OSError(f"Short PCM read: expected {count} bytes, got {len(data)}")
    return data


def _check_mount_toc(mount: Path, image: SavedImage) -> None:
    with (mount / ".TOC.plist").open("rb") as source:
        toc = plistlib.load(source)
    if not isinstance(toc, dict) or toc.get("Format 0x02 TOC Data") != image.raw_toc:
        raise ValueError("Mounted CD TOC disagrees with saved image (or the disc changed)")


def _track_files(mount: Path, image: SavedImage) -> dict[int, Path]:
    files = {}
    for path in mount.iterdir():
        if path.suffix.lower() not in (".aif", ".aiff", ".aifc") or not path.is_file():
            continue
        match = re.match(r"^([0-9]{1,2})\s", path.name)
        if match is None:
            raise ValueError(f"Cannot identify cddafs track number: {path.name}")
        number = int(match[1])
        if number in files:
            raise ValueError(f"Duplicate cddafs track number: {number}")
        files[number] = path
    if set(files) != {track.number for track in image.toc.tracks}:
        raise ValueError("Mounted AIFF track numbers disagree with saved TOC")
    return files


def _little_endian(data: bytes, byte_order: str) -> bytes:
    if byte_order == "little":
        return data
    samples = array("H")
    if samples.itemsize != 2:
        raise RuntimeError("16-bit array elements are required for PCM byte swapping")
    samples.frombytes(data)
    samples.byteswap()
    return samples.tobytes()


def compare_cddafs(
    directory: Path,
    mount: Path,
    output: Path,
    *,
    progress: Progress | None = None,
) -> dict:
    with _report(output, "compare", directory) as report:
        image = SavedImage.load(directory)
        _source_details(report, image)
        mount = mount.resolve()
        report.update(
            mount=str(mount),
            compared_bytes=0,
            uncompared_prefix_sectors=image.toc.tracks[0].start_lba,
            verification_scope=(
                "All TOC track intervals; same drive/OS, not independent secure ripping"
            ),
        )
        _check_mount_toc(mount, image)
        files = _track_files(mount, image)
        with image.path.open("rb") as original:
            for track, end in image.track_ranges():
                entry = {
                    "number": track.number,
                    "status": "running",
                    "file": files[track.number].name,
                    "start_lba": track.start_lba,
                    "end_lba": end,
                    "compared_bytes": 0,
                }
                report["tracks"].append(entry)
                expected_bytes = (end - track.start_lba) * SECTOR_BYTES
                digest = hashlib.sha256()
                with files[track.number].open("rb") as audio:
                    info = read_pcm_info(audio)
                    entry["aiff"] = asdict(info)
                    if info.data_bytes != expected_bytes:
                        raise ValueError(f"Track {track.number} PCM length disagrees with TOC")
                    audio.seek(info.data_offset)
                    original.seek(track.start_lba * SECTOR_BYTES)
                    done = 0
                    while done < expected_bytes:
                        count = min(CHUNK_BYTES, expected_bytes - done)
                        actual = _little_endian(_read_exact(audio, count), info.byte_order)
                        expected = _read_exact(original, count)
                        if actual != expected:
                            offset = next(
                                i
                                for i, pair in enumerate(zip(actual, expected, strict=True))
                                if pair[0] != pair[1]
                            )
                            absolute = track.start_lba * SECTOR_BYTES + done + offset
                            entry["first_mismatch"] = {
                                "lba": absolute // SECTOR_BYTES,
                                "byte_offset_in_sector": absolute % SECTOR_BYTES,
                                "byte_offset_in_track": done + offset,
                            }
                            raise ValueError(
                                f"PCM mismatch on track {track.number} "
                                f"at LBA {absolute // SECTOR_BYTES}"
                            )
                        digest.update(expected)
                        done += count
                        entry["compared_bytes"] = done
                        report["compared_bytes"] += count
                        if progress:
                            progress(track.number, done, expected_bytes)
                _check_mount_toc(mount, image)
                entry.update(status="pcm_matched", pcm_sha256=digest.hexdigest())
                write_json(output / "report.json", report)
        image.verify_data()
        _check_mount_toc(mount, image)
        report["status"] = "pcm_matched"
    return report


def _verify_wav(path: Path, frames: int, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels(),
            audio.getsampwidth(),
            audio.getframerate(),
            audio.getnframes(),
            audio.getcomptype(),
        ) != (2, 2, 44100, frames, "NONE"):
            raise ValueError(f"WAV parameters disagree with source PCM: {path.name}")
        remaining = frames
        while remaining:
            count = min(CHUNK_BYTES // 4, remaining)
            data = audio.readframes(count)
            if len(data) != count * 4:
                raise OSError(f"Short WAV read: {path.name}")
            digest.update(data)
            remaining -= count
    if digest.hexdigest() != expected_sha256:
        raise ValueError(f"WAV PCM hash disagrees with image: {path.name}")


def split_wav(directory: Path, output: Path, *, progress: Progress | None = None) -> dict:
    with _report(output, "split", directory) as report:
        image = SavedImage.load(directory)
        _source_details(report, image)
        report.update(
            pcm_format="signed 16-bit little-endian stereo, 44100 Hz",
            processing="PCM copy only; no gain, resampling, or de-emphasis",
            boundaries="INDEX 01 to next INDEX 01/leadout; INDEX 00 boundaries remain unknown",
        )
        plan = []
        first_start = image.toc.tracks[0].start_lba
        if first_start:
            plan.append(("pregap.wav", 0, 0, first_start, 0))
        plan.extend(
            (f"track-{track.number:02}.wav", track.number, track.start_lba, end, track.control)
            for track, end in image.track_ranges()
        )
        image_digest = hashlib.sha256()
        with image.path.open("rb") as original:
            for name, number, start, end, control in plan:
                expected_bytes = (end - start) * SECTOR_BYTES
                frames = expected_bytes // 4
                entry = {
                    "number": number,
                    "kind": "pregap" if number == 0 else "track",
                    "status": "running",
                    "file": name,
                    "start_lba": start,
                    "end_lba": end,
                    "frames": frames,
                    "pcm_bytes": expected_bytes,
                    "pre_emphasis": bool(control & 1) if number else None,
                }
                report["tracks"].append(entry)
                partial = output / (name + ".part")
                digest = hashlib.sha256()
                original.seek(start * SECTOR_BYTES)
                with partial.open("xb") as destination:
                    with wave.open(destination, "wb") as audio:
                        audio.setparams((2, 2, 44100, frames, "NONE", "not compressed"))
                        done = 0
                        while done < expected_bytes:
                            data = _read_exact(original, min(CHUNK_BYTES, expected_bytes - done))
                            audio.writeframesraw(data)
                            digest.update(data)
                            image_digest.update(data)
                            done += len(data)
                            if progress:
                                progress(number, done, expected_bytes)
                    destination.flush()
                    os.fsync(destination.fileno())
                _verify_wav(partial, frames, digest.hexdigest())
                entry.update(
                    status="pcm_verified",
                    pcm_sha256=digest.hexdigest(),
                    wav_sha256=file_sha256(partial),
                )
                write_json(output / "report.json", report)
            if original.read(1) or image_digest.hexdigest() != image.sha256:
                raise ValueError("Image changed during WAV extraction")
        image.verify_data()
        for entry in report["tracks"]:
            (output / (entry["file"] + ".part")).rename(output / entry["file"])
            entry["status"] = "complete"
        report["status"] = "wav_written"
    return report

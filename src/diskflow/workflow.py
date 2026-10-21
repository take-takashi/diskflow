"""Local album workflow, invoked by the mise import task through Usage variables."""

import json
import math
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from diskflow.aac import encode_aac
from diskflow.archive import capture, now, write_json
from diskflow.darwin import MAX_READ_SECTORS, DarwinCD
from diskflow.derived import split_wav
from diskflow.image import SavedImage, file_sha256
from diskflow.metadata import MAX_COVER_BYTES, _text, cover_info, fetch_metadata
from diskflow.normalize import (
    DEFAULT_PEAK_HEADROOM_DB,
    DEFAULT_TARGET_LUFS,
    DEFAULT_TRUE_PEAK_DBTP,
    normalize_wavs,
)
from diskflow.tagging import tag_album

# Only archive files produced by rip are imported; unrelated files are not copied.
CAPTURE_FILES = ("audio.bin", "toc.bin", "report.json", "toc.json", "disc.cue", "reads.jsonl")
STAGES = (
    ("capture", "capture", "reads_matched"),
    ("metadata", "metadata", "metadata_ready"),
    ("split", "wav", "wav_written"),
    ("normalize", "normalized", "normalized"),
    ("encode", "aac", "aac_encoded"),
    ("tag", "tagged", "tagged"),
)


class WorkflowFailed(RuntimeError):
    def __init__(self, report: dict, exit_code: int):
        super().__init__(report["error"])
        self.report = report
        self.exit_code = exit_code


def _copy_capture(source: Path, output: Path) -> dict:
    SavedImage.load(source)
    output.mkdir()
    for name in CAPTURE_FILES:
        original = source / name
        if original.is_symlink():
            raise ValueError(f"Archive files must not be symlinks: {name}")
        if not original.exists():
            continue
        expected = file_sha256(original)
        with original.open("rb") as src, (output / name).open("xb") as dst:
            shutil.copyfileobj(src, dst)
        if file_sha256(output / name) != expected or file_sha256(original) != expected:
            raise ValueError(f"Archive changed during copy: {name}")
    SavedImage.load(output)
    return json.loads((output / "report.json").read_bytes())


def _artifact(root: Path, path: Path) -> dict:
    if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular file inside output: {path.name}")
    return {
        "file": path.relative_to(root).as_posix(),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def _result(root: Path, tagged: dict) -> dict:
    image = SavedImage.load(root / "capture")
    final = root / "tagged"
    album = _artifact(root, final / "album.json")
    if album["sha256"] != tagged["metadata_manifest_sha256"]:
        raise ValueError("Final album metadata changed")
    artwork = _artifact(root, final / tagged["artwork"]["file"])
    if artwork["sha256"] != tagged["artwork"]["sha256"]:
        raise ValueError("Final cover changed")
    tracks = []
    for entry in tagged["tracks"]:
        name = entry["file"]
        if Path(name).name != name or not name.endswith(".m4a") or entry["status"] != "complete":
            raise ValueError("Invalid final track filename/status")
        audio = _artifact(root, final / name)
        if audio["sha256"] != entry["sha256"] or audio["bytes"] != entry["bytes"]:
            raise ValueError("Final AAC changed")
        tracks.append({"number": entry["number"], "title": entry["title"], **audio})
    archive = [
        _artifact(root, root / "capture" / name)
        for name in CAPTURE_FILES
        if (root / "capture" / name).exists()
    ]
    return {
        "directory": "tagged",
        "album": album,
        "artwork": artwork,
        "tracks": tracks,
        "track_count": len(tracks),
        "archive": archive,
        "source_audio_sha256": image.sha256,
        "loudness_normalized": tagged["loudness_normalized"],
        "all_targets_reached": tagged["all_targets_reached"],
        "metadata_tagged": True,
    }


def import_album(
    output: Path,
    artwork: Path,
    *,
    device: str | None = None,
    saved_capture: Path | None = None,
    apple_album_id: int | None = None,
    release_id: str | None = None,
    edition: str | None = None,
    passes: int = 2,
    retries: int = 2,
    chunk_sectors: int = 16,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    true_peak_dbtp: float = DEFAULT_TRUE_PEAK_DBTP,
    peak_headroom_db: float = DEFAULT_PEAK_HEADROOM_DB,
    stage_progress: Callable[[str, int, int, int], None] | None = None,
) -> dict:
    # Validate arguments and artwork before creating output or touching a CD device.
    if (device is None) == (saved_capture is None):
        raise ValueError("Specify exactly one of --device or --capture")
    if device is not None and not re.fullmatch(r"/dev/rdisk[0-9]+", device):
        raise ValueError("Specify a whole raw CD device, e.g. /dev/rdisk8")
    if (
        type(passes) is not int
        or passes < 2
        or type(retries) is not int
        or retries < 0
        or type(chunk_sectors) is not int
        or not 1 <= chunk_sectors <= MAX_READ_SECTORS
    ):
        raise ValueError("Use >=2 passes, >=0 retries, and 1..75 sectors per chunk")
    if apple_album_id is not None and release_id is not None:
        raise ValueError("Select either --apple-album-id or --release-id")
    if apple_album_id is not None and (type(apple_album_id) is not int or apple_album_id <= 0):
        raise ValueError("Apple album ID must be a positive integer")
    if release_id is not None:
        release_id = str(uuid.UUID(release_id))
    if edition is not None:
        edition = _text(edition, "edition")
    if not (
        math.isfinite(target_lufs)
        and -70 < target_lufs < 0
        and math.isfinite(true_peak_dbtp)
        and -20 <= true_peak_dbtp <= 0
        and math.isfinite(peak_headroom_db)
        and 0 <= peak_headroom_db <= 6
    ):
        raise ValueError("Use finite LUFS (-70..0), peak (-20..0 dBTP), and headroom (0..6 dB)")
    output = output.expanduser()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output must be a NEW directory: {output}")
    root = output.resolve()
    source = saved_capture.expanduser().resolve() if saved_capture is not None else None
    if source is not None:
        if root.is_relative_to(source):
            raise ValueError("Output must not be inside the source capture directory")
        SavedImage.load(source)
    artwork = artwork.expanduser().resolve()
    if not artwork.is_file():
        raise ValueError("--artwork must be a regular JPEG/PNG file")
    with artwork.open("rb") as image_file:
        cover = image_file.read(MAX_COVER_BYTES + 1)
    art = cover_info(cover)
    report = {
        "schema_version": 1,
        "operation": "import",
        "status": "running",
        "started_at": now(),
        "output_directory": str(root),
        "report_file": "report.json",
        "archival_verified": False,
        "verification_scope": (
            "Stage validations only; no independent secure-rip verification "
            "or full cddafs comparison"
        ),
        "source": {
            "kind": "capture" if source is not None else "device",
            "path": str(source) if source is not None else device,
        },
        "parameters": {
            "apple_album_id": apple_album_id,
            "release_id": release_id,
            "edition": edition,
            "passes": passes,
            "retries": retries,
            "chunk_sectors": chunk_sectors,
            "target_lufs": target_lufs,
            "true_peak_dbtp": true_peak_dbtp,
            "peak_headroom_db": peak_headroom_db,
        },
        "artwork_input": {"source_path": str(artwork), **art},
        "stages": [
            {
                "name": name,
                "directory": directory,
                "report": f"{directory}/report.json",
                "status": "pending",
            }
            for name, directory, _ in STAGES
        ],
        "result": None,
    }
    root.mkdir(parents=True, exist_ok=False)
    report_path = root / "report.json"
    write_json(report_path, report)
    current = None

    def stage(name: str, action: Callable[[], dict]) -> dict:
        nonlocal current
        current = next(s for s in report["stages"] if s["name"] == name)
        current.update(status="running", started_at=now())
        write_json(report_path, report)
        if stage_progress:
            stage_progress(name, 0, 0, 0)
        value = action()
        manifest = root / current["report"]
        actual = json.loads(manifest.read_bytes())
        expected = next(expected for key, _, expected in STAGES if key == name)
        if value.get("status") != expected or actual.get("status") != expected:
            raise ValueError(f"Stage did not complete successfully: {name}")
        current.update(
            status="complete",
            operation_status=expected,
            finished_at=now(),
            manifest_sha256=file_sha256(manifest),
        )
        write_json(report_path, report)
        current = None
        return value

    def progress(name: str):
        def update(number: int, done: int, total: int) -> None:
            if stage_progress:
                stage_progress(name, number, done, total)

        return update

    try:
        # Freeze the supplied cover before a potentially long physical read.
        cover_name = "input-cover.jpg" if art["format"] == "JPEG" else "input-cover.png"
        (root / cover_name).write_bytes(cover)
        report["artwork_input"]["file"] = cover_name
        write_json(report_path, report)

        def read_capture() -> dict:
            if source is not None:
                return _copy_capture(source, root / "capture")
            with DarwinCD(device) as reader:
                return capture(
                    reader,
                    root / "capture",
                    device=device,
                    mode="rip",
                    passes=passes,
                    retries=retries,
                    chunk_sectors=chunk_sectors,
                    progress=progress("capture"),
                )

        stage("capture", read_capture)
        metadata = stage(
            "metadata",
            lambda: fetch_metadata(
                root / "capture",
                root / "metadata",
                apple_album_id=apple_album_id,
                release_id=release_id,
                edition=edition,
                artwork=root / cover_name,
            ),
        )
        if metadata["artwork"]["sha256"] != art["sha256"]:
            raise ValueError("Cover snapshot changed during metadata acquisition")
        stage(
            "split", lambda: split_wav(root / "capture", root / "wav", progress=progress("split"))
        )
        stage(
            "normalize",
            lambda: normalize_wavs(
                root / "wav",
                root / "normalized",
                target_lufs=target_lufs,
                true_peak_dbtp=true_peak_dbtp,
                peak_headroom_db=peak_headroom_db,
                progress=progress("normalize"),
            ),
        )
        stage(
            "encode",
            lambda: encode_aac(root / "normalized", root / "aac", progress=progress("encode")),
        )

        def tag_tracks() -> dict:
            if file_sha256(root / "metadata" / "album.json") != metadata["album_sha256"]:
                raise ValueError("Metadata manifest changed before tagging")
            return tag_album(
                root / "aac", root / "metadata", root / "tagged", progress=progress("tag")
            )

        tagged = stage("tag", tag_tracks)
        for entry in report["stages"]:
            if file_sha256(root / entry["report"]) != entry["manifest_sha256"]:
                raise ValueError(f"Stage report changed: {entry['name']}")
        if file_sha256(root / cover_name) != art["sha256"]:
            raise ValueError("Cover snapshot changed")
        report["result"] = _result(root, tagged)
        report.update(status="complete", finished_at=now())
        write_json(report_path, report)
        return report
    except (Exception, KeyboardInterrupt) as exc:
        if current is not None:
            current.update(status="failed", finished_at=now())
        report.update(
            status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=now(), result=None
        )
        write_json(report_path, report)
        raise WorkflowFailed(report, 130 if isinstance(exc, KeyboardInterrupt) else 1) from exc


def main(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    last_progress = 0.0

    def progress(stage: str, number: int, done: int, total: int) -> None:
        nonlocal last_progress
        current = time.monotonic()
        if number == 0 and total == 0:
            print(f"{stage}: started", file=sys.stderr)
            last_progress = current
        elif current - last_progress >= 5 or done == total:
            print(f"{stage}: {number}: {done}/{total}", file=sys.stderr)
            last_progress = current

    def option(name: str) -> str | None:
        return env.get(f"usage_{name}") or None

    try:
        output, artwork = option("output"), option("artwork")
        if output is None or artwork is None:
            raise ValueError("Use mise run import --help; --output and --artwork are required")
        apple_id = option("apple_album_id")
        report = import_album(
            Path(output),
            Path(artwork),
            device=option("device"),
            saved_capture=Path(option("capture")) if option("capture") else None,
            apple_album_id=int(apple_id) if apple_id is not None else None,
            release_id=option("release_id"),
            edition=option("edition"),
            passes=int(option("passes") or "2"),
            retries=int(option("retries") or "2"),
            chunk_sectors=int(option("chunk_sectors") or "16"),
            target_lufs=float(option("target_lufs") or str(DEFAULT_TARGET_LUFS)),
            true_peak_dbtp=float(option("true_peak") or str(DEFAULT_TRUE_PEAK_DBTP)),
            peak_headroom_db=float(option("peak_headroom") or str(DEFAULT_PEAK_HEADROOM_DB)),
            stage_progress=progress,
        )
        code = 0
    except WorkflowFailed as exc:
        report, code = exc.report, exc.exit_code
    except (OSError, ValueError, RuntimeError) as exc:
        report = {
            "schema_version": 1,
            "operation": "import",
            "status": "failed",
            "archival_verified": False,
            "error": f"{type(exc).__name__}: {exc}",
            "result": None,
        }
        code = 1
    except KeyboardInterrupt:
        report = {
            "schema_version": 1,
            "operation": "import",
            "status": "failed",
            "archival_verified": False,
            "error": "KeyboardInterrupt",
            "result": None,
        }
        code = 130
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

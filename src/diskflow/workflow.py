"""User-facing CD import: discover inputs, retain capture plus finished AAC only."""

import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from diskflow.aac import encode_aac
from diskflow.archive import capture, now, write_json
from diskflow.darwin import MAX_READ_SECTORS, DarwinCD
from diskflow.derived import split_wav
from diskflow.discovery import Chooser, discover_cd
from diskflow.image import SavedImage, file_sha256
from diskflow.metadata import MAX_COVER_BYTES, _text, cover_info, fetch_metadata
from diskflow.normalize import (
    DEFAULT_PEAK_HEADROOM_DB,
    DEFAULT_TARGET_LUFS,
    DEFAULT_TRUE_PEAK_DBTP,
    normalize_wavs,
)
from diskflow.reproduction import (
    METADATA_FILES,
    cached_metadata,
    read_recipe,
    resolve_capture,
    reuse_metadata,
    validate_cache,
    write_recipe,
)
from diskflow.tagging import tag_album

CAPTURE_FILES = ("audio.bin", "toc.bin", "report.json", "toc.json", "disc.cue", "reads.jsonl")
STAGES = (
    ("capture", "capture", "reads_matched"),
    ("metadata", "capture/metadata", "metadata_ready"),
    ("split", "logs/split", "wav_written"),
    ("normalize", "logs/normalize", "normalized"),
    ("encode", "logs/encode", "aac_encoded"),
    ("tag", "aac", "tagged"),
)


class WorkflowFailed(RuntimeError):
    def __init__(self, report: dict, exit_code: int):
        super().__init__(report["error"])
        self.report = report
        self.exit_code = exit_code


def default_output() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Path.home() / "Music" / "diskflow" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def _copy_capture(source: Path, output: Path) -> dict:
    SavedImage.load(source)
    output.mkdir()
    for name in (*CAPTURE_FILES, "hints.json"):
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
    final = root / "aac"
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
    archive_paths = [
        root / "capture" / name
        for name in (*CAPTURE_FILES, "hints.json", "recipe.json")
        if (root / "capture" / name).exists()
    ]
    archive_paths += [
        p
        for p in sorted((root / "capture" / "metadata").iterdir())
        if METADATA_FILES.fullmatch(p.name)
    ]
    return {
        "directory": "aac",
        "archive_directory": "capture",
        "album": album,
        "artwork": artwork,
        "tracks": tracks,
        "track_count": len(tracks),
        "archive": [_artifact(root, p) for p in archive_paths],
        "source_audio_sha256": image.sha256,
        "loudness_normalized": tagged["loudness_normalized"],
        "all_targets_reached": tagged["all_targets_reached"],
        "metadata_tagged": True,
    }


def _remove_work_audio(root: Path) -> None:
    """Delete only verified audio created in this new job, never an input/archive."""
    files = []
    for directory, suffix in (("split", "wav"), ("normalize", "wav"), ("encode", "m4a")):
        base = root / "logs" / directory
        if base.is_symlink():
            raise ValueError("作業フォルダがシンボリックリンクへ変更されました。")
        report = json.loads((base / "report.json").read_bytes())
        for entry in report["tracks"]:
            name = entry["file"]
            if not re.fullmatch(rf"track-\d{{2}}\.{suffix}", name) and not (
                directory == "split" and name == "pregap.wav"
            ):
                raise ValueError("削除対象に作業用音声以外のファイルが含まれています。")
            path = base / name
            actual = _artifact(root, path)
            key = "wav_sha256" if suffix == "wav" else "sha256"
            if actual["sha256"] != entry[key]:
                raise ValueError("作業用音声が変更されました。安全のため削除しません。")
            files.append(path)
    for path in files:
        if not path.resolve().is_relative_to(root / "logs") or path.is_symlink():
            raise ValueError("削除先が作業フォルダ外です。")
        path.unlink()


def import_album(
    output: Path | None = None,
    artwork: Path | None = None,
    *,
    device: str | None = None,
    saved_capture: Path | None = None,
    apple_album_id: int | None = None,
    release_id: str | None = None,
    edition: str | None = None,
    passes: int = 2,
    retries: int = 2,
    chunk_sectors: int = 16,
    target_lufs: float | None = None,
    true_peak_dbtp: float | None = None,
    peak_headroom_db: float | None = None,
    query: str | None = None,
    choose: Chooser | None = None,
    query_provider: Callable[[], str | None] | None = None,
    stage_progress: Callable[[str, int, int, int], None] | None = None,
) -> dict:
    if device is not None and saved_capture is not None:
        raise ValueError("Specify only one of --device or a saved capture")
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
    requested = (output or default_output()).expanduser()
    if requested.exists() or requested.is_symlink():
        raise FileExistsError(f"Output must be a NEW directory: {requested}")
    root = requested.resolve()
    source = resolve_capture(saved_capture) if saved_capture is not None else None
    cache, recipe, hints = None, None, {}
    if source is not None:
        if root.is_relative_to(saved_capture.expanduser().resolve()):
            raise ValueError("Output must not be inside the source capture directory")
        image = SavedImage.load(source)
        cache = cached_metadata(source)
        if cache is not None:
            validate_cache(cache, image)
        recipe = read_recipe(source, cache, image)
        if (source / "hints.json").exists():
            hints = json.loads((source / "hints.json").read_bytes())
            if hints.get("toc_sha256") != hashlib.sha256(image.raw_toc).hexdigest():
                raise ValueError("保存されたCD情報のTOCが一致しません。")
    policy = recipe["normalization"] if recipe else {}
    target_lufs = (
        target_lufs if target_lufs is not None else policy.get("target_lufs", DEFAULT_TARGET_LUFS)
    )
    true_peak_dbtp = (
        true_peak_dbtp
        if true_peak_dbtp is not None
        else policy.get("true_peak_dbtp", DEFAULT_TRUE_PEAK_DBTP)
    )
    peak_headroom_db = (
        peak_headroom_db
        if peak_headroom_db is not None
        else policy.get("peak_headroom_db", DEFAULT_PEAK_HEADROOM_DB)
    )
    if not (
        math.isfinite(target_lufs)
        and -70 < target_lufs < 0
        and math.isfinite(true_peak_dbtp)
        and -20 <= true_peak_dbtp <= 0
        and math.isfinite(peak_headroom_db)
        and 0 <= peak_headroom_db <= 6
    ):
        raise ValueError("Use finite LUFS (-70..0), peak (-20..0 dBTP), and headroom (0..6 dB)")
    cover, art = None, None
    # A cached jacket takes precedence over an unrelated cover.jpg in the working directory.
    if artwork is None and cache is None:
        local = [p for p in (Path.cwd() / "cover.jpg", Path.cwd() / "cover.png") if p.exists()]
        if len(local) > 1:
            raise ValueError("cover.jpgとcover.pngが両方あります。使う画像を1枚にしてください。")
        artwork = local[0] if local else None
    if artwork is not None:
        artwork = artwork.expanduser().resolve()
        if not artwork.is_file():
            raise ValueError("カバー画像には通常のJPEG/PNGファイルを指定してください。")
        with artwork.open("rb") as image_file:
            cover = image_file.read(MAX_COVER_BYTES + 1)
        art = cover_info(cover)
    discovery = None
    if source is None and device is None:
        discovery = discover_cd(choose=choose)
        device, hints = discovery["device"], discovery["hints"]
    report = {
        "schema_version": 2,
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
        "artwork_input": {"source_path": str(artwork), **art} if art else None,
        "warnings": [],
        "stages": [
            {"name": n, "directory": d, "report": f"{d}/report.json", "status": "pending"}
            for n, d, _ in STAGES
        ],
        "result": None,
    }
    root.mkdir(parents=True, exist_ok=False)
    (root / "logs").mkdir()
    path = root / "report.json"
    write_json(path, report)
    current = None

    def stage(name: str, action: Callable[[], dict]) -> dict:
        nonlocal current
        current = next(s for s in report["stages"] if s["name"] == name)
        current.update(status="running", started_at=now())
        write_json(path, report)
        if stage_progress:
            stage_progress(name, 0, 0, 0)
        value = action()
        manifest = root / current["report"]
        actual = json.loads(manifest.read_bytes())
        expected = next(e for n, _, e in STAGES if n == name)
        if value.get("status") != expected or actual.get("status") != expected:
            raise ValueError(f"Stage did not complete successfully: {name}")
        current.update(
            status="complete",
            operation_status=expected,
            finished_at=now(),
            manifest_sha256=file_sha256(manifest),
        )
        write_json(path, report)
        current = None
        return value

    def progress(name: str):
        def update(number: int, done: int, total: int) -> None:
            if stage_progress:
                stage_progress(name, number, done, total)

        return update

    try:
        snapshot = None
        if art is not None:
            snapshot = (
                root
                / "logs"
                / ("input-cover.jpg" if art["format"] == "JPEG" else "input-cover.png")
            )
            snapshot.write_bytes(cover)

        def read_capture() -> dict:
            if source is not None:
                return _copy_capture(source, root / "capture")
            with DarwinCD(device) as reader:
                raw_toc = reader.read_toc()
                if (
                    discovery
                    and discovery.get("toc_sha256")
                    and hashlib.sha256(raw_toc).hexdigest() != discovery["toc_sha256"]
                ):
                    raise ValueError("自動検出後にCDが交換されました。再実行してください。")
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
        image = SavedImage.load(root / "capture")
        if (
            discovery
            and discovery.get("toc_sha256")
            and hashlib.sha256(image.raw_toc).hexdigest() != discovery["toc_sha256"]
        ):
            raise ValueError("自動検出後にCDが交換されました。再実行してください。")
        if hints:
            hints["toc_sha256"] = hashlib.sha256(image.raw_toc).hexdigest()
            write_json(root / "capture" / "hints.json", hints)
        meta_dir = root / "capture" / "metadata"

        def obtain_metadata() -> dict:
            if (
                cache is not None
                and apple_album_id is None
                and release_id is None
                and query is None
            ):
                return reuse_metadata(
                    cache, root / "capture", meta_dir, artwork=snapshot, edition=edition
                )
            return fetch_metadata(
                root / "capture",
                meta_dir,
                apple_album_id=apple_album_id,
                release_id=release_id,
                edition=edition,
                artwork=snapshot,
                automatic=True,
                hints=hints,
                query=query,
                choose=choose,
                query_provider=query_provider,
            )

        metadata = stage("metadata", obtain_metadata)
        if art and metadata["artwork"]["sha256"] != art["sha256"]:
            raise ValueError("Cover snapshot changed during metadata acquisition")
        if metadata["artwork"].get("selection") == "provider_default":
            report["warnings"].append(
                "カバーはデータベースの画像です。実物の通常盤・初回盤との一致は未確認です。"
            )
        if report["artwork_input"] is not None:
            report["artwork_input"]["file"] = f"capture/metadata/{metadata['artwork']['file']}"
        stage(
            "split",
            lambda: split_wav(
                root / "capture", root / "logs" / "split", progress=progress("split")
            ),
        )
        stage(
            "normalize",
            lambda: normalize_wavs(
                root / "logs" / "split",
                root / "logs" / "normalize",
                target_lufs=target_lufs,
                true_peak_dbtp=true_peak_dbtp,
                peak_headroom_db=peak_headroom_db,
                progress=progress("normalize"),
            ),
        )
        stage(
            "encode",
            lambda: encode_aac(
                root / "logs" / "normalize", root / "logs" / "encode", progress=progress("encode")
            ),
        )

        def tag_tracks() -> dict:
            if file_sha256(meta_dir / "album.json") != metadata["album_sha256"]:
                raise ValueError("Metadata manifest changed before tagging")
            return tag_album(
                root / "logs" / "encode", meta_dir, root / "aac", progress=progress("tag")
            )

        tagged = stage("tag", tag_tracks)
        for entry in report["stages"]:
            if file_sha256(root / entry["report"]) != entry["manifest_sha256"]:
                raise ValueError(f"Stage report changed: {entry['name']}")
        if snapshot and file_sha256(snapshot) != art["sha256"]:
            raise ValueError("Cover snapshot changed")
        write_recipe(
            root / "capture",
            metadata,
            {
                "target_lufs": target_lufs,
                "true_peak_dbtp": true_peak_dbtp,
                "peak_headroom_db": peak_headroom_db,
            },
        )
        result = _result(root, tagged)
        _remove_work_audio(root)
        if snapshot:
            snapshot.unlink()
        for entry in report["stages"]:
            if entry["name"] in ("split", "normalize", "encode"):
                entry["audio_retained"] = False
        report.update(status="complete", finished_at=now(), result=result)
        write_json(path, report)
        return report
    except (Exception, KeyboardInterrupt) as exc:
        if current is not None:
            current.update(status="failed", finished_at=now())
        report.update(
            status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=now(), result=None
        )
        write_json(path, report)
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

    def choose(prompt: str, candidates: list[dict]) -> int:
        print(prompt, file=sys.stderr)
        for i, item in enumerate(candidates, 1):
            print(f"  {i}. {item['label']}", file=sys.stderr)
        print("番号（空欄で中止）: ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip()
        return int(answer) - 1 if answer.isdecimal() else -1

    def ask_query() -> str | None:
        print(
            "CDの曲情報が未登録です。アーティスト名・アルバム名を入力してください（空欄で中止）: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        return sys.stdin.readline().strip() or None

    try:
        saved = option("source") or option("capture")
        if option("source") and option("capture"):
            raise ValueError("保存フォルダは1つだけ指定してください。")
        interactive = sys.stdin.isatty() and option("non_interactive") != "true"
        apple_id = option("apple_album_id")
        output = Path(option("output")) if option("output") else default_output()
        print(f"保存先: {output}", file=sys.stderr)
        report = import_album(
            output,
            Path(option("artwork")) if option("artwork") else None,
            device=option("device"),
            saved_capture=Path(saved) if saved else None,
            apple_album_id=int(apple_id) if apple_id else None,
            release_id=option("release_id"),
            edition=option("edition"),
            passes=int(option("passes") or "2"),
            retries=int(option("retries") or "2"),
            chunk_sectors=int(option("chunk_sectors") or "16"),
            target_lufs=float(option("target_lufs")) if option("target_lufs") else None,
            true_peak_dbtp=float(option("true_peak")) if option("true_peak") else None,
            peak_headroom_db=float(option("peak_headroom")) if option("peak_headroom") else None,
            query=option("search"),
            choose=choose if interactive else None,
            query_provider=ask_query if interactive else None,
            stage_progress=progress,
        )
        for warning in report["warnings"]:
            print(warning, file=sys.stderr)
        print(f"完成AAC: {Path(report['output_directory']) / 'aac'}", file=sys.stderr)
        print(f"保存用capture: {Path(report['output_directory']) / 'capture'}", file=sys.stderr)
        code = 0
    except WorkflowFailed as exc:
        report, code = exc.report, exc.exit_code
    except (OSError, ValueError, RuntimeError) as exc:
        report = {
            "schema_version": 2,
            "operation": "import",
            "status": "failed",
            "archival_verified": False,
            "error": f"{type(exc).__name__}: {exc}",
            "result": None,
        }
        code = 1
    except KeyboardInterrupt:
        report = {
            "schema_version": 2,
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

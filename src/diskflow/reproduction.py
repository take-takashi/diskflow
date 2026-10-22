"""A capture bundle carries the metadata and policy needed for offline regeneration."""

import hashlib
import json
import platform
import re
import shutil
from importlib.metadata import version
from pathlib import Path

from diskflow.archive import write_json
from diskflow.derived import operation_report
from diskflow.image import SavedImage, file_sha256
from diskflow.metadata import MetadataClient, _artwork, _text, disc_id, validate_tracks
from diskflow.tagging import _load_album

METADATA_FILES = re.compile(
    r"(?:album\.json|report\.json|cover\.(?:jpg|png)|requests\.jsonl|candidates\.json|"
    r"apple(?:-\d+|-search)?\.json|musicbrainz-(?:disc|release)\.json)(?:\.error)?"
)


def _bundle_files(capture: Path) -> list[Path]:
    paths = [
        capture / n
        for n in (
            "audio.bin",
            "toc.bin",
            "report.json",
            "toc.json",
            "disc.cue",
            "reads.jsonl",
            "hints.json",
        )
        if (capture / n).exists()
    ]
    paths += [
        p for p in sorted((capture / "metadata").iterdir()) if METADATA_FILES.fullmatch(p.name)
    ]
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError("captureには通常のファイルだけを保存できます。")
    return paths


def resolve_capture(path: Path) -> Path:
    path = path.expanduser().resolve()
    if (path / "capture" / "audio.bin").is_file():
        return path / "capture"
    return path


def cached_metadata(capture: Path) -> Path | None:
    if (capture / "metadata").exists():
        directory = capture / "metadata"
        report = directory / "report.json"
        if report.is_file() and not (capture / "recipe.json").exists():
            if json.loads(report.read_bytes()).get("status") in ("failed", "running"):
                return None
        return directory
    # Import v1 put metadata next to capture. Do not scan unrelated albums.
    parent_report = capture.parent / "report.json"
    if capture.name == "capture" and parent_report.is_file():
        parent = json.loads(parent_report.read_bytes())
        if parent.get("operation") == "import" and (capture.parent / "metadata").is_dir():
            directory = capture.parent / "metadata"
            report = directory / "report.json"
            if report.is_file() and json.loads(report.read_bytes()).get("status") in (
                "failed",
                "running",
            ):
                return None
            return directory
    return None


def validate_cache(directory: Path, image: SavedImage) -> tuple[dict, bytes, str]:
    album, cover, digest = _load_album(directory)
    if album["source_toc_sha256"] != hashlib.sha256(image.raw_toc).hexdigest():
        raise ValueError("保存された曲情報はこのcaptureのTOCと一致しません。")
    if album["musicbrainz_disc_id"] != disc_id(image.toc):
        raise ValueError("保存された曲情報のDisc IDが一致しません。")
    frames = [t["frames"] for t in album["tracks"]]
    validate_tracks(image.toc, album["tracks"])
    if frames != [t["frames"] for t in album["tracks"]]:
        raise ValueError("保存された曲情報のフレーム数が一致しません。")
    manifest = json.loads((directory / "report.json").read_bytes())
    if manifest.get("status") != "metadata_ready" or manifest.get("album_sha256") != digest:
        raise ValueError("保存されたメタデータのレポートまたはハッシュが不正です。")
    return album, cover, digest


def read_recipe(capture: Path, metadata: Path | None, image: SavedImage) -> dict | None:
    path = capture / "recipe.json"
    if not path.exists():
        return None
    recipe = json.loads(path.read_bytes())
    if not isinstance(recipe, dict) or recipe.get("schema_version") != 1 or metadata is None:
        raise ValueError("再生成用のrecipeまたは曲情報が不完全です。")
    album, _, digest = validate_cache(metadata, image)
    files = recipe.get("files")
    actual = {p.relative_to(capture).as_posix(): file_sha256(p) for p in _bundle_files(capture)}
    if not isinstance(files, dict) or actual != files:
        raise ValueError("capture内のファイル一覧またはハッシュが一致しません。")
    if (
        recipe.get("audio_sha256") != image.sha256
        or recipe.get("toc_sha256") != hashlib.sha256(image.raw_toc).hexdigest()
        or recipe.get("metadata_sha256") != digest
        or recipe.get("artwork_sha256") != album["artwork"]["sha256"]
    ):
        raise ValueError("capture・曲情報・画像と再生成用recipeのハッシュが一致しません。")
    policy = recipe.get("normalization")
    if not isinstance(policy, dict) or any(
        type(policy.get(k)) not in (float, int)
        for k in ("target_lufs", "true_peak_dbtp", "peak_headroom_db")
    ):
        raise ValueError("再生成用の音量設定が不正です。")
    if recipe.get("encoding") != {
        "codec": "Apple AAC",
        "bitrate_bps": 256000,
        "strategy": 2,
        "quality": 127,
    }:
        raise ValueError("このバージョンで再生成できないAAC設定です。")
    return recipe


def reuse_metadata(
    source: Path,
    capture: Path,
    output: Path,
    *,
    artwork: Path | None,
    edition: str | None,
) -> dict:
    image = SavedImage.load(capture)
    album, cover, digest = validate_cache(source, image)
    with operation_report(output, "metadata", capture) as report:
        for path in source.iterdir():
            if METADATA_FILES.fullmatch(path.name) and path.name not in (
                "album.json",
                "report.json",
                "cover.jpg",
                "cover.png",
            ):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("メタデータには通常のファイルだけを保存できます。")
                shutil.copyfile(path, output / path.name)
        if artwork is not None:
            previous = album["artwork"]["sha256"]
            album["artwork"] = _artwork(MetadataClient(output), None, local=artwork, url=None)
            if previous != album["artwork"]["sha256"] and edition is None:
                album.pop("edition", None)
        else:
            (output / album["artwork"]["file"]).write_bytes(cover)
        if edition is not None:
            album["edition"] = {
                "label": _text(edition, "edition"),
                "basis": "user-specified",
                "note": "Packaging/pressing is not inferred from the CD TOC",
            }
        write_json(output / "album.json", album)
        if file_sha256(source / "album.json") != digest:
            raise ValueError("再利用元のメタデータが処理中に変更されました。")
        report.update(
            status="metadata_ready",
            album=album["title"],
            artist=album["artist"],
            provider=album["source"]["provider"],
            artwork=album["artwork"],
            source_metadata_sha256=digest,
            metadata_reused=True,
            album_sha256=file_sha256(output / "album.json"),
            track_count=len(album["tracks"]),
        )
    return report


def write_recipe(capture: Path, metadata: dict, policy: dict) -> None:
    image = SavedImage.load(capture)
    write_json(
        capture / "recipe.json",
        {
            "schema_version": 1,
            "audio_sha256": image.sha256,
            "toc_sha256": hashlib.sha256(image.raw_toc).hexdigest(),
            "metadata_sha256": metadata["album_sha256"],
            "artwork_sha256": metadata["artwork"]["sha256"],
            "normalization": policy,
            "files": {
                p.relative_to(capture).as_posix(): file_sha256(p) for p in _bundle_files(capture)
            },
            "encoding": {
                "codec": "Apple AAC",
                "bitrate_bps": 256000,
                "strategy": 2,
                "quality": 127,
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "pyav": version("av"),
                "mutagen": version("mutagen"),
            },
            "reproducibility": "Same source PCM, metadata and settings; not byte-identical AAC",
        },
    )

"""Tag copied M4A files and prove AAC packets, codec configuration, and timing are unchanged."""

import hashlib
import json
import re
import shutil
import unicodedata
from pathlib import Path

from diskflow.archive import write_json
from diskflow.derived import Progress, operation_report
from diskflow.image import file_sha256
from diskflow.metadata import _text, cover_info


def track_filename(number: int, title: str) -> str:
    title = _text(title, "track title")
    safe = re.sub(r'[<>:"/\\|?*]', "_", unicodedata.normalize("NFC", title))
    safe = safe.encode("utf-8")[:180].decode("utf-8", errors="ignore").strip(" .") or "Track"
    return f"{number:02} {safe}.m4a"


def audio_signature(path: Path) -> dict:
    import av

    payload = hashlib.sha256()
    timing = hashlib.sha256()
    count = 0
    try:
        with av.open(str(path)) as source:
            if len(source.streams.audio) != 1:
                raise ValueError("Expected one AAC audio stream")
            stream = source.streams.audio[0]
            codec = stream.codec_context
            if codec.name != "aac" or codec.sample_rate != 44100 or len(codec.layout.channels) != 2:
                raise ValueError("Expected 44100 Hz stereo AAC")
            result = {
                "codec": codec.name,
                "sample_rate": codec.sample_rate,
                "channels": 2,
                "duration": stream.duration,
                "time_base": str(stream.time_base),
                "codec_config_sha256": hashlib.sha256(codec.extradata or b"").hexdigest(),
            }
            for packet in source.demux(stream):
                if packet.size == 0:
                    continue
                data = bytes(packet)
                payload.update(len(data).to_bytes(8, "big"))
                payload.update(data)
                timing.update(
                    json.dumps(
                        [packet.pts, packet.dts, packet.duration, str(packet.time_base)]
                    ).encode("ascii")
                    + b"\n"
                )
                count += 1
    except av.FFmpegError as exc:
        raise ValueError(f"Cannot inspect AAC packets: {path.name}: {exc}") from exc
    if not count:
        raise ValueError("M4A contains no AAC packets")
    return result | {
        "packets": count,
        "packet_sha256": payload.hexdigest(),
        "timing_sha256": timing.hexdigest(),
    }


def _source_toc(directory: Path, report: dict) -> str:
    """Verify the saved manifest chain: encode -> [normalize ->] split -> TOC hash."""
    seen = {directory.resolve()}
    for _ in range(4):
        if not isinstance(report.get("source_directory"), str):
            raise ValueError("Missing source directory in audio provenance")
        source = Path(report["source_directory"]).resolve()
        if source in seen:
            raise ValueError("Cycle in audio source provenance")
        seen.add(source)
        raw = (source / "report.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != report.get("source_manifest_sha256"):
            raise ValueError("Source provenance report changed")
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise ValueError("Invalid source provenance report")
        if (report.get("operation"), report.get("status")) == ("split", "wav_written"):
            value = report.get("toc_sha256")
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Missing TOC hash in source provenance")
            return value
        if (report.get("operation"), report.get("status")) != ("normalize", "normalized"):
            raise ValueError("Unexpected audio source provenance")
    raise ValueError("Audio source provenance is too deep")


def _load_album(directory: Path) -> tuple[dict, bytes, str]:
    raw = (directory / "album.json").read_bytes()
    album = json.loads(raw)
    if not isinstance(album, dict) or album.get("schema_version") != 1:
        raise ValueError("Invalid album metadata schema")
    for field in (
        "title",
        "artist",
        "album_artist",
        "date",
        "source_toc_sha256",
        "musicbrainz_disc_id",
    ):
        _text(album.get(field), field)
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", album["date"]):
        raise ValueError("Invalid album release date")
    if not isinstance(album.get("source"), dict) or album["source"].get("provider") not in (
        "apple",
        "musicbrainz",
    ):
        raise ValueError("Invalid album metadata provider")
    _text(album["source"].get("url"), "metadata source URL")
    edition = album.get("edition")
    if edition is not None:
        if not isinstance(edition, dict) or edition.get("basis") != "user-specified":
            raise ValueError("Invalid user-specified edition")
        _text(edition.get("label"), "edition")
    for field in ("disc_number", "disc_total"):
        if type(album.get(field)) is not int or not 1 <= album[field] <= 99:
            raise ValueError("Invalid album disc number/count")
    if album["disc_number"] > album["disc_total"]:
        raise ValueError("Invalid album disc range")
    tracks = album.get("tracks")
    if not isinstance(tracks, list) or not tracks or len(tracks) > 99:
        raise ValueError("Invalid album track list")
    for number, track in enumerate(tracks, 1):
        if (
            not isinstance(track, dict)
            or type(track.get("number")) is not int
            or track["number"] != number
            or type(track.get("frames")) is not int
            or track["frames"] <= 0
        ):
            raise ValueError("Invalid album track number/frame count")
        _text(track.get("title"), "title")
        _text(track.get("artist"), "artist")
    art = album.get("artwork")
    if not isinstance(art, dict) or art.get("file") not in ("cover.jpg", "cover.png"):
        raise ValueError("Invalid album cover filename")
    selection = art.get("selection")
    if selection not in (None, "provider_default", "explicit_url", "explicit_file"):
        raise ValueError("Invalid artwork selection")
    if edition is not None and selection not in ("explicit_url", "explicit_file"):
        raise ValueError("A user-specified edition requires explicitly selected artwork")
    data = (directory / art["file"]).read_bytes()
    actual = cover_info(data)
    if any(actual[key] != art.get(key) for key in actual):
        raise ValueError("Cover image disagrees with metadata")
    return album, data, hashlib.sha256(raw).hexdigest()


def _write_tags(path: Path, album: dict, track: dict, cover: bytes) -> None:
    from mutagen import MutagenError
    from mutagen.mp4 import MP4, MP4Cover

    expected = {
        "\xa9nam": [track["title"]],
        "\xa9ART": [track["artist"]],
        "aART": [album["album_artist"]],
        "\xa9alb": [album["title"]],
        "\xa9day": [album["date"]],
        "trkn": [(track["number"], len(album["tracks"]))],
        "disk": [(album["disc_number"], album["disc_total"])],
        "----:com.apple.iTunes:diskflow_metadata_source": [
            json.dumps(album["source"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ],
        "----:com.apple.iTunes:MusicBrainz Disc Id": [album["musicbrainz_disc_id"].encode("ascii")],
    }
    artwork_source = {
        key: album["artwork"][key]
        for key in (
            "selection",
            "source_url",
            "source_name",
            "provider_source_url",
            "sha256",
            "format",
            "width",
            "height",
            "selected_at",
        )
        if key in album["artwork"]
    }
    artwork_source.setdefault("selection", "legacy_unspecified")
    expected["----:com.apple.iTunes:diskflow_artwork_source"] = [
        json.dumps(artwork_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ]
    if album.get("edition") is not None:
        expected["----:com.apple.iTunes:diskflow_edition"] = [
            album["edition"]["label"].encode("utf-8")
        ]
    if album["source"]["provider"] == "musicbrainz":
        expected["----:com.apple.iTunes:MusicBrainz Album Id"] = [
            album["source"]["id"].encode("ascii")
        ]
    for field, key in (("genre", "\xa9gen"), ("copyright", "cprt")):
        if album.get(field):
            expected[key] = [_text(album[field], field)]
    image_format = (
        MP4Cover.FORMAT_JPEG if album["artwork"]["format"] == "JPEG" else MP4Cover.FORMAT_PNG
    )
    try:
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        # Preserve unrelated atoms, including any encoder/gapless information.
        for key, value in expected.items():
            audio[key] = value
        audio["covr"] = [MP4Cover(cover, imageformat=image_format)]
        audio.save()
        checked = MP4(path)
        if any(checked.get(key) != value for key, value in expected.items()):
            raise ValueError("Written MP4 tags did not round-trip")
        covers = checked.get("covr", [])
        if len(covers) != 1 or bytes(covers[0]) != cover or covers[0].imageformat != image_format:
            raise ValueError("Written MP4 cover did not round-trip")
    except MutagenError as exc:
        raise ValueError(f"MP4 tag processing failed: {exc}") from exc


def tag_album(
    directory: Path, metadata: Path, output: Path, *, progress: Progress | None = None
) -> dict:
    directory, metadata = directory.resolve(), metadata.resolve()
    with operation_report(output, "tag", directory) as report:
        raw = (directory / "report.json").read_bytes()
        source_report = json.loads(raw)
        if not isinstance(source_report, dict) or (
            source_report.get("operation"),
            source_report.get("status"),
        ) != ("encode", "aac_encoded"):
            raise ValueError("Tag input must be a completed AAC encode output")
        tracks = source_report.get("tracks")
        if not isinstance(tracks, list) or not tracks or len(tracks) > 99:
            raise ValueError("Invalid encoded track list")
        album, cover, metadata_sha256 = _load_album(metadata)
        toc_sha256 = _source_toc(directory, source_report)
        if toc_sha256 != album["source_toc_sha256"] or len(tracks) != len(album["tracks"]):
            raise ValueError("Album metadata does not belong to this encoded CD")
        report.update(
            album=album["title"],
            artist=album["artist"],
            metadata_source=album["source"],
            edition=album.get("edition"),
            artwork=album["artwork"],
            metadata_manifest_sha256=metadata_sha256,
            source_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            source_toc_sha256=toc_sha256,
            metadata_tagged=True,
            loudness_normalized=source_report.get("loudness_normalized", False),
            all_targets_reached=source_report.get("all_targets_reached"),
            source_normalization=source_report.get("source_normalization"),
            verification_scope=(
                "MP4 tags/cover and unchanged AAC packets, codec configuration and timing"
            ),
        )
        for number, (encoded, track) in enumerate(zip(tracks, album["tracks"], strict=True), 1):
            if (
                not isinstance(encoded, dict)
                or encoded.get("status") != "complete"
                or encoded.get("number") != number
                or encoded.get("file") != f"track-{number:02}.m4a"
                or encoded.get("source_frames") != track["frames"]
            ):
                raise ValueError("Encoded track numbers/durations disagree with album metadata")
            source = directory / encoded["file"]
            if file_sha256(source) != encoded.get("sha256"):
                raise ValueError("Source AAC changed or disagrees with encode report")
            name = track_filename(number, track["title"])
            partial = output / (name + ".part")
            entry = {
                "number": number,
                "status": "running",
                "title": track["title"],
                "artist": track["artist"],
                "file": name,
                "source_file": source.name,
                "source_sha256": encoded["sha256"],
                "frames": track["frames"],
            }
            report["tracks"].append(entry)
            before = audio_signature(source)
            shutil.copyfile(source, partial)
            if file_sha256(partial) != encoded["sha256"]:
                raise ValueError("AAC copy disagrees with source")
            _write_tags(partial, album, track, cover)
            after = audio_signature(partial)
            if before != after:
                raise ValueError(
                    f"AAC packets, timing or codec changed while tagging track {number}"
                )
            entry.update(
                status="verified",
                sha256=file_sha256(partial),
                bytes=partial.stat().st_size,
                audio_signature=after,
                audio_unchanged=True,
                artwork_sha256=album["artwork"]["sha256"],
                decoded_loudness=encoded.get("decoded_loudness"),
                loudness_metrics_inherited=encoded.get("decoded_loudness") is not None,
            )
            if progress:
                progress(number, 1, 1)
            write_json(output / "report.json", report)
        if (
            file_sha256(directory / "report.json") != report["source_manifest_sha256"]
            or file_sha256(metadata / "album.json") != metadata_sha256
            or file_sha256(metadata / album["artwork"]["file"]) != album["artwork"]["sha256"]
        ):
            raise ValueError("Source audio/metadata manifest changed during tagging")
        for encoded in tracks:
            if file_sha256(directory / encoded["file"]) != encoded["sha256"]:
                raise ValueError("Source AAC changed during tagging")
        write_json(output / "album.json", album)
        (output / album["artwork"]["file"]).write_bytes(cover)
        for entry in report["tracks"]:
            (output / (entry["file"] + ".part")).rename(output / entry["file"])
            entry["status"] = "complete"
        report["status"] = "tagged"
    return report

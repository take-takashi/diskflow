"""Read audio CDs and prepare local audio files and verification reports."""

import argparse
import json
import plistlib
import subprocess
import sys
import time
import wave
from pathlib import Path

from diskflow.aac import encode_aac
from diskflow.archive import capture, now
from diskflow.darwin import DarwinCD
from diskflow.derived import compare_cddafs, split_wav
from diskflow.toc import parse_toc


def _plist_command(command: list[str], *, empty_result=None):
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=30)
        if not result.stdout.strip() and empty_result is not None:
            return empty_result
        return plistlib.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}


def _json_default(value):
    if isinstance(value, bytes):
        return {"encoding": "hex", "data": value.hex()}
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def inspect_mount(mount: Path) -> dict:
    toc_path = mount / ".TOC.plist"
    with toc_path.open("rb") as source:
        plist = plistlib.load(source)
    result = {
        "mount": str(mount),
        "disk_info": _plist_command(["/usr/sbin/diskutil", "info", "-plist", str(mount)]),
        "toc_plist": plist,
        "track_files": [
            {"name": path.name, "bytes": path.stat().st_size}
            for path in sorted(mount.iterdir())
            if path.suffix.lower() in (".aif", ".aiff", ".aifc") and path.is_file()
        ],
    }
    raw = plist.get("Format 0x02 TOC Data")
    if isinstance(raw, bytes):
        try:
            result["parsed_toc"] = parse_toc(raw).as_dict()
        except ValueError as exc:
            result["toc_parse_error"] = str(exc)
    return result


def inspect(mount: Path | None = None) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("Hardware discovery requires macOS")
    mounts = (
        [mount]
        if mount
        else sorted(path for path in Path("/Volumes").iterdir() if (path / ".TOC.plist").is_file())
    )
    return {
        "created_at": now(),
        "diskutil_list": _plist_command(["/usr/sbin/diskutil", "list", "-plist"]),
        "cd_media": _plist_command(
            ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOCDMedia"], empty_result=[]
        ),
        "audio_cd_mount_count": len(mounts),
        "mounts": [inspect_mount(path) for path in mounts],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="macOS audio CD capture and local WAV/AAC preparation",
        epilog="Files and reports are saved locally. Uploads belong in the calling application.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("inspect", help="Inspect devices and mounted .TOC.plist files")
    discovery.add_argument("mount", nargs="?", type=Path, help="Optional audio CD mount path")
    discovery.add_argument("--output", type=Path, help="Save JSON to a NEW file")
    for name, help_text in (
        ("probe", "Read boundary/midpoint samples at least twice"),
        ("rip", "Read the single-session audio image at least twice"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("device", help="Whole raw CD device, e.g. /dev/rdisk8")
        command.add_argument("--output", required=True, type=Path, help="NEW output directory")
        command.add_argument("--passes", type=int, default=2, help="Full read passes (>=2)")
        command.add_argument("--retries", type=int, default=2, help="Extra attempts per I/O error")
        if name == "rip":
            command.add_argument(
                "--chunk-sectors", type=int, default=16, help="Sectors per read (1..75)"
            )
    for name, help_text in (
        ("compare", "Compare every track's PCM with a mounted cddafs CD"),
        ("split", "Extract lossless track WAVs from a completed image (no CD needed)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("capture", type=Path, help="Directory containing a completed rip")
        command.add_argument("--output", required=True, type=Path, help="NEW output directory")
        if name == "compare":
            command.add_argument("--mount", required=True, type=Path, help="Mounted audio CD path")
    normalization = commands.add_parser(
        "normalize", help="Per-track fixed gain, limited by true peak"
    )
    normalization.add_argument(
        "wavs", type=Path, help="Directory containing an ORIGINAL split output"
    )
    normalization.add_argument("--output", required=True, type=Path, help="NEW output directory")
    normalization.add_argument(
        "--target-lufs", type=float, help="Integrated LUFS target (default -14)"
    )
    normalization.add_argument(
        "--true-peak", type=float, help="True peak ceiling in dBTP (default -1)"
    )
    normalization.add_argument(
        "--peak-headroom",
        type=float,
        help="Extra WAV peak headroom in dB (default 0.05); increase if AAC overshoots",
    )
    encoding = commands.add_parser("encode", help="Apple AAC 256 kbps from verified WAVs (no tags)")
    encoding.add_argument("wavs", type=Path, help="Directory containing split or normalized WAVs")
    encoding.add_argument("--output", required=True, type=Path, help="NEW output directory")
    metadata = commands.add_parser("metadata", help="Fetch TOC-matched album metadata and cover")
    metadata.add_argument("capture", type=Path, help="Completed CD rip directory")
    metadata.add_argument("--output", required=True, type=Path, help="NEW output directory")
    provider = metadata.add_mutually_exclusive_group()
    provider.add_argument("--apple-album-id", type=int, help="Explicit Apple album ID fallback")
    provider.add_argument(
        "--release-id", help="Select a MusicBrainz release from the Disc ID candidates"
    )
    metadata.add_argument(
        "--edition", help="User-specified edition label; requires --artwork or --artwork-url"
    )
    artwork = metadata.add_mutually_exclusive_group()
    artwork.add_argument(
        "--artwork", type=Path, help="Use a local JPEG/PNG instead of provider artwork"
    )
    artwork.add_argument(
        "--artwork-url", help="Use this HTTPS JPEG/PNG URL instead of provider artwork"
    )
    tagging = commands.add_parser("tag", help="Embed album tags/cover without re-encoding AAC")
    tagging.add_argument("aac", type=Path, help="Completed encode output directory")
    tagging.add_argument("--metadata", required=True, type=Path, help="Metadata output directory")
    tagging.add_argument("--output", required=True, type=Path, help="NEW output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            text = json.dumps(
                inspect(args.mount), ensure_ascii=False, indent=2, default=_json_default
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as destination:
                    destination.write(text + "\n")
            print(text)
            return 0
        last_progress = 0.0

        def progress(pass_number: int, completed: int, total: int) -> None:
            nonlocal last_progress
            current = time.monotonic()
            if current - last_progress >= 5 or completed == total:
                if args.command in ("encode", "normalize"):
                    label, unit = "track", "frames"
                elif args.command == "tag":
                    label, unit = "track", "file"
                elif args.command in ("compare", "split"):
                    label, unit = "track", "bytes"
                else:
                    label, unit = "pass", "sectors"
                print(f"{label} {pass_number}: {completed}/{total} {unit}", file=sys.stderr)
                last_progress = current

        if args.command == "metadata":
            from diskflow.metadata import fetch_metadata

            report = fetch_metadata(
                args.capture,
                args.output,
                apple_album_id=args.apple_album_id,
                release_id=args.release_id,
                artwork=args.artwork,
                artwork_url=args.artwork_url,
                edition=args.edition,
            )
            print(f"{report['status']}: {args.output / 'album.json'}")
            print(f"{report['artist']} / {report['album']}")
            if report.get("edition"):
                print(f"Edition (user-specified): {report['edition']['label']}")
            print(f"Artwork selection: {report['artwork'].get('selection', 'legacy_unspecified')}")
            return 0
        if args.command == "tag":
            from diskflow.tagging import tag_album

            report = tag_album(args.aac, args.metadata, args.output, progress=progress)
            print(f"{report['status']}: {args.output / 'report.json'}")
            print(
                "AAC packets, codec configuration and timing are unchanged; source files retained."
            )
            return 0
        if args.command == "normalize":
            # Do not import the optional-to-reading native audio backend for CD-only commands.
            from diskflow.normalize import normalize_wavs

            options = {
                key: value
                for key, value in (
                    ("target_lufs", args.target_lufs),
                    ("true_peak_dbtp", args.true_peak),
                    ("peak_headroom_db", args.peak_headroom),
                )
                if value is not None
            }
            report = normalize_wavs(args.wavs, args.output, progress=progress, **options)
            print(f"{report['status']}: {args.output / 'report.json'}")
            unmet = sum(not track["target_reached"] for track in report["tracks"])
            print(
                f"Fixed Track Gain applied; {unmet} track(s) did not reach the LUFS target. "
                "No limiter/compression; original WAVs are unchanged."
            )
            return 0
        if args.command == "encode":
            report = encode_aac(args.wavs, args.output, progress=progress)
            print(f"{report['status']}: {args.output / 'report.json'}")
            if report["loudness_normalized"]:
                print(
                    "Normalized WAVs encoded; decoded AAC true peak checked. Metadata tags not set."
                )
            else:
                print(
                    "AAC baseline only; loudness normalization and metadata tagging "
                    "are NOT applied."
                )
            return 0
        if args.command in ("compare", "split"):
            if args.command == "compare":
                report = compare_cddafs(args.capture, args.mount, args.output, progress=progress)
            else:
                report = split_wav(args.capture, args.output, progress=progress)
            print(f"{report['status']}: {args.output / 'report.json'}")
            print("Original image is unchanged; archival accuracy is NOT yet verified.")
            return 0
        with DarwinCD(args.device) as reader:
            report = capture(
                reader,
                args.output,
                device=args.device,
                mode=args.command,
                passes=args.passes,
                retries=args.retries,
                chunk_sectors=getattr(args, "chunk_sectors", 1),
                progress=progress,
            )
        print(f"{report['status']}: {args.output / 'report.json'}")
        print("Repeated reads matched; archival accuracy is NOT yet verified.")
        return 0
    except KeyboardInterrupt:
        print("Interrupted; incomplete output is retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, wave.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.command in (
            "probe",
            "rip",
            "compare",
            "split",
            "normalize",
            "encode",
            "metadata",
            "tag",
        ):
            print(f"If capture started, evidence is in {args.output}", file=sys.stderr)
        return 1

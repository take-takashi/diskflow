"""Apple AAC encoding from verified raw or normalized WAVs, without source edits."""

import json
import math
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from diskflow.archive import now, write_json
from diskflow.derived import Progress, operation_report
from diskflow.image import file_sha256
from diskflow.wavs import check_wav, load_wavs

AFCONVERT = "/usr/bin/afconvert"
AFINFO = "/usr/bin/afinfo"
NS = "{http://apple.com/core_audio/audio_info}"


def _run(argv: list[str], log, *, timeout: int = 600) -> str:
    event = {"started_at": now(), "argv": argv}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        event.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if result.returncode:
            raise RuntimeError(
                f"{Path(argv[0]).name} exited {result.returncode}: {result.stderr.strip()}"
            )
        event["status"] = "complete"
        return result.stdout
    except subprocess.TimeoutExpired as exc:
        event.update(status="failed", error=str(exc))
        raise RuntimeError(f"{Path(argv[0]).name} timed out after {timeout}s") from exc
    except (Exception, KeyboardInterrupt) as exc:
        event.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        event["finished_at"] = now()
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
        log.flush()


def _parse_afinfo(xml: str, expected_frames: int) -> dict:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("afinfo returned invalid XML") from exc
    files = root.findall(f"{NS}audio_file")
    if root.tag != NS + "audio_info" or len(files) != 1:
        raise ValueError("Expected exactly one audio file in afinfo XML")
    tracks = files[0].findall(f"{NS}tracks/{NS}track")
    if files[0].findtext(NS + "file_type", "").strip(" '\t\n") != "m4af" or len(tracks) != 1:
        raise ValueError("Expected an M4A container with exactly one audio track")
    track = tracks[0]

    def text(name: str) -> str:
        found = track.findtext("/".join(NS + part for part in name.split("/")))
        if found is None:
            raise ValueError(f"afinfo XML is missing {name}")
        return found.strip()

    channels = int(text("num_channels"))
    sample_rate = float(text("sample_rate"))
    codec = text("format_type")
    if (channels, sample_rate, codec) != (2, 44100, "aac"):
        raise ValueError("Encoded output is not 44100 Hz stereo AAC")
    packet = {
        name: int(text("packet_table_info/" + name))
        for name in ("total_frames", "valid_frames", "priming_frames", "remainder_frames")
    }
    if (
        packet["valid_frames"] != expected_frames
        or any(v < 0 for v in packet.values())
        or packet["total_frames"]
        != sum(packet[n] for n in ("valid_frames", "priming_frames", "remainder_frames"))
    ):
        raise ValueError("AAC valid frames or priming/padding disagree with the input")
    duration = float(text("duration"))
    if not math.isfinite(duration) or abs(duration - expected_frames / 44100) > 1 / 44100:
        raise ValueError("AAC duration disagrees with input frame count")
    alerts = [
        {
            "level": a.get("level"),
            "code": a.get("code"),
            "description": a.findtext(NS + "description", "").strip(),
        }
        for a in root.iter(NS + "alert")
    ]
    if any(str(a["level"]).upper() == "ERROR" for a in alerts):
        raise ValueError("afinfo reported an error; see commands.jsonl")
    return {
        "codec": codec,
        "channels": channels,
        "sample_rate": int(sample_rate),
        "duration_seconds": duration,
        "reported_bitrate_bps": int(text("bit_rate")),
        "packet_table": packet,
        "alerts": alerts,
    }


def encode_aac(directory: Path, output: Path, *, progress: Progress | None = None) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("Apple AAC encoding requires macOS afconvert/afinfo")
    directory = directory.resolve()
    with operation_report(output, "encode", directory) as report:
        report.update(
            platform=platform.platform(),
            encoder={
                "tool": AFCONVERT,
                "bitrate_bps": 256000,
                "strategy": "constrained_vbr",
                "strategy_id": 2,
                "quality": 127,
            },
            loudness_normalized=False,
            metadata_tagged=False,
            itunes_plus_equivalence_verified=False,
            aac_settings_reference=(
                "https://www.apple.com/apple-music/apple-digital-masters/docs/apple-digital-masters.pdf"
            ),
            verification_scope=(
                "Codec/container, valid frames, full decode; NOT PCM equality or audio quality"
            ),
        )
        source_set = load_wavs(directory)
        tracks = source_set.tracks
        report.update(
            source_manifest_sha256=source_set.manifest_sha256,
            skipped_files=list(source_set.skipped),
            loudness_normalized=source_set.normalization is not None,
            source_normalization=source_set.normalization,
        )
        if source_set.normalization:
            report["verification_scope"] = (
                "Codec/container, valid frames, float decode, LUFS/true peak; "
                "not perceptual quality"
            )
        output = output.resolve()
        with (output / "commands.jsonl").open("x", encoding="utf-8") as log:
            for source_entry in tracks:
                number, frames = source_entry["number"], source_entry["frames"]
                source = directory / source_entry["file"]
                name = f"track-{number:02}.m4a"
                partial = output / (name + ".part")
                decoded_suffix = "caf" if source_set.normalization else "wav"
                decoded = output / f".track-{number:02}.decoded.{decoded_suffix}"
                entry = {
                    "number": number,
                    "status": "running",
                    "file": name,
                    "source_wav": source.name,
                    "source_wav_sha256": source_entry["wav_sha256"],
                    "source_pcm_sha256": source_entry["pcm_sha256"],
                    "source_frames": frames,
                }
                report["tracks"].append(entry)
                if progress:
                    progress(number, 0, frames)
                _run(
                    [
                        AFCONVERT,
                        "-f",
                        "m4af",
                        "-d",
                        "aac",
                        "-b",
                        "256000",
                        "-s",
                        "2",
                        "-q",
                        "127",
                        "-l",
                        "Stereo",
                        str(source),
                        str(partial),
                    ],
                    log,
                )
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise ValueError(f"Encoder produced no audio file: {name}")
                info = _parse_afinfo(_run([AFINFO, "-x", str(partial)], log, timeout=30), frames)
                if source_set.normalization:
                    from diskflow.loudness import measure_loudness

                    # Float PCM avoids hiding AAC overshoot through integer clipping.
                    _run([AFCONVERT, "-f", "caff", "-d", "LEF32", str(partial), str(decoded)], log)
                    loudness = measure_loudness(decoded)
                    entry["decoded_loudness"] = loudness.as_dict()
                    entry["decoded_target_reached"] = (
                        loudness.integrated_lufs is not None
                        and abs(loudness.integrated_lufs - source_set.normalization["target_lufs"])
                        <= 0.1
                    )
                    if loudness.frames != frames:
                        raise ValueError("Decoded AAC frame count disagrees with input")
                    ceiling = source_set.normalization["true_peak_dbtp"]
                    if (
                        loudness.true_peak_upper_dbtp is not None
                        and loudness.true_peak_upper_dbtp > ceiling
                    ):
                        raise ValueError(
                            f"AAC track {number} exceeds {ceiling} dBTP after decoding; "
                            "normalize the ORIGINAL WAVs again with more --peak-headroom"
                        )
                else:
                    _run([AFCONVERT, "-f", "WAVE", "-d", "LEI16", str(partial), str(decoded)], log)
                    check_wav(decoded, frames)  # AAC is lossy; never require PCM hash equality.
                if file_sha256(source) != source_entry["wav_sha256"]:
                    raise ValueError(f"Source WAV changed during encoding: {source.name}")
                entry.update(
                    status="decode_verified",
                    afinfo=info,
                    decoded_frames=frames,
                    bytes=partial.stat().st_size,
                    sha256=file_sha256(partial),
                )
                decoded.unlink()
                if progress:
                    progress(number, frames, frames)
                write_json(output / "report.json", report)
        source_set.verify_unchanged()
        for entry in report["tracks"]:
            (output / (entry["file"] + ".part")).rename(output / entry["file"])
            entry["status"] = "complete"
        if source_set.normalization:
            report["all_targets_reached"] = all(
                t["decoded_target_reached"] for t in report["tracks"]
            )
        report["status"] = "aac_encoded"
    return report

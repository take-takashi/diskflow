"""Peak-limited, per-track fixed gain applied to new PCM WAV files only."""

import math
import os
import shutil
from pathlib import Path

import av

from diskflow.archive import write_json
from diskflow.derived import Progress, operation_report
from diskflow.image import file_sha256
from diskflow.loudness import drain, measure_loudness
from diskflow.wavs import check_wav, load_wavs

DEFAULT_TARGET_LUFS = -14.0
DEFAULT_TRUE_PEAK_DBTP = -1.0
DEFAULT_PEAK_HEADROOM_DB = 0.05


def calculate_gain_db(
    integrated_lufs: float | None,
    true_peak_upper_dbtp: float | None,
    target_lufs: float,
    true_peak_dbtp: float,
    peak_headroom_db: float = DEFAULT_PEAK_HEADROOM_DB,
) -> float:
    values = [v for v in (integrated_lufs, true_peak_upper_dbtp) if v is not None]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Non-finite loudness measurements cannot determine gain")
    loudness_gain = target_lufs - integrated_lufs if integrated_lufs is not None else 0.0
    peak_gain = (
        true_peak_dbtp - peak_headroom_db - true_peak_upper_dbtp
        if true_peak_upper_dbtp is not None
        else math.inf
    )
    return min(loudness_gain, peak_gain)


def _write_gain(source_path: Path, output: Path, gain_db: float) -> None:
    if abs(gain_db) < 1e-9:
        shutil.copyfile(source_path, output)
        return
    try:
        with av.open(str(source_path)) as source, av.open(str(output), "w", format="wav") as dest:
            stream = source.streams.audio[0]
            encoder = dest.add_stream("pcm_s16le", rate=44100)
            encoder.layout = "stereo"
            graph = av.filter.Graph()
            input_node = graph.add_abuffer(template=stream)
            volume = graph.add("volume", f"volume={gain_db:.12f}dB:precision=double")
            quantize = graph.add("aresample", "osf=s16:dither_method=triangular")
            format_node = graph.add(
                "aformat", "sample_fmts=s16:sample_rates=44100:channel_layouts=stereo"
            )
            sink = graph.add("abuffersink")
            graph.link_nodes(input_node, volume, quantize, format_node, sink)
            graph.configure()
            for frame in source.decode(audio=0):
                graph.push(frame)
                for filtered in drain(graph):
                    for packet in encoder.encode(filtered):
                        dest.mux(packet)
            graph.push(None)
            for filtered in drain(graph):
                for packet in encoder.encode(filtered):
                    dest.mux(packet)
            for packet in encoder.encode(None):
                dest.mux(packet)
        with output.open("r+b") as written:
            written.flush()
            os.fsync(written.fileno())
    except av.FFmpegError as exc:
        raise RuntimeError(f"PyAV fixed-gain processing failed: {exc}") from exc


def normalize_wavs(
    directory: Path,
    output: Path,
    *,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    true_peak_dbtp: float = DEFAULT_TRUE_PEAK_DBTP,
    peak_headroom_db: float = DEFAULT_PEAK_HEADROOM_DB,
    progress: Progress | None = None,
) -> dict:
    if not (
        math.isfinite(target_lufs)
        and -70 < target_lufs < 0
        and math.isfinite(true_peak_dbtp)
        and -20 <= true_peak_dbtp <= 0
        and math.isfinite(peak_headroom_db)
        and 0 <= peak_headroom_db <= 6
    ):
        raise ValueError("Use finite LUFS (-70..0), peak (-20..0 dBTP), and headroom (0..6 dB)")
    directory = directory.resolve()
    with operation_report(output, "normalize", directory) as report:
        source_set = load_wavs(directory, allow_normalized=False)
        policy = {
            "method": "track_gain_peak_limited",
            "target_lufs": target_lufs,
            "true_peak_dbtp": true_peak_dbtp,
            "peak_headroom_db": peak_headroom_db,
            "target_tolerance_lu": 0.1,
            "limiter": False,
            "engine": "PyAV ebur128 + astats; double-precision volume + TPDF s16 conversion",
            "pyav_version": av.__version__,
            "library_versions": av.library_versions,
            "true_peak_metadata_linear_resolution": 0.001,
            "true_peak_validation": "conservative rounded-metadata upper estimate",
            "edge_handling": "original-duration LUFS; zero-padded peaks only (no output padding)",
        }
        report.update(
            normalization=policy,
            loudness_normalized=True,
            source_manifest_sha256=source_set.manifest_sha256,
            skipped_files=list(source_set.skipped),
            all_targets_reached=False,
        )
        for source_entry in source_set.tracks:
            number, frames = source_entry["number"], source_entry["frames"]
            source = directory / source_entry["file"]
            partial = output / (source_entry["file"] + ".part")
            entry = {
                "number": number,
                "kind": "track",
                "status": "running",
                "file": source_entry["file"],
                "frames": frames,
                "pcm_bytes": frames * 4,
                "pre_emphasis": False,
                "source_wav_sha256": source_entry["wav_sha256"],
                "source_pcm_sha256": source_entry["pcm_sha256"],
                "attempts": [],
            }
            report["tracks"].append(entry)
            if progress:
                progress(number, 0, frames)
            before = measure_loudness(source)
            if before.frames != frames:
                raise ValueError("Input frame count changed during loudness analysis")
            entry["before"] = before.as_dict()
            gain = calculate_gain_db(
                before.integrated_lufs,
                before.true_peak_upper_dbtp,
                target_lufs,
                true_peak_dbtp,
                peak_headroom_db,
            )
            for attempt in range(1, 4):
                _write_gain(source, partial, gain)
                pcm_sha256 = check_wav(partial, frames)
                after = measure_loudness(partial)
                if after.frames != frames:
                    raise ValueError("Fixed gain changed the PCM frame count")
                entry["attempts"].append(
                    {"number": attempt, "gain_db": gain, "after": after.as_dict()}
                )
                peak_ok = (
                    after.true_peak_upper_dbtp is None
                    or after.true_peak_upper_dbtp <= true_peak_dbtp
                )
                if peak_ok:
                    break
                gain -= after.true_peak_upper_dbtp - true_peak_dbtp + max(peak_headroom_db, 0.05)
            else:
                raise ValueError(f"Track {number} still exceeds the true-peak ceiling")
            if after.sample_peak_dbfs is not None and after.sample_peak_dbfs >= -0.001:
                raise ValueError(f"Track {number} has full-scale PCM samples after normalization")
            reached = (
                after.integrated_lufs is not None
                and abs(after.integrated_lufs - target_lufs) <= policy["target_tolerance_lu"]
            )
            reason = (
                "target_reached"
                if reached
                else "silence"
                if before.sample_peak_dbfs is None
                else "below_gate_or_too_short"
                if before.integrated_lufs is None
                else "peak_limited"
                if gain < target_lufs - before.integrated_lufs - 1e-6
                else "measurement_target_miss"
            )
            entry.update(
                status="verified",
                gain_db=gain,
                after=after.as_dict(),
                target_reached=reached,
                result=reason,
                pcm_sha256=pcm_sha256,
                wav_sha256=file_sha256(partial),
                dither="TPDF" if abs(gain) >= 1e-9 else "not_applied",
            )
            if file_sha256(source) != source_entry["wav_sha256"]:
                raise ValueError("Source WAV changed during normalization")
            if progress:
                progress(number, frames, frames)
            write_json(output / "report.json", report)
        source_set.verify_unchanged()
        for entry in report["tracks"]:
            (output / (entry["file"] + ".part")).rename(output / entry["file"])
            entry["status"] = "complete"
        report.update(
            status="normalized",
            all_targets_reached=all(t["target_reached"] for t in report["tracks"]),
        )
    return report

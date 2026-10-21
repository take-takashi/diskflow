"""Streaming BS.1770/R128 measurements using bundled PyAV libraries, not FFmpeg CLI."""

import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

import av
from av.error import BlockingIOError as NeedMore
from av.error import EOFError as EndOfStream


@dataclass(frozen=True)
class Loudness:
    frames: int
    integrated_lufs: float | None
    true_peak_dbtp: float | None
    true_peak_upper_dbtp: float | None
    sample_peak_dbfs: float | None
    analysis_padding_frames: int

    def as_dict(self) -> dict:
        return asdict(self)


def drain(graph):
    while True:
        try:
            yield graph.pull()
        except NeedMore, EndOfStream:
            return


def measure_loudness(path: Path) -> Loudness:
    """Keep original-duration LUFS; pad only to expose the final true/sample peaks.

    ebur128 exports metadata at 100 ms boundaries. EOF alone can omit a partial
    final block and its peaks, so append <200 ms of zeros for peak analysis only.
    The source and its decoded frame count are never extended on disk.
    """
    original_last = final_last = None
    frames = 0
    try:
        with av.open(str(path)) as source:
            if len(source.streams.audio) != 1:
                raise ValueError("Expected one audio stream for loudness analysis")
            stream = source.streams.audio[0]
            if stream.codec_context.sample_rate != 44100 or stream.codec_context.channels != 2:
                raise ValueError("Loudness analysis requires 44100 Hz stereo audio")
            graph = av.filter.Graph()
            input_node = graph.add_abuffer(template=stream)
            meter = graph.add("ebur128", "metadata=1:peak=true")
            stats = graph.add(
                "astats",
                "metadata=1:reset=0:measure_perchannel=none:"
                "measure_overall=Peak_level+Number_of_NaNs+Number_of_Infs",
            )
            sink = graph.add("abuffersink")
            graph.link_nodes(input_node, meter, stats, sink)
            graph.configure()
            for frame in source.decode(audio=0):
                if frame.sample_rate != 44100 or len(frame.layout.channels) != 2:
                    raise ValueError("Audio format changed during loudness analysis")
                frames += frame.samples
                graph.push(frame)
                for filtered in drain(graph):
                    if "lavfi.r128.I" in filtered.metadata:
                        original_last = dict(filtered.metadata)
            if not frames:
                raise ValueError("No audio frames to analyze")
            padding = (-frames) % 4410 + 4410
            silence = av.AudioFrame(
                format=stream.codec_context.format.name,
                layout=stream.codec_context.layout.name,
                samples=padding,
            )
            silence.sample_rate = 44100
            silence.time_base = Fraction(1, 44100)
            silence.pts = frames
            for plane in silence.planes:
                plane.update(bytes(plane.buffer_size))
            graph.push(silence)
            for filtered in drain(graph):
                if "lavfi.r128.I" in filtered.metadata:
                    final_last = dict(filtered.metadata)
            graph.push(None)
            for filtered in drain(graph):
                if "lavfi.r128.I" in filtered.metadata:
                    final_last = dict(filtered.metadata)
    except av.FFmpegError as exc:
        raise RuntimeError(f"PyAV loudness analysis failed: {exc}") from exc
    if final_last is None:
        raise ValueError("No final R128/astats measurements")
    try:
        for key in ("lavfi.astats.Number of NaNs", "lavfi.astats.Number of Infs"):
            if float(final_last[key]) != 0:
                raise ValueError("Non-finite PCM samples in audio")
        sample_peak = float(final_last["lavfi.astats.Overall.Peak_level"])
        peak = float(final_last["lavfi.r128.true_peak"])
        if (
            not math.isfinite(peak)
            or peak < 0
            or math.isnan(sample_peak)
            or sample_peak == math.inf
        ):
            raise ValueError("Invalid peak measurements")
        integrated = float(original_last["lavfi.r128.I"]) if original_last else -70.0
        if not math.isfinite(integrated):
            raise ValueError("Invalid integrated loudness measurement")
    except (KeyError, TypeError) as exc:
        raise ValueError("Missing R128/astats measurements") from exc
    # R128 metadata rounds linear true peak to 0.001. Use a conservative upper
    # estimate as well, never round a near-ceiling measurement into a false pass.
    sample_peak = sample_peak if math.isfinite(sample_peak) else None
    upper = max(peak + 0.001, 10 ** (sample_peak / 20)) if sample_peak is not None else peak
    return Loudness(
        frames=frames,
        integrated_lufs=integrated if frames >= 17640 and integrated > -69.99 else None,
        true_peak_dbtp=20 * math.log10(peak) if peak > 0 else None,
        true_peak_upper_dbtp=20 * math.log10(upper) if upper > 0 else None,
        sample_peak_dbfs=sample_peak,
        analysis_padding_frames=padding,
    )

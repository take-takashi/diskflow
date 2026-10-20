"""Bounded AIFF/AIFF-C parsing for uncompressed CD-format PCM (no aifc module)."""

import math
import struct
from dataclasses import dataclass
from typing import BinaryIO


@dataclass(frozen=True)
class PCMInfo:
    container: str
    compression: str
    byte_order: str
    channels: int
    sample_width: int
    sample_rate: int
    frames: int
    data_offset: int
    data_bytes: int


def _read_exact(source: BinaryIO, count: int) -> bytes:
    data = source.read(count)
    if len(data) != count:
        raise ValueError("Truncated AIFF data")
    return data


def _sample_rate(data: bytes) -> float:
    exponent, significand = struct.unpack(">HQ", data)
    if exponent & 0x8000 or exponent == 0x7FFF:
        raise ValueError("Invalid AIFF sample rate")
    try:
        return math.ldexp(significand, (exponent or 1) - 16383 - 63)
    except OverflowError as exc:
        raise ValueError("Invalid AIFF sample rate") from exc


def read_pcm_info(source: BinaryIO) -> PCMInfo:
    """Locate COMM/SSND without reading PCM or assuming a fixed-size header."""
    source.seek(0, 2)
    file_size = source.tell()
    source.seek(0)
    header = source.read(12)
    if len(header) != 12 or header[:4] != b"FORM" or header[8:] not in (b"AIFF", b"AIFC"):
        raise ValueError("Expected an AIFF or AIFF-C FORM container")
    form_end = int.from_bytes(header[4:8], "big") + 8
    if form_end != file_size:
        raise ValueError("AIFF FORM length disagrees with file size")
    container = header[8:]
    common = sound = None
    position = 12
    while position < form_end:
        if position + 8 > form_end:
            raise ValueError("Truncated AIFF chunk header")
        source.seek(position)
        chunk_id, size = struct.unpack(">4sI", _read_exact(source, 8))
        end = position + 8 + size
        if end + (size & 1) > form_end:
            raise ValueError("Truncated AIFF chunk or padding")
        if chunk_id == b"COMM":
            if common is not None:
                raise ValueError("Duplicate AIFF COMM chunk")
            minimum = 22 if container == b"AIFC" else 18
            if size < minimum:
                raise ValueError("Truncated AIFF COMM chunk")
            data = _read_exact(source, minimum)
            channels, frames, bits = struct.unpack(">HIH", data[:8])
            rate = _sample_rate(data[8:18])
            compression = data[18:22] if container == b"AIFC" else b"NONE"
            if compression not in (b"NONE", b"twos", b"sowt"):
                raise ValueError(f"Unsupported AIFF compression: {compression!r}")
            if (channels, bits, rate) != (2, 16, 44100):
                raise ValueError("Expected 44100 Hz, 16-bit stereo PCM")
            common = (channels, frames, compression)
        elif chunk_id == b"SSND":
            if sound is not None:
                raise ValueError("Duplicate AIFF SSND chunk")
            if size < 8:
                raise ValueError("Truncated AIFF SSND chunk")
            offset, _block_size = struct.unpack(">II", _read_exact(source, 8))
            if offset > size - 8:
                raise ValueError("AIFF SSND offset exceeds chunk size")
            sound = (position + 16 + offset, size - 8 - offset)
        position = end + (size & 1)
    if common is None or sound is None:
        raise ValueError("Missing AIFF COMM or SSND chunk")
    channels, frames, compression = common
    data_offset, data_bytes = sound
    if frames * channels * 2 != data_bytes:
        raise ValueError("AIFF COMM frame count disagrees with SSND PCM length")
    return PCMInfo(
        container.decode("ascii"),
        compression.decode("ascii"),
        "little" if compression == b"sowt" else "big",
        channels,
        2,
        44100,
        frames,
        data_offset,
        data_bytes,
    )

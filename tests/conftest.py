import hashlib
import json
import math
import plistlib
import struct

import pytest

from diskflow.toc import SECTOR_BYTES, parse_toc


@pytest.fixture
def make_toc():
    def build(starts=(0, 5), leadout=11, controls=None, session=1):
        controls = controls or [0] * len(starts)

        def msf(lba):
            minute, rest = divmod(lba + 150, 60 * 75)
            second, frame = divmod(rest, 75)
            return bytes((minute, second, frame))

        def descriptor(point, position, control=0):
            return bytes((session, 0x10 | control, 0, point, 0, 0, 0, 0)) + position

        descriptors = [
            descriptor(0xA0, bytes((1, 0, 0))),
            descriptor(0xA1, bytes((len(starts), 0, 0))),
            descriptor(0xA2, msf(leadout)),
        ]
        descriptors += [
            descriptor(number, msf(start), control)
            for number, (start, control) in enumerate(zip(starts, controls, strict=True), 1)
        ]
        payload = bytes((session, session)) + b"".join(descriptors)
        return len(payload).to_bytes(2, "big") + payload

    return build


@pytest.fixture
def make_aiff():
    def chunk(name, data):
        return name + struct.pack(">I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")

    def build(
        pcm,
        *,
        container=b"AIFC",
        compression=None,
        frames=None,
        offset=19,
        channels=2,
        bits=16,
        rate=44100,
        sound_first=False,
    ):
        compression = compression or (b"sowt" if container == b"AIFC" else b"NONE")
        mantissa, exponent = math.frexp(rate)
        extended = struct.pack(">HQ", exponent + 16382, int(mantissa * (1 << 64)))
        common = struct.pack(">HIH", channels, len(pcm) // 4 if frames is None else frames, bits)
        common += extended
        if container == b"AIFC":
            common += compression + b"\x00\x00"
        if compression != b"sowt":
            swapped = bytearray(pcm)
            swapped[::2], swapped[1::2] = swapped[1::2], swapped[::2]
            pcm = bytes(swapped)
        sound = struct.pack(">II", offset, 0) + bytes(offset) + pcm
        chunks = [chunk(b"COMM", common), chunk(b"SSND", sound)]
        if sound_first:
            chunks.reverse()
        body = container + chunk(b"JUNK", b"odd") + b"".join(chunks)
        return b"FORM" + struct.pack(">I", len(body)) + body

    return build


@pytest.fixture
def make_capture(make_toc):
    def build(path, *, starts=(0, 5), leadout=11, controls=None):
        path.mkdir()
        raw_toc = make_toc(starts=starts, leadout=leadout, controls=controls)
        pcm = b"".join(struct.pack("<hh", n, -n - 1) * (SECTOR_BYTES // 4) for n in range(leadout))
        (path / "toc.bin").write_bytes(raw_toc)
        (path / "audio.bin").write_bytes(pcm)
        report = {
            "mode": "rip",
            "status": "reads_matched",
            "data_file": "audio.bin",
            "expected_bytes": len(pcm),
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "toc_sha256": hashlib.sha256(raw_toc).hexdigest(),
        }
        (path / "report.json").write_text(json.dumps(report))
        return path, pcm

    return build


@pytest.fixture
def captured(make_capture, tmp_path):
    return make_capture(tmp_path / "capture")


@pytest.fixture
def make_mount(make_aiff):
    def build(path, capture, *, container=b"AIFC", compression=None):
        path.mkdir()
        directory, pcm = capture
        raw_toc = (directory / "toc.bin").read_bytes()
        (path / ".TOC.plist").write_bytes(plistlib.dumps({"Format 0x02 TOC Data": raw_toc}))
        toc = parse_toc(raw_toc)
        ends = [t.start_lba for t in toc.tracks[1:]] + [toc.sessions[0].leadout_lba]
        for track, end in zip(toc.tracks, ends, strict=True):
            payload = pcm[track.start_lba * SECTOR_BYTES : end * SECTOR_BYTES]
            (path / f"{track.number} 曲.aiff").write_bytes(
                make_aiff(payload, container=container, compression=compression)
            )
        return path

    return build


@pytest.fixture
def mounted(make_mount, captured, tmp_path):
    return make_mount(tmp_path / "mount", captured)

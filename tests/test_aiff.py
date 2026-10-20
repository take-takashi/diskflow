import io
import struct

import pytest

from diskflow.aiff import read_pcm_info


@pytest.mark.parametrize(
    "container,compression,order",
    [
        (b"AIFF", b"NONE", "big"),
        (b"AIFC", b"NONE", "big"),
        (b"AIFC", b"twos", "big"),
        (b"AIFC", b"sowt", "little"),
    ],
)
@pytest.mark.parametrize("sound_first", [False, True])
def test_pcm_chunks_padding_and_byte_order(make_aiff, container, compression, order, sound_first):
    pcm = b"\x01\x02\x03\x04" * 10
    encoded = make_aiff(
        pcm, container=container, compression=compression, offset=13, sound_first=sound_first
    )
    source = io.BytesIO(encoded)
    info = read_pcm_info(source)
    assert info.container == container.decode()
    assert info.compression == compression.decode()
    assert info.byte_order == order
    assert (info.channels, info.sample_width, info.sample_rate, info.frames) == (2, 2, 44100, 10)
    assert info.data_bytes == len(pcm)
    source.seek(info.data_offset)
    expected = pcm if order == "little" else b"\x02\x01\x04\x03" * 10
    assert source.read(info.data_bytes) == expected
    assert info.data_offset != 2352  # No hard-coded cddafs header size.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channels": 1},
        {"bits": 24},
        {"rate": 48000},
        {"compression": b"alaw"},
    ],
)
def test_unsupported_audio_refused(make_aiff, kwargs):
    with pytest.raises(ValueError, match="Expected|Unsupported"):
        read_pcm_info(io.BytesIO(make_aiff(bytes(40), **kwargs)))


def test_frame_count_mismatch(make_aiff):
    with pytest.raises(ValueError, match="frame count"):
        read_pcm_info(io.BytesIO(make_aiff(bytes(40), frames=11)))


@pytest.mark.parametrize("length", [0, 4, 11, 20, 60])
def test_truncated_file(make_aiff, length):
    with pytest.raises(ValueError):
        read_pcm_info(io.BytesIO(make_aiff(bytes(40))[:length]))


def test_ssnd_offset_outside_chunk(make_aiff):
    data = bytearray(make_aiff(bytes(40)))
    start = data.index(b"SSND")
    data[start + 8 : start + 12] = struct.pack(">I", 10000)
    with pytest.raises(ValueError, match="offset exceeds"):
        read_pcm_info(io.BytesIO(data))


def test_duplicate_common_chunk(make_aiff):
    data = make_aiff(bytes(40))
    start = data.index(b"COMM")
    length = int.from_bytes(data[start + 4 : start + 8], "big")
    extra = data[start : start + 8 + length]
    changed = data[:4] + (len(data) + len(extra) - 8).to_bytes(4, "big") + data[8:] + extra
    with pytest.raises(ValueError, match="Duplicate"):
        read_pcm_info(io.BytesIO(changed))


def test_missing_sound_chunk(make_aiff):
    data = make_aiff(bytes(40)).replace(b"SSND", b"JUNK")
    with pytest.raises(ValueError, match="Missing"):
        read_pcm_info(io.BytesIO(data))

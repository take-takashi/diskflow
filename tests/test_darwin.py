import ctypes

import pytest

from diskflow.darwin import CDRead, CDReadTOC, DarwinCD
from diskflow.toc import SECTOR_BYTES


def test_64_bit_darwin_abi():
    from diskflow.darwin import DKIOCCDREAD, DKIOCCDREADTOC

    assert ctypes.sizeof(CDRead) == 32
    assert CDRead.buffer_length.offset == 20
    assert CDRead.buffer.offset == 24
    assert ctypes.sizeof(CDReadTOC) == 24
    assert CDReadTOC.session.offset == 7
    assert CDReadTOC.buffer_length.offset == 14
    assert CDReadTOC.buffer.offset == 16
    assert DKIOCCDREAD == 0xC0206460
    assert DKIOCCDREADTOC == 0xC0186464


@pytest.fixture
def cd(monkeypatch):
    monkeypatch.setattr("diskflow.darwin.sys.platform", "darwin")
    reader = DarwinCD("/dev/rdisk99")
    reader.fd = 123  # ioctl is always mocked; never touch a real disk in tests.
    return reader


def test_audio_ioctl_uses_2352_byte_offsets(cd, monkeypatch):
    def ioctl(fd, command, argument, mutate):
        request = CDRead.from_buffer(argument)
        assert fd == 123
        assert request.offset == 150 * SECTOR_BYTES
        assert request.sector_area == 0x10
        assert request.sector_type == 1
        assert request.buffer_length == 2 * SECTOR_BYTES
        assert bytes(request.reserved) == bytes(10)
        ctypes.memset(request.buffer, 42, request.buffer_length)
        return 0

    monkeypatch.setattr("diskflow.darwin.fcntl.ioctl", ioctl)
    assert cd.read_audio(150, 2) == bytes((42,)) * (2 * SECTOR_BYTES)


def test_short_audio_read_is_not_success(cd, monkeypatch):
    def ioctl(fd, command, argument, mutate):
        request = CDRead.from_buffer(argument)
        request.buffer_length -= 1
        return 0

    monkeypatch.setattr("diskflow.darwin.fcntl.ioctl", ioctl)
    with pytest.raises(OSError, match="Short CDDA read"):
        cd.read_audio(0, 1)


def test_toc_buffer_and_returned_length(cd, monkeypatch, make_toc):
    raw = make_toc()

    def ioctl(fd, command, argument, mutate):
        request = CDReadTOC.from_buffer(argument)
        assert request.format == 2
        assert request.format_as_time == 1
        assert request.session == 0
        assert request.buffer_length == 65534  # Avoid the observed 0xffff allocation error.
        assert bytes(request.reserved1) == bytes(5)
        assert bytes(request.reserved2) == bytes(6)
        ctypes.memmove(request.buffer, raw, len(raw))
        request.buffer_length = len(raw)
        return 0

    monkeypatch.setattr("diskflow.darwin.fcntl.ioctl", ioctl)
    assert cd.read_toc() == raw


@pytest.mark.parametrize("path", ["/dev/disk8", "/dev/rdisk8s1", "/tmp/audio", "/dev/rdisk0/../x"])
def test_only_explicit_whole_raw_devices(monkeypatch, path):
    monkeypatch.setattr("diskflow.darwin.sys.platform", "darwin")
    with pytest.raises(ValueError, match="whole raw CD"):
        DarwinCD(path)


@pytest.mark.parametrize("lba,sectors", [(-1, 1), (0, 0), (0, 76)])
def test_invalid_ranges_rejected(cd, lba, sectors):
    with pytest.raises(ValueError):
        cd.read_audio(lba, sectors)

"""Read-only CD ioctls, expressed using Python's standard library.

ABI source: Apple's IOKit/storage/IOCDMediaBSDClient.h and IOCDTypes.h.
Only the 64-bit Darwin ABI is supported. No compiler or SDK is needed at runtime.
"""

import ctypes
import fcntl
import os
import re
import stat
import sys

from diskflow.toc import SECTOR_BYTES


class CDRead(ctypes.Structure):
    _fields_ = [
        ("offset", ctypes.c_uint64),
        ("sector_area", ctypes.c_uint8),
        ("sector_type", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 10),
        ("buffer_length", ctypes.c_uint32),
        ("buffer", ctypes.c_void_p),
    ]


class CDReadTOC(ctypes.Structure):
    _fields_ = [
        ("format", ctypes.c_uint8),
        ("format_as_time", ctypes.c_uint8),
        ("reserved1", ctypes.c_uint8 * 5),
        ("session", ctypes.c_uint8),
        ("reserved2", ctypes.c_uint8 * 6),
        ("buffer_length", ctypes.c_uint16),
        ("buffer", ctypes.c_void_p),
    ]


def _iowr(number: int, structure: type[ctypes.Structure]) -> int:
    # sys/ioccom.h: IOC_INOUT | (sizeof(type) << 16) | ('d' << 8) | number.
    return 0xC0000000 | (ctypes.sizeof(structure) << 16) | (ord("d") << 8) | number


DKIOCCDREAD = _iowr(96, CDRead)
DKIOCCDREADTOC = _iowr(100, CDReadTOC)
MAX_READ_SECTORS = 75
# The tested macOS/USB setup rejects allocation length 0xffff, but accepts 0xfffe.
TOC_BUFFER_BYTES = 65534


def _ioctl(fd: int, command: int, request):
    argument = bytearray(bytes(request))
    fcntl.ioctl(fd, command, argument, True)
    return type(request).from_buffer_copy(argument)


class DarwinCD:
    def __init__(self, device: str):
        if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("CD access requires 64-bit macOS")
        if not re.fullmatch(r"/dev/rdisk[0-9]+", device):
            raise ValueError("Specify a whole raw CD device, e.g. /dev/rdisk8 (not a partition)")
        self.device = device
        self.fd: int | None = None

    def __enter__(self):
        self.fd = os.open(self.device, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        if not stat.S_ISCHR(os.fstat(self.fd).st_mode):
            self.__exit__(None, None, None)
            raise ValueError("Expected a raw character device")
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read_toc(self) -> bytes:
        buffer = ctypes.create_string_buffer(TOC_BUFFER_BYTES)
        request = CDReadTOC(
            format=0x02,
            format_as_time=1,
            session=0,
            buffer_length=ctypes.sizeof(buffer),
            buffer=ctypes.addressof(buffer),
        )
        result = _ioctl(self.fd, DKIOCCDREADTOC, request)
        actual = result.buffer_length
        if actual < 4 or actual > len(buffer):
            raise OSError(f"Invalid TOC transfer length: {actual}")
        declared = int.from_bytes(buffer.raw[:2], "big") + 2
        if declared < 4 or declared > actual:
            raise OSError(f"Short TOC read: expected {declared} bytes, got {actual}")
        return buffer.raw[:declared]

    def read_audio(self, lba: int, sectors: int) -> bytes:
        if lba < 0 or not 1 <= sectors <= MAX_READ_SECTORS:
            raise ValueError(f"Expected LBA >= 0 and 1..{MAX_READ_SECTORS} sectors")
        length = sectors * SECTOR_BYTES
        buffer = ctypes.create_string_buffer(length)
        request = CDRead(
            offset=lba * SECTOR_BYTES,
            sector_area=0x10,  # kCDSectorAreaUser
            sector_type=0x01,  # kCDSectorTypeCDDA
            buffer_length=length,
            buffer=ctypes.addressof(buffer),
        )
        result = _ioctl(self.fd, DKIOCCDREAD, request)
        if result.buffer_length != length:
            raise OSError(
                f"Short CDDA read at LBA {lba}: expected {length}, got {result.buffer_length}"
            )
        return buffer.raw

"""Decode macOS's binary, full (format 0x02) CD TOC; never guess track positions."""

from dataclasses import asdict, dataclass
from itertools import pairwise

SECTOR_BYTES = 2352
FRAMES_PER_SECOND = 75
LEAD_IN_FRAMES = 150


def msf_to_lba(minute: int, second: int, frame: int) -> int:
    if not (0 <= minute <= 255 and 0 <= second < 60 and 0 <= frame < 75):
        raise ValueError(f"Invalid binary MSF: {minute}:{second}:{frame}")
    return (minute * 60 + second) * 75 + frame - LEAD_IN_FRAMES


def cue_time(sectors: int) -> str:
    if sectors < 0:
        raise ValueError("Negative CUE position")
    minute, remainder = divmod(sectors, 60 * FRAMES_PER_SECOND)
    second, frame = divmod(remainder, FRAMES_PER_SECOND)
    return f"{minute:02}:{second:02}:{frame:02}"


@dataclass(frozen=True)
class Track:
    number: int
    session: int
    control: int
    start_lba: int

    @property
    def is_audio(self) -> bool:
        return not bool(self.control & 0x04)


@dataclass(frozen=True)
class Session:
    number: int
    first_track: int
    last_track: int
    leadout_lba: int


@dataclass(frozen=True)
class TOC:
    first_session: int
    last_session: int
    sessions: tuple[Session, ...]
    tracks: tuple[Track, ...]

    def as_dict(self) -> dict:
        result = asdict(self)
        result["tracks"] = [asdict(t) | {"is_audio": t.is_audio} for t in self.tracks]
        return result

    def audio_session(self) -> Session:
        """Initial PoC intentionally refuses mixed-mode and multisession media."""
        if self.first_session != 1 or self.last_session != 1 or len(self.sessions) != 1:
            raise ValueError("Multisession CDs are not supported by this PoC; use inspect only")
        if not self.tracks or any(not t.is_audio for t in self.tracks):
            raise ValueError("Data/mixed-mode CDs are not supported by this PoC")
        if any(t.control & 0x08 for t in self.tracks):
            raise ValueError("Four-channel audio CDs are not supported by this PoC")
        if self.tracks[0].number != 1 or self.tracks[0].start_lba < 0:
            raise ValueError("Unsupported first track number/position")
        return self.sessions[0]

    def cue(self) -> str:
        self.audio_session()
        lines = [
            "REM PoC: INDEX 00/subchannels/negative LBAs are not fully preserved",
            'FILE "audio.bin" BINARY',
        ]
        for track in self.tracks:
            lines.append(f"  TRACK {track.number:02} AUDIO")
            flags = []
            if track.control & 0x01:
                flags.append("PRE")
            if track.control & 0x02:
                flags.append("DCP")
            if flags:
                lines.append("    FLAGS " + " ".join(flags))
            if track.number == 1 and track.start_lba > 0:
                lines.append("    INDEX 00 00:00:00")
            lines.append(f"    INDEX 01 {cue_time(track.start_lba)}")
        return "\n".join(lines) + "\n"


def parse_toc(raw: bytes) -> TOC:
    """The length is big-endian; descriptor MSF bytes are binary, not BCD."""
    if len(raw) < 4:
        raise ValueError("Truncated TOC header")
    total = int.from_bytes(raw[:2], "big") + 2
    if total < 4 or total > len(raw) or (total - 4) % 11:
        raise ValueError("Truncated or malformed TOC descriptors")
    first, last = raw[2:4]
    if not (1 <= first <= last <= 99):
        raise ValueError("Invalid TOC session range")

    entries: dict[tuple[int, int], bytes] = {}
    for offset in range(4, total, 11):
        descriptor = raw[offset : offset + 11]
        session, adr_control, _, point = descriptor[:4]
        if adr_control >> 4 != 1:
            continue
        if not (first <= session <= last):
            raise ValueError("TOC descriptor session is outside header range")
        if point not in (0xA0, 0xA1, 0xA2) and not 1 <= point <= 99:
            continue
        key = (session, point)
        if key in entries and entries[key] != descriptor:
            raise ValueError("Conflicting TOC descriptors")
        entries[key] = descriptor

    sessions, tracks = [], []
    for number in range(first, last + 1):
        try:
            first_track = entries[number, 0xA0][8]
            last_track = entries[number, 0xA1][8]
            leadout = msf_to_lba(*entries[number, 0xA2][8:11])
        except KeyError as exc:
            raise ValueError(f"Missing session {number} A0/A1/A2 descriptor") from exc
        if not (1 <= first_track <= last_track <= 99):
            raise ValueError("Invalid track number range")
        session_tracks = []
        for track_number in range(first_track, last_track + 1):
            try:
                descriptor = entries[number, track_number]
            except KeyError as exc:
                raise ValueError(f"Missing track {track_number} descriptor") from exc
            session_tracks.append(
                Track(track_number, number, descriptor[1] & 0x0F, msf_to_lba(*descriptor[8:11]))
            )
        actual_numbers = {p for s, p in entries if s == number and 1 <= p <= 99}
        if actual_numbers != set(range(first_track, last_track + 1)):
            raise ValueError("Track descriptors disagree with session track range")
        boundaries = [t.start_lba for t in session_tracks] + [leadout]
        if boundaries[0] < 0 or any(a >= b for a, b in pairwise(boundaries)):
            raise ValueError("Invalid or non-increasing track/leadout positions")
        sessions.append(Session(number, first_track, last_track, leadout))
        tracks.extend(session_tracks)
    return TOC(first, last, tuple(sessions), tuple(tracks))

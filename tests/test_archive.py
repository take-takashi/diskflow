import hashlib
import json

import pytest

from diskflow.archive import capture
from diskflow.toc import SECTOR_BYTES


class FakeCD:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.toc_reads = 0
        self.on_read = None

    def read_toc(self):
        self.toc_reads += 1
        return self.raw

    def read_audio(self, lba, sectors):
        self.calls.append((lba, sectors))
        data = b"".join(bytes((n % 251,)) * SECTOR_BYTES for n in range(lba, lba + sectors))
        return self.on_read(self, lba, sectors, data) if self.on_read else data


def run(cd, path, **kwargs):
    return capture(cd, path, device="fake", mode="rip", chunk_sectors=4, **kwargs)


def report(path):
    return json.loads((path / "report.json").read_text())


def test_two_full_passes_match(make_toc, tmp_path):
    cd = FakeCD(make_toc())
    output = tmp_path / "rip"
    result = run(cd, output)
    data = (output / "audio.bin").read_bytes()
    assert len(data) == 11 * SECTOR_BYTES
    assert result["status"] == "reads_matched"
    assert result["archival_verified"] is False
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["passes"][0]["sha256"] == result["passes"][1]["sha256"]
    assert cd.calls == [(0, 4), (4, 4), (8, 3)] * 2
    assert cd.toc_reads == 3
    assert (output / "toc.bin").read_bytes() == cd.raw
    assert (output / "disc.cue").exists()
    assert not (output / "audio.bin.part").exists()
    assert report(output) == result


def test_probe_reads_only_mapped_samples(make_toc, tmp_path):
    cd = FakeCD(make_toc())
    output = tmp_path / "probe"
    result = capture(cd, output, device="fake", mode="probe")
    positions = [0, 2, 4, 5, 8, 10]
    assert cd.calls == [(n, 1) for n in positions] * 2
    assert result["sectors_per_pass"] == len(positions)
    assert result["chunk_sectors"] == 1
    assert not (output / "audio.bin").exists()
    assert not (output / "disc.cue").exists()
    mapping = json.loads((output / "sample-map.json").read_text())["samples"]
    samples = (output / "samples.bin").read_bytes()
    for entry in mapping:
        offset = entry["file_offset"]
        assert samples[offset : offset + SECTOR_BYTES] == bytes((entry["lba"],)) * SECTOR_BYTES


def test_mismatch_fails_without_promoting_image(make_toc, tmp_path):
    cd = FakeCD(make_toc())

    def corrupt(cd, lba, sectors, data):
        if len(cd.calls) > 3 and lba <= 7 < lba + sectors:
            offset = (7 - lba) * SECTOR_BYTES
            return data[:offset] + b"!" + data[offset + 1 :]
        return data

    cd.on_read = corrupt
    output = tmp_path / "mismatch"
    with pytest.raises(OSError, match="pass 2 at LBA 7"):
        run(cd, output)
    assert report(output)["status"] == "failed"
    assert report(output)["passes"][1]["status"] == "failed"
    assert (output / "audio.bin.part").stat().st_size == 11 * SECTOR_BYTES
    assert not (output / "audio.bin").exists()
    assert not (output / "disc.cue").exists()
    events = [json.loads(line) for line in (output / "reads.jsonl").read_text().splitlines()]
    assert events[-1] == {"status": "mismatch", "pass": 2, "lba": 7}


def test_transient_error_is_retried_and_logged(make_toc, tmp_path):
    cd = FakeCD(make_toc())

    def flaky(cd, lba, sectors, data):
        if len(cd.calls) == 1:
            raise OSError("simulated I/O error")
        return data

    cd.on_read = flaky
    output = tmp_path / "retry"
    assert run(cd, output)["status"] == "reads_matched"
    events = [json.loads(line) for line in (output / "reads.jsonl").read_text().splitlines()]
    assert events[0]["status"] == "read_error"
    assert events[1]["attempt"] == 2
    assert events[1]["lba"] == 0


@pytest.mark.parametrize("short", [True, False])
def test_exhausted_retries_never_skip_sectors(make_toc, tmp_path, short):
    cd = FakeCD(make_toc())

    def broken(cd, lba, sectors, data):
        if short:
            return data[:-1]
        raise OSError("unreadable")

    cd.on_read = broken
    output = tmp_path / "failed"
    with pytest.raises(OSError):
        run(cd, output, retries=1)
    assert cd.calls == [(0, 4), (0, 4)]
    assert report(output)["status"] == "failed"
    assert not (output / "audio.bin").exists()
    assert (output / "audio.bin.part").stat().st_size == 0


def test_changed_toc_is_a_failure(make_toc, tmp_path):
    cd = FakeCD(make_toc())

    def change_disc(cd, lba, sectors, data):
        cd.raw = make_toc(leadout=12)
        return data

    cd.on_read = change_disc
    output = tmp_path / "changed"
    with pytest.raises(OSError, match="TOC changed"):
        run(cd, output)
    assert not (output / "audio.bin").exists()
    assert report(output)["status"] == "failed"


def test_interrupted_capture_is_marked_failed(make_toc, tmp_path):
    cd = FakeCD(make_toc())

    def interrupt(*_):
        raise KeyboardInterrupt

    cd.on_read = interrupt
    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        run(cd, output)
    assert report(output)["status"] == "failed"
    assert (output / "audio.bin.part").exists()


def test_existing_output_is_never_overwritten(make_toc, tmp_path):
    cd = FakeCD(make_toc())
    with pytest.raises(FileExistsError):
        run(cd, tmp_path)
    assert cd.toc_reads == 0
    assert not cd.calls


def test_unsupported_cd_retains_toc_but_reads_no_audio(make_toc, tmp_path):
    cd = FakeCD(make_toc(controls=(0, 4)))
    output = tmp_path / "mixed"
    with pytest.raises(ValueError, match="mixed-mode"):
        run(cd, output)
    assert report(output)["status"] == "failed"
    assert (output / "toc.bin").read_bytes() == cd.raw
    assert not cd.calls


@pytest.mark.parametrize("kwargs", [{"passes": 1}, {"retries": -1}, {"chunk_sectors": 0}])
def test_invalid_options_create_no_output(make_toc, tmp_path, kwargs):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        capture(FakeCD(make_toc()), output, device="fake", mode="rip", **kwargs)
    assert not output.exists()

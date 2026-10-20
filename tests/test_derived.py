import hashlib
import json
import wave

import pytest

from diskflow.derived import compare_cddafs, split_wav
from diskflow.image import SavedImage
from diskflow.toc import SECTOR_BYTES


def read_report(output):
    return json.loads((output / "report.json").read_text())


def test_full_pcm_comparison(captured, mounted, tmp_path, monkeypatch):
    monkeypatch.setattr("diskflow.derived.CHUNK_BYTES", SECTOR_BYTES * 2)
    directory, pcm = captured
    output = tmp_path / "comparison"
    progress = []
    result = compare_cddafs(
        directory, mounted, output, progress=lambda *args: progress.append(args)
    )
    assert result["status"] == "pcm_matched"
    assert result["compared_bytes"] == len(pcm)
    assert result["archival_verified"] is False
    assert result["uncompared_prefix_sectors"] == 0
    assert [t["compared_bytes"] for t in result["tracks"]] == [5 * SECTOR_BYTES, 6 * SECTOR_BYTES]
    assert progress[-1] == (2, 6 * SECTOR_BYTES, 6 * SECTOR_BYTES)
    assert read_report(output) == result
    assert (directory / "audio.bin").read_bytes() == pcm


@pytest.mark.parametrize("container,compression", [(b"AIFF", b"NONE"), (b"AIFC", b"twos")])
def test_big_endian_pcm_is_compared_in_little_endian(
    captured, make_mount, tmp_path, container, compression
):
    mount = make_mount(tmp_path / "mount", captured, container=container, compression=compression)
    result = compare_cddafs(captured[0], mount, tmp_path / "comparison")
    assert result["status"] == "pcm_matched"
    assert result["tracks"][0]["aiff"]["byte_order"] == "big"


def test_difference_outside_previous_sample_positions_is_detected(captured, mounted, tmp_path):
    path = mounted / "1 曲.aiff"
    data = bytearray(path.read_bytes())
    from diskflow.aiff import read_pcm_info

    with path.open("rb") as source:
        offset = read_pcm_info(source).data_offset
    data[offset + SECTOR_BYTES + 17] ^= 1  # LBA 1 was not sampled by probe.
    path.write_bytes(data)
    output = tmp_path / "mismatch"
    with pytest.raises(ValueError, match="track 1 at LBA 1"):
        compare_cddafs(captured[0], mounted, output)
    result = read_report(output)
    assert result["status"] == "failed"
    assert result["tracks"][0]["first_mismatch"] == {
        "lba": 1,
        "byte_offset_in_sector": 17,
        "byte_offset_in_track": SECTOR_BYTES + 17,
    }


@pytest.mark.parametrize("change", ["missing", "duplicate", "unnumbered"])
def test_ambiguous_track_files_refused(captured, mounted, tmp_path, change):
    track = mounted / "1 曲.aiff"
    if change == "missing":
        track.unlink()
    elif change == "duplicate":
        (mounted / "1 Another.aiff").write_bytes(track.read_bytes())
    else:
        track.rename(mounted / "Unknown.aiff")
    output = tmp_path / "comparison"
    with pytest.raises(ValueError):
        compare_cddafs(captured[0], mounted, output)
    assert read_report(output)["status"] == "failed"


def test_numeric_not_lexicographic_track_order(make_capture, make_mount, tmp_path):
    captured = make_capture(tmp_path / "capture", starts=tuple(range(11)), leadout=11)
    mount = make_mount(tmp_path / "mount", captured)
    result = compare_cddafs(captured[0], mount, tmp_path / "comparison")
    assert [t["number"] for t in result["tracks"]] == list(range(1, 12))


def test_missing_cd_is_not_reported_as_match(captured, tmp_path):
    output = tmp_path / "comparison"
    with pytest.raises(FileNotFoundError):
        compare_cddafs(captured[0], tmp_path / "no-cd", output)
    assert read_report(output)["status"] == "failed"


def test_wrong_mounted_toc_fails_before_audio_comparison(captured, mounted, tmp_path):
    import plistlib

    (mounted / ".TOC.plist").write_bytes(plistlib.dumps({"Format 0x02 TOC Data": b"wrong"}))
    output = tmp_path / "comparison"
    with pytest.raises(ValueError, match="TOC disagrees"):
        compare_cddafs(captured[0], mounted, output)
    result = read_report(output)
    assert result["status"] == "failed"
    assert result["tracks"] == []


def test_track_pcm_length_must_match_toc(captured, mounted, make_aiff, tmp_path):
    (mounted / "1 曲.aiff").write_bytes(make_aiff(bytes(SECTOR_BYTES)))
    output = tmp_path / "comparison"
    with pytest.raises(ValueError, match="PCM length disagrees with TOC"):
        compare_cddafs(captured[0], mounted, output)
    assert read_report(output)["status"] == "failed"


def test_changed_toc_during_comparison(captured, mounted, tmp_path):
    output = tmp_path / "comparison"

    def change(*_):
        (mounted / ".TOC.plist").unlink(missing_ok=True)

    with pytest.raises(FileNotFoundError):
        compare_cddafs(captured[0], mounted, output, progress=change)
    assert read_report(output)["status"] == "failed"


def test_initial_prefix_is_not_claimed_as_compared(make_capture, make_mount, tmp_path):
    captured = make_capture(tmp_path / "capture", starts=(2, 5))
    mount = make_mount(tmp_path / "mount", captured)
    result = compare_cddafs(captured[0], mount, tmp_path / "comparison")
    assert result["uncompared_prefix_sectors"] == 2
    assert result["compared_bytes"] == len(captured[1]) - 2 * SECTOR_BYTES


def test_wav_extraction_preserves_every_pcm_byte(captured, tmp_path, monkeypatch):
    monkeypatch.setattr("diskflow.derived.CHUNK_BYTES", SECTOR_BYTES * 2)
    directory, pcm = captured
    original_files = {p.name: p.read_bytes() for p in directory.iterdir()}
    output = tmp_path / "wav"
    result = split_wav(directory, output)
    assert result["status"] == "wav_written"
    assert result["archival_verified"] is False
    pieces = []
    for entry in result["tracks"]:
        path = output / entry["file"]
        assert not (output / (entry["file"] + ".part")).exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["wav_sha256"]
        with wave.open(str(path), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                2,
                2,
                44100,
            )
            data = audio.readframes(audio.getnframes())
            assert len(data) == entry["pcm_bytes"]
            assert hashlib.sha256(data).hexdigest() == entry["pcm_sha256"]
            pieces.append(data)
    assert b"".join(pieces) == pcm
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == original_files
    assert read_report(output) == result


def test_positive_lba_pregap_is_not_discarded(make_capture, tmp_path):
    directory, pcm = make_capture(tmp_path / "capture", starts=(2, 5))
    output = tmp_path / "wav"
    result = split_wav(directory, output)
    assert result["tracks"][0]["kind"] == "pregap"
    assert result["tracks"][0]["file"] == "pregap.wav"
    assert result["tracks"][0]["pre_emphasis"] is None  # TOC does not describe this prefix.
    with wave.open(str(output / "pregap.wav"), "rb") as audio:
        assert audio.readframes(audio.getnframes()) == pcm[: 2 * SECTOR_BYTES]
    assert sum(t["pcm_bytes"] for t in result["tracks"]) == len(pcm)


def test_pre_emphasis_is_recorded_without_dsp(make_capture, tmp_path):
    directory, pcm = make_capture(tmp_path / "capture", controls=(1, 0))
    output = tmp_path / "wav"
    result = split_wav(directory, output)
    assert result["tracks"][0]["pre_emphasis"] is True
    with wave.open(str(output / "track-01.wav"), "rb") as audio:
        assert audio.readframes(audio.getnframes()) == pcm[: 5 * SECTOR_BYTES]


@pytest.mark.parametrize("operation", ["compare", "split"])
def test_outputs_never_overwrite(captured, mounted, tmp_path, operation):
    with pytest.raises(FileExistsError):
        if operation == "compare":
            compare_cddafs(captured[0], mounted, tmp_path)
        else:
            split_wav(captured[0], tmp_path)
    assert not (tmp_path / "report.json").exists()


def test_interrupted_split_keeps_partials_only(captured, tmp_path):
    output = tmp_path / "wav"

    def interrupt(*_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        split_wav(captured[0], output, progress=interrupt)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.wav"))
    assert list(output.glob("*.part"))


def test_corrupted_wav_is_not_promoted(captured, tmp_path, monkeypatch):
    original = wave.Wave_write.writeframesraw

    def corrupt(self, data):
        return original(self, bytes((data[0] ^ 1,)) + data[1:])

    monkeypatch.setattr(wave.Wave_write, "writeframesraw", corrupt)
    output = tmp_path / "wav"
    with pytest.raises(ValueError, match="WAV PCM hash disagrees"):
        split_wav(captured[0], output)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.wav"))
    assert list(output.glob("*.part"))


def test_image_changed_during_split_is_not_promoted(captured, tmp_path):
    directory, _ = captured
    output = tmp_path / "wav"
    changed = False

    def change(*_):
        nonlocal changed
        if not changed:
            with (directory / "audio.bin").open("r+b") as audio:
                audio.seek(10 * SECTOR_BYTES)
                audio.write(b"corrupt")
            changed = True

    with pytest.raises(ValueError, match="changed|SHA-256"):
        split_wav(directory, output, progress=change)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.wav"))


@pytest.mark.parametrize("damage", ["status", "mode", "audio_hash", "toc_hash", "size"])
def test_saved_image_must_match_capture_report(captured, damage):
    directory, _ = captured
    path = directory / "report.json"
    data = json.loads(path.read_text())
    key, value = {
        "status": ("status", "failed"),
        "mode": ("mode", "probe"),
        "audio_hash": ("sha256", "0" * 64),
        "toc_hash": ("toc_sha256", "0" * 64),
        "size": ("expected_bytes", 1),
    }[damage]
    data[key] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        SavedImage.load(directory)


@pytest.mark.parametrize("change", [b"", b"truncated", b"extra" * 10000])
def test_wrong_image_size_refused(captured, tmp_path, change):
    directory, _ = captured
    (directory / "audio.bin").write_bytes(change)
    output = tmp_path / "wav"
    with pytest.raises(ValueError, match="size or SHA-256"):
        split_wav(directory, output)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.wav*"))

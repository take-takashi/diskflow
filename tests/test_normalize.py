import hashlib
import json
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from diskflow.loudness import measure_loudness
from diskflow.normalize import calculate_gain_db, normalize_wavs
from diskflow.wavs import load_wavs


def sine_pcm(amplitude=2000, frames=88200):
    return b"".join(
        struct.pack(
            "<hh",
            int(amplitude * math.sin(n * math.tau * 440 / 44100)),
            int(amplitude * math.sin(n * math.tau * 880 / 44100)),
        )
        for n in range(frames)
    )


def wav_set(directory: Path, pcm: bytes):
    directory.mkdir()
    path = directory / "track-01.wav"
    frames = len(pcm) // 4
    with wave.open(str(path), "wb") as audio:
        audio.setparams((2, 2, 44100, frames, "NONE", "not compressed"))
        audio.writeframes(pcm)
    report = {
        "operation": "split",
        "status": "wav_written",
        "tracks": [
            {
                "number": 1,
                "kind": "track",
                "status": "complete",
                "file": path.name,
                "frames": frames,
                "pcm_bytes": len(pcm),
                "pre_emphasis": False,
                "wav_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
            }
        ],
    }
    (directory / "report.json").write_text(json.dumps(report))
    return directory


def report_at(path):
    return json.loads((path / "report.json").read_text())


@pytest.fixture
def tone(tmp_path):
    return wav_set(tmp_path / "source", sine_pcm())


@pytest.mark.parametrize(
    "i,tp,expected",
    [
        (-22, -4, 2.95),
        (-10, -5, -4),
        (None, None, 0),
        (None, -0.5, -0.55),
    ],
)
def test_fixed_gain_matches_reference_with_peak_guard(i, tp, expected):
    assert calculate_gain_db(i, tp, -14, -1) == pytest.approx(expected)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_invalid_measurements_are_not_silently_treated_as_silence(bad):
    with pytest.raises(ValueError):
        calculate_gain_db(bad, -1, -14, -1)


def test_meter_catches_peak_in_partial_final_100ms(tmp_path):
    pcm = bytes((44673 - 8) * 4) + struct.pack("<hh", 30000, -30000) * 8
    source = wav_set(tmp_path / "tail", pcm)
    measured = measure_loudness(source / "track-01.wav")
    assert measured.frames == 44673
    assert measured.integrated_lufs is None  # Original-duration signal was below the R128 gate.
    assert measured.true_peak_dbtp > 0
    assert measured.sample_peak_dbfs > -1
    assert 4410 <= measured.analysis_padding_frames < 8820


def test_normalizes_waveform_preserving_source_frames_and_dynamics(tone, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")  # No FFmpeg/ffprobe or other executable can be used.
    original = {p.name: p.read_bytes() for p in tone.iterdir()}
    output = tmp_path / "normalized"
    result = normalize_wavs(tone, output)
    track = result["tracks"][0]
    assert result["status"] == "normalized"
    assert result["all_targets_reached"] is True
    assert result["normalization"]["limiter"] is False
    assert track["after"]["integrated_lufs"] == pytest.approx(-14, abs=0.1)
    assert track["after"]["true_peak_upper_dbtp"] <= -1
    assert track["after"]["frames"] == track["frames"] == 88200
    assert track["dither"] == "TPDF"
    assert track["pcm_sha256"] != track["source_pcm_sha256"]
    assert not list(output.glob("*.part"))
    assert {p.name: p.read_bytes() for p in tone.iterdir()} == original
    verified = load_wavs(output)
    assert verified.normalization["true_peak_dbtp"] == -1
    verified.verify_unchanged()
    # Fixed gain only: samples differ from linear scaling by at most quantization/dither error.
    with wave.open(str(tone / "track-01.wav"), "rb") as audio:
        before = struct.unpack("<" + "h" * 4000, audio.readframes(2000))
    with wave.open(str(output / "track-01.wav"), "rb") as audio:
        after = struct.unpack("<" + "h" * 4000, audio.readframes(2000))
    factor = 10 ** (track["gain_db"] / 20)
    assert max(abs(new - old * factor) for old, new in zip(before, after, strict=True)) < 3


def test_peak_limited_track_is_not_falsely_reported_at_target(tmp_path):
    pcm = sine_pcm(frames=88200) + struct.pack("<hh", 30000, -30000) * 573
    source = wav_set(tmp_path / "source", pcm)
    result = normalize_wavs(source, tmp_path / "normalized")
    track = result["tracks"][0]
    assert result["all_targets_reached"] is False
    assert track["result"] == "peak_limited"
    assert track["target_reached"] is False
    assert track["gain_db"] < 0
    assert track["after"]["true_peak_upper_dbtp"] <= -1
    assert track["after"]["integrated_lufs"] < -14.1


def test_silence_is_preserved_without_invalid_json(tmp_path):
    source = wav_set(tmp_path / "source", bytes(88200 * 4))
    output = tmp_path / "normalized"
    result = normalize_wavs(source, output)
    track = result["tracks"][0]
    assert track["result"] == "silence"
    assert track["gain_db"] == 0
    assert track["after"]["integrated_lufs"] is None
    assert track["after"]["true_peak_dbtp"] is None
    assert track["dither"] == "not_applied"
    assert (source / "track-01.wav").read_bytes() == (output / "track-01.wav").read_bytes()

    def reject_non_json(value):
        raise AssertionError(f"Non-JSON value: {value}")

    json.loads((output / "report.json").read_text(), parse_constant=reject_non_json)


def test_very_short_signal_is_not_boosted_as_minus_70_lufs(tmp_path):
    source = wav_set(tmp_path / "source", sine_pcm(frames=1000))
    result = normalize_wavs(source, tmp_path / "normalized")
    track = result["tracks"][0]
    assert track["before"]["integrated_lufs"] is None
    assert track["gain_db"] == 0
    assert track["result"] == "below_gate_or_too_short"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_lufs": math.nan},
        {"target_lufs": 1},
        {"true_peak_dbtp": math.inf},
        {"true_peak_dbtp": 1},
        {"peak_headroom_db": -1},
    ],
)
def test_invalid_policy_creates_no_output(tone, tmp_path, kwargs):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        normalize_wavs(tone, output, **kwargs)
    assert not output.exists()


def test_existing_output_is_never_overwritten(tone, tmp_path):
    with pytest.raises(FileExistsError):
        normalize_wavs(tone, tmp_path)
    assert not (tmp_path / "report.json").exists()


def test_renormalizing_processed_wavs_is_refused(tone, tmp_path):
    output = tmp_path / "normalized"
    normalize_wavs(tone, output)
    second = tmp_path / "second"
    with pytest.raises(ValueError, match="completed split"):
        normalize_wavs(output, second)
    assert report_at(second)["status"] == "failed"


def test_ignored_gain_cannot_produce_a_false_peak_pass(tmp_path, monkeypatch):
    source = wav_set(tmp_path / "source", sine_pcm(amplitude=30000))
    monkeypatch.setattr(
        "diskflow.normalize._write_gain", lambda src, dst, gain: shutil.copyfile(src, dst)
    )
    output = tmp_path / "normalized"
    with pytest.raises(ValueError, match="still exceeds"):
        normalize_wavs(source, output)
    assert report_at(output)["status"] == "failed"
    assert len(report_at(output)["tracks"][0]["attempts"]) == 3
    assert not list(output.glob("*.wav"))
    assert list(output.glob("*.part"))


def test_input_corruption_is_refused(tone, tmp_path):
    (tone / "track-01.wav").write_bytes(b"corrupt")
    output = tmp_path / "normalized"
    with pytest.raises(ValueError, match="WAV hash"):
        normalize_wavs(tone, output)
    assert report_at(output)["status"] == "failed"


def test_source_changed_after_processing_prevents_promotion(tone, tmp_path):
    def change(number, done, total):
        if done == total:
            with (tone / "track-01.wav").open("r+b") as source:
                source.seek(44)
                source.write(b"changed")

    output = tmp_path / "normalized"
    with pytest.raises(ValueError, match="Source WAV changed"):
        normalize_wavs(tone, output, progress=change)
    assert report_at(output)["status"] == "failed"
    assert not list(output.glob("*.wav"))


def test_interruption_is_recorded(tone, tmp_path):
    def interrupt(*_):
        raise KeyboardInterrupt

    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        normalize_wavs(tone, output, progress=interrupt)
    assert report_at(output)["status"] == "failed"


def test_remeasurement_miss_is_not_mislabeled_as_peak_limited(tone, tmp_path, monkeypatch):
    from dataclasses import replace

    original = measure_loudness
    calls = 0

    def shifted_result(path):
        nonlocal calls
        measured = original(path)
        calls += 1
        return replace(measured, integrated_lufs=-14.5) if calls > 1 else measured

    monkeypatch.setattr("diskflow.normalize.measure_loudness", shifted_result)
    result = normalize_wavs(tone, tmp_path / "normalized")
    assert result["all_targets_reached"] is False
    assert result["tracks"][0]["result"] == "measurement_target_miss"


def test_cli_explicit_policy_values(tone, tmp_path):
    from diskflow.cli import main

    output = tmp_path / "normalized"
    assert (
        main(
            [
                "normalize",
                str(tone),
                "--output",
                str(output),
                "--target-lufs",
                "-18",
                "--true-peak",
                "-2",
                "--peak-headroom",
                "1.0",
            ]
        )
        == 0
    )
    policy = report_at(output)["normalization"]
    assert policy["target_lufs"] == -18
    assert policy["true_peak_dbtp"] == -2
    assert policy["peak_headroom_db"] == 1


def test_cli_normalization(tone, tmp_path, capsys):
    from diskflow.cli import main

    output = tmp_path / "normalized"
    assert main(["normalize", str(tone), "--output", str(output)]) == 0
    assert "Fixed Track Gain applied" in capsys.readouterr().out

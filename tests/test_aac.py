import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from diskflow.aac import _parse_afinfo, encode_aac


def afinfo_xml(frames):
    priming = 2112
    remainder = (-(frames + priming)) % 1024
    return f"""<?xml version="1.0"?>
<audio_info xmlns="http://apple.com/core_audio/audio_info"><audio_file>
<file_type>'m4af'</file_type><tracks><track>
<num_channels>2</num_channels><sample_rate>44100</sample_rate><format_type>aac </format_type>
<duration>{frames / 44100:.6f}</duration><bit_rate>250000</bit_rate>
<packet_table_info><total_frames>{frames + priming + remainder}</total_frames>
<valid_frames>{frames}</valid_frames><priming_frames>{priming}</priming_frames>
<remainder_frames>{remainder}</remainder_frames></packet_table_info>
</track></tracks></audio_file></audio_info>"""


@pytest.fixture
def fake_tools(monkeypatch):
    monkeypatch.setattr("diskflow.aac.sys.platform", "darwin")
    state = SimpleNamespace(
        calls=[],
        fail_stage=None,
        timeout_stage=None,
        interrupt=False,
        decoded_frames_delta=0,
        empty_output=False,
        xml=afinfo_xml,
    )
    sources = {}

    def run(argv, **kwargs):
        assert kwargs == dict(
            capture_output=True,
            text=True,
            timeout=30 if argv[0].endswith("afinfo") else 600,
            check=False,
        )
        stage = (
            "info" if argv[0].endswith("afinfo") else ("encode" if argv[2] == "m4af" else "decode")
        )
        state.calls.append((stage, argv))
        if state.interrupt:
            raise KeyboardInterrupt
        if stage == state.timeout_stage:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if stage == state.fail_stage:
            if stage == "encode":
                Path(argv[-1]).write_bytes(b"incomplete output")
            return subprocess.CompletedProcess(argv, 23, "", "simulated tool error")
        if stage == "encode":
            source, output = Path(argv[-2]), Path(argv[-1])
            assert ["-b", "256000"] == argv[5:7]
            assert "Stereo" in argv
            output.write_bytes(b"" if state.empty_output else b"simulated AAC file")
            sources[output] = source
            return subprocess.CompletedProcess(argv, 0, "", "")
        if stage == "info":
            with wave.open(str(sources[Path(argv[-1])]), "rb") as audio:
                frames = audio.getnframes()
            return subprocess.CompletedProcess(argv, 0, state.xml(frames), "")
        source = sources[Path(argv[-2])]
        with wave.open(str(source), "rb") as audio:
            frames = audio.getnframes() + state.decoded_frames_delta
        with wave.open(argv[-1], "wb") as decoded:
            decoded.setparams((2, 2, 44100, frames, "NONE", "not compressed"))
            decoded.writeframes(b"\x10\x20\x30\x40" * frames)  # Lossy output differs from source.
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(
        "diskflow.aac.subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    return state


def read_report(output):
    return json.loads((output / "report.json").read_text())


def test_encode_records_baseline_and_fully_decodes(wavs, fake_tools, tmp_path):
    before = {p.name: p.read_bytes() for p in wavs.iterdir()}
    output = tmp_path / "aac"
    result = encode_aac(wavs, output)
    assert result["status"] == "aac_encoded"
    assert result["loudness_normalized"] is False
    assert result["metadata_tagged"] is False
    assert result["archival_verified"] is False
    assert result["itunes_plus_equivalence_verified"] is False
    assert result["encoder"]["bitrate_bps"] == 256000
    assert [stage for stage, _ in fake_tools.calls] == ["encode", "info", "decode"] * 2
    assert [t["file"] for t in result["tracks"]] == ["track-01.m4a", "track-02.m4a"]
    assert all(t["decoded_frames"] == t["source_frames"] for t in result["tracks"])
    assert all(t["status"] == "complete" for t in result["tracks"])
    assert len(list(output.glob("*.m4a"))) == 2
    assert not list(output.glob("*.part"))
    assert not list(output.glob(".*.decoded.wav"))
    assert {p.name: p.read_bytes() for p in wavs.iterdir()} == before
    events = [json.loads(line) for line in (output / "commands.jsonl").read_text().splitlines()]
    assert len(events) == 6 and all(e["returncode"] == 0 for e in events)
    assert read_report(output) == result


@pytest.mark.parametrize("stage", ["encode", "info", "decode"])
def test_tool_errors_leave_evidence_not_final_audio(wavs, fake_tools, tmp_path, stage):
    fake_tools.fail_stage = stage
    output = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="simulated tool error"):
        encode_aac(wavs, output)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.m4a"))
    assert list(output.glob("*.part"))
    events = [json.loads(line) for line in (output / "commands.jsonl").read_text().splitlines()]
    assert events[-1]["returncode"] == 23
    assert events[-1]["stderr"] == "simulated tool error"


def test_timeout_is_reported(wavs, fake_tools, tmp_path):
    fake_tools.timeout_stage = "encode"
    output = tmp_path / "timeout"
    with pytest.raises(RuntimeError, match="timed out"):
        encode_aac(wavs, output)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.m4a"))


def test_decode_frame_mismatch_is_failure(wavs, fake_tools, tmp_path):
    fake_tools.decoded_frames_delta = 1
    output = tmp_path / "decode-mismatch"
    with pytest.raises(ValueError, match="format/frame count"):
        encode_aac(wavs, output)
    assert read_report(output)["status"] == "failed"
    assert list(output.glob(".*.decoded.wav"))
    assert not list(output.glob("*.m4a"))


def test_empty_encoded_output_is_not_success(wavs, fake_tools, tmp_path):
    fake_tools.empty_output = True
    output = tmp_path / "empty"
    with pytest.raises(ValueError, match="produced no audio"):
        encode_aac(wavs, output)
    assert read_report(output)["status"] == "failed"


@pytest.mark.parametrize("change", ["incomplete", "path", "emphasis", "pcm_hash", "frames"])
def test_invalid_split_manifest_refused_before_encoding(wavs, fake_tools, tmp_path, change):
    path = wavs / "report.json"
    data = json.loads(path.read_text())
    key, value = {
        "incomplete": ("status", "running"),
        "path": ("file", "../track-01.wav"),
        "emphasis": ("pre_emphasis", True),
        "pcm_hash": ("pcm_sha256", "0" * 64),
        "frames": ("frames", -1),
    }[change]
    data["tracks"][0][key] = value
    path.write_text(json.dumps(data))
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        encode_aac(wavs, output)
    assert not fake_tools.calls
    assert read_report(output)["status"] == "failed"


def test_corrupted_source_wav_refused(wavs, fake_tools, tmp_path):
    (wavs / "track-01.wav").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="WAV hash"):
        encode_aac(wavs, tmp_path / "aac")
    assert not fake_tools.calls


def test_source_changed_after_encoding_prevents_promotion(wavs, fake_tools, tmp_path):
    output = tmp_path / "aac"
    changed = False

    def change(number, completed, total):
        nonlocal changed
        if number == 1 and completed == total and not changed:
            with (wavs / "track-01.wav").open("r+b") as source:
                source.seek(44)
                source.write(b"corrupt")
            changed = True

    with pytest.raises(ValueError, match="Source WAV changed"):
        encode_aac(wavs, output, progress=change)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.m4a"))


def test_changed_split_report_prevents_promotion(wavs, fake_tools, tmp_path):
    output = tmp_path / "aac"

    def change(number, completed, total):
        if number == 1 and completed == total:
            path = wavs / "report.json"
            path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="Source split report changed"):
        encode_aac(wavs, output, progress=change)
    assert read_report(output)["status"] == "failed"
    assert not list(output.glob("*.m4a"))


def test_encode_cli_uses_verified_wavs(wavs, fake_tools, tmp_path, capsys):
    from diskflow.cli import main

    output = tmp_path / "aac"
    assert main(["encode", str(wavs), "--output", str(output)]) == 0
    text = capsys.readouterr().out
    assert "aac_encoded" in text
    assert "normalization and metadata tagging are NOT applied" in text


def test_interrupt_marks_failure(wavs, fake_tools, tmp_path):
    fake_tools.interrupt = True
    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        encode_aac(wavs, output)
    assert read_report(output)["status"] == "failed"


def test_existing_directory_never_overwritten(wavs, fake_tools, tmp_path):
    with pytest.raises(FileExistsError):
        encode_aac(wavs, tmp_path)
    assert not fake_tools.calls
    assert not (tmp_path / "report.json").exists()


def test_pregap_is_explicitly_skipped(make_capture, fake_tools, tmp_path):
    from diskflow.derived import split_wav

    captured, _ = make_capture(tmp_path / "capture", starts=(2, 5))
    wavs = tmp_path / "wavs"
    split_wav(captured, wavs)
    result = encode_aac(wavs, tmp_path / "aac")
    assert result["skipped_files"] == ["pregap.wav"]
    assert len(result["tracks"]) == 2


def test_requires_macos(wavs, monkeypatch, tmp_path):
    monkeypatch.setattr("diskflow.aac.sys.platform", "linux")
    with pytest.raises(RuntimeError, match="requires macOS"):
        encode_aac(wavs, tmp_path / "aac")
    assert not (tmp_path / "aac").exists()


@pytest.mark.parametrize(
    "old,new",
    [
        ("<format_type>aac </format_type>", "<format_type>alac</format_type>"),
        ("<sample_rate>44100</sample_rate>", "<sample_rate>48000</sample_rate>"),
        ("<num_channels>2</num_channels>", "<num_channels>1</num_channels>"),
        ("<valid_frames>1000</valid_frames>", "<valid_frames>1001</valid_frames>"),
        ("<priming_frames>2112</priming_frames>", "<priming_frames>0</priming_frames>"),
        ("<file_type>'m4af'</file_type>", "<file_type>'WAVE'</file_type>"),
        ("<bit_rate>250000</bit_rate>", ""),
        ("<duration>0.022676</duration>", "<duration>nan</duration>"),
    ],
)
def test_wrong_codec_format_or_packet_info_rejected(old, new):
    with pytest.raises(ValueError):
        _parse_afinfo(afinfo_xml(1000).replace(old, new), 1000)


def test_afinfo_warnings_are_retained_but_errors_fail():
    def xml(level):
        alert = (
            f'<alerts><alert level="{level}" code="test">'
            "<description>detail</description></alert></alerts>"
        )
        return afinfo_xml(1000).replace("</audio_file>", alert + "</audio_file>")

    result = _parse_afinfo(xml("WARNING"), 1000)
    assert result["alerts"] == [{"level": "WARNING", "code": "test", "description": "detail"}]
    with pytest.raises(ValueError, match="afinfo reported an error"):
        _parse_afinfo(xml("ERROR"), 1000)


@pytest.fixture
def normalized_wavs(wavs, tmp_path):
    from diskflow.normalize import normalize_wavs

    output = tmp_path / "normalized"
    normalize_wavs(wavs, output)
    return output


def mock_decoded_loudness(monkeypatch, peak):
    from diskflow.loudness import Loudness

    def measure(path):
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
        return Loudness(frames, -14.0, peak - 0.01, peak, peak - 0.1, 4410)

    monkeypatch.setattr("diskflow.loudness.measure_loudness", measure)


@pytest.mark.parametrize("peak", [-1.1, -1.01, -0.99, -0.2])
def test_normalized_aac_float_decode_peak_ceiling(
    normalized_wavs, fake_tools, tmp_path, monkeypatch, peak
):
    mock_decoded_loudness(monkeypatch, peak)
    output = tmp_path / "aac"
    if peak > -1:
        with pytest.raises(ValueError, match="more --peak-headroom"):
            encode_aac(normalized_wavs, output)
        assert read_report(output)["status"] == "failed"
        assert not list(output.glob("*.m4a"))
        assert list(output.glob(".*.decoded.caf"))
    else:
        result = encode_aac(normalized_wavs, output)
        assert result["loudness_normalized"] is True
        assert result["all_targets_reached"] is True
        assert all(t["decoded_loudness"]["true_peak_upper_dbtp"] <= -1 for t in result["tracks"])
        assert not list(output.glob(".*.decoded.caf"))
    decode_commands = [argv for stage, argv in fake_tools.calls if stage == "decode"]
    assert decode_commands
    assert decode_commands[0][1:5] == ["-f", "caff", "-d", "LEF32"]


def test_normalized_aac_decoded_frame_count_is_checked(
    normalized_wavs, fake_tools, tmp_path, monkeypatch
):
    fake_tools.decoded_frames_delta = 1
    mock_decoded_loudness(monkeypatch, -2)
    with pytest.raises(ValueError, match="Decoded AAC frame count"):
        encode_aac(normalized_wavs, tmp_path / "aac")


@pytest.mark.parametrize("bad", [float("nan"), 1.0, True])
def test_invalid_normalization_ceiling_cannot_bypass_aac_check(
    normalized_wavs, fake_tools, tmp_path, bad
):
    path = normalized_wavs / "report.json"
    data = json.loads(path.read_text())
    data["normalization"]["true_peak_dbtp"] = bad
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="normalization policy"):
        encode_aac(normalized_wavs, tmp_path / "aac")
    assert not fake_tools.calls


def test_invalid_xml_rejected():
    with pytest.raises(ValueError, match="invalid XML"):
        _parse_afinfo("not XML", 1000)


def test_priming_is_recorded_not_mistaken_for_valid_audio():
    info = _parse_afinfo(afinfo_xml(1000), 1000)
    assert info["packet_table"]["valid_frames"] == 1000
    assert info["packet_table"]["priming_frames"] == 2112
    assert info["packet_table"]["total_frames"] > 1000

import json
import plistlib
import subprocess
import sys

from diskflow.cli import _plist_command, inspect_mount, main


def test_inspect_mount_retains_plist_and_parses_full_toc(make_toc, tmp_path, monkeypatch):
    monkeypatch.setattr("diskflow.cli._plist_command", lambda _: {"DeviceIdentifier": "disk99"})
    data = {
        "Format 0x02 TOC Data": make_toc(),
        "Sessions": [{"Track Array": [{"Point": 1, "Start Block": 150}], "Leadout Block": 161}],
    }
    (tmp_path / ".TOC.plist").write_bytes(plistlib.dumps(data))
    (tmp_path / "1 Audio Track.aiff").write_bytes(b"fake file, not real audio")
    result = inspect_mount(tmp_path)
    assert result["toc_plist"] == data
    assert result["parsed_toc"]["tracks"][0]["start_lba"] == 0
    assert result["parsed_toc"]["sessions"][0]["leadout_lba"] == 11
    assert result["track_files"][0]["name"] == "1 Audio Track.aiff"


def test_inspect_saves_json_without_overwriting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("diskflow.cli.inspect", lambda _: {"raw": b"\x00\xff", "mounts": []})
    path = tmp_path / "inspect.json"
    args = ["inspect", "--output", str(path)]
    assert main(args) == 0
    assert json.loads(path.read_text())["raw"] == {"encoding": "hex", "data": "00ff"}
    assert main(args) == 1
    assert "error:" in capsys.readouterr().err


def test_empty_ioreg_output_means_no_media(monkeypatch):
    monkeypatch.setattr(
        "diskflow.cli.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"", b""),
    )
    assert _plist_command(["/usr/sbin/ioreg"], empty_result=[]) == []
    assert "error" in _plist_command(["/usr/sbin/diskutil"])


def test_split_cli_never_opens_a_cd(captured, tmp_path, monkeypatch, capsys):
    def no_cd(*_):
        raise AssertionError("Offline extraction must not access a CD device")

    monkeypatch.setattr("diskflow.cli.DarwinCD", no_cd)
    output = tmp_path / "wav"
    assert main(["split", str(captured[0]), "--output", str(output)]) == 0
    assert "wav_written" in capsys.readouterr().out
    assert (output / "track-01.wav").exists()


def test_compare_cli_checks_all_pcm(captured, mounted, tmp_path, capsys):
    output = tmp_path / "compare"
    assert (
        main(["compare", str(captured[0]), "--mount", str(mounted), "--output", str(output)]) == 0
    )
    report = json.loads((output / "report.json").read_text())
    assert report["compared_bytes"] == len(captured[1])
    assert "pcm_matched" in capsys.readouterr().out


def test_compare_cli_with_no_cd_returns_error(captured, tmp_path, capsys):
    output = tmp_path / "compare"
    assert (
        main(
            [
                "compare",
                str(captured[0]),
                "--mount",
                str(tmp_path / "no-cd"),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads((output / "report.json").read_text())["status"] == "failed"
    assert "error:" in capsys.readouterr().err


def test_cd_cli_and_help_do_not_import_pyav():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from diskflow.cli import _parser; "
            "_parser(); assert 'av' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_bad_device_does_not_open_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("diskflow.darwin.sys.platform", "darwin")
    output = tmp_path / "probe"
    assert main(["probe", "/dev/disk0", "--output", str(output)]) == 1
    assert "whole raw CD device" in capsys.readouterr().err
    assert not output.exists()

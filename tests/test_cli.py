import json
import plistlib
import subprocess

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


def test_bad_device_does_not_open_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("diskflow.darwin.sys.platform", "darwin")
    output = tmp_path / "probe"
    assert main(["probe", "/dev/disk0", "--output", str(output)]) == 1
    assert "whole raw CD device" in capsys.readouterr().err
    assert not output.exists()

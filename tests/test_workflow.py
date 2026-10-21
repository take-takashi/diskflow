import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import diskflow.workflow as workflow
from diskflow.archive import write_json
from diskflow.image import file_sha256


@pytest.fixture
def local_pipeline(monkeypatch, fake_metadata, encoded_wavs):
    """Mock external metadata/Apple encoding; keep real copy/split/gain/tag stages."""

    def encode(directory, output, *, progress=None):
        output.mkdir()
        source = json.loads((directory / "report.json").read_bytes())
        result = json.loads((encoded_wavs / "report.json").read_bytes())
        for entry in result["tracks"]:
            shutil.copyfile(encoded_wavs / entry["file"], output / entry["file"])
        result.update(
            source_directory=str(directory.resolve()),
            source_manifest_sha256=file_sha256(directory / "report.json"),
            loudness_normalized=True,
            all_targets_reached=source["all_targets_reached"],
            source_normalization=source["normalization"],
        )
        write_json(output / "report.json", result)
        return result

    monkeypatch.setattr(workflow, "encode_aac", encode)

    def no_cd(*_):
        raise AssertionError("Saved captures must not open any device")

    monkeypatch.setattr(workflow, "DarwinCD", no_cd)


@pytest.fixture
def local_cover(tmp_path, cover_bytes):
    path = tmp_path / "カバー画像.jpg"
    path.write_bytes(cover_bytes)
    return path


def test_saved_capture_pipeline_is_self_contained_and_machine_readable(
    local_pipeline, captured, local_cover, tmp_path
):
    source = captured[0]
    (source / "unrelated.txt").write_text("Do not copy unrelated input files")
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    cover_before = local_cover.read_bytes()
    output = tmp_path / "音楽 出力" / "album"
    result = workflow.import_album(
        output, local_cover, saved_capture=source, apple_album_id=123, edition="初回限定盤"
    )
    assert result == json.loads((output / "report.json").read_bytes())
    assert result["schema_version"] == 1
    assert result["operation"] == "import"
    assert result["status"] == "complete"
    assert result["archival_verified"] is False
    assert result["source"] == {"kind": "capture", "path": str(source)}
    assert result["output_directory"] == str(output)
    assert result["result"]["track_count"] == 2
    assert result["result"]["loudness_normalized"] is True
    assert result["result"]["all_targets_reached"] is False  # Very short test audio.
    assert result["result"]["directory"] == "tagged"
    assert [s["name"] for s in result["stages"]] == [s[0] for s in workflow.STAGES]
    assert all(s["status"] == "complete" for s in result["stages"])
    assert not (output / "capture" / "unrelated.txt").exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert local_cover.read_bytes() == cover_before
    assert (output / "input-cover.jpg").read_bytes() == cover_before
    artifacts = result["result"]
    for item in [
        artifacts["album"],
        artifacts["artwork"],
        *artifacts["tracks"],
        *artifacts["archive"],
    ]:
        assert not Path(item["file"]).is_absolute()
        path = output / item["file"]
        assert path.is_file()
        assert item["sha256"] == file_sha256(path)
        assert item["bytes"] == path.stat().st_size
    for stage in result["stages"]:
        assert stage["manifest_sha256"] == file_sha256(output / stage["report"])
    assert (output / artifacts["artwork"]["file"]).read_bytes() == cover_before
    # Moving the album does not invalidate root-relative final artifact references.
    moved = output.rename(tmp_path / "moved album")
    assert all((moved / t["file"]).exists() for t in artifacts["tracks"])


def test_device_pipeline_reads_twice_and_freezes_cover_before_read(
    local_pipeline, captured, local_cover, cover_bytes, tmp_path, monkeypatch
):
    calls = []
    toc = (captured[0] / "toc.bin").read_bytes()
    pcm = captured[1]

    class Reader:
        def __init__(self, device):
            calls.append(device)

        def __enter__(self):
            local_cover.write_bytes(b"changed by caller while reading")
            return self

        def __exit__(self, *_):
            pass

        def read_toc(self):
            return toc

        def read_audio(self, lba, count):
            calls.append((lba, count))
            return pcm[lba * 2352 : (lba + count) * 2352]

    monkeypatch.setattr(workflow, "DarwinCD", Reader)
    output = tmp_path / "album"
    result = workflow.import_album(output, local_cover, device="/dev/rdisk99", apple_album_id=123)
    assert result["status"] == "complete"
    assert calls == ["/dev/rdisk99", (0, 11), (0, 11)]
    assert (output / "capture" / "audio.bin").read_bytes() == pcm
    assert (output / result["result"]["artwork"]["file"]).read_bytes() == cover_bytes
    assert (output / "capture" / "disc.cue").exists()


@pytest.mark.parametrize(
    "change",
    [
        "no_source",
        "both_sources",
        "bad_device",
        "both_providers",
        "bad_apple_id",
        "bad_release_id",
        "bad_edition",
        "bad_passes",
        "bad_retries",
        "bad_chunk",
        "bad_lufs",
        "bad_peak",
        "bad_headroom",
        "bad_cover",
        "missing_cover",
        "bad_image",
        "nested_output",
        "existing_output",
    ],
)
def test_bad_arguments_cannot_read_cd_or_overwrite_input(
    captured, local_cover, tmp_path, monkeypatch, change
):
    def no_cd(*_):
        raise AssertionError("Preflight must run before CD access")

    monkeypatch.setattr(workflow, "DarwinCD", no_cd)
    options = {"saved_capture": captured[0], "apple_album_id": 123}
    output = tmp_path / "album"
    if change == "no_source":
        options.pop("saved_capture")
    elif change == "both_sources":
        options["device"] = "/dev/rdisk99"
    elif change == "bad_device":
        options.pop("saved_capture")
        options["device"] = "/dev/disk0"
    elif change == "both_providers":
        options["release_id"] = "11111111-1111-4111-8111-111111111111"
    elif change == "bad_apple_id":
        options["apple_album_id"] = 0
    elif change == "bad_release_id":
        options.pop("apple_album_id")
        options["release_id"] = "not-a-uuid"
    elif change == "bad_edition":
        options["edition"] = "bad\nlabel"
    elif change == "bad_passes":
        options["passes"] = 1
    elif change == "bad_retries":
        options["retries"] = -1
    elif change == "bad_chunk":
        options["chunk_sectors"] = 76
    elif change == "bad_lufs":
        options["target_lufs"] = float("nan")
    elif change == "bad_peak":
        options["true_peak_dbtp"] = 1
    elif change == "bad_headroom":
        options["peak_headroom_db"] = 7
    elif change == "bad_cover":
        local_cover.write_bytes(b"not an image")
    elif change == "missing_cover":
        local_cover.unlink()
    elif change == "bad_image":
        (captured[0] / "audio.bin").write_bytes(b"corrupt")
    elif change == "nested_output":
        output = captured[0] / "nested"
    else:
        output.mkdir()
        (output / "keep.txt").write_text("untouched")
    with pytest.raises((ValueError, FileExistsError)):
        workflow.import_album(output, local_cover, **options)
    if change == "existing_output":
        assert (output / "keep.txt").read_text() == "untouched"
        assert list(output.iterdir()) == [output / "keep.txt"]
    else:
        assert not output.exists()


@pytest.mark.parametrize(
    "failure", ["metadata", "encode", "interrupt", "false_success", "changed_report"]
)
def test_pipeline_stops_and_reports_partial_results(
    local_pipeline, captured, local_cover, tmp_path, monkeypatch, failure
):
    def fail(*args, **kwargs):
        if failure == "interrupt":
            raise KeyboardInterrupt
        raise RuntimeError("injected failure")

    if failure == "metadata":
        monkeypatch.setattr(workflow, "fetch_metadata", fail)
        failed_stage = "metadata"
    elif failure in ("encode", "interrupt"):
        monkeypatch.setattr(workflow, "encode_aac", fail)
        failed_stage = "encode"
    elif failure == "false_success":
        original = workflow.encode_aac

        def incomplete(*args, **kwargs):
            result = original(*args, **kwargs)
            result["status"] = "running"
            return result

        monkeypatch.setattr(workflow, "encode_aac", incomplete)
        failed_stage = "encode"
    else:
        original = workflow.encode_aac

        def changed(directory, output, **kwargs):
            result = original(directory, output, **kwargs)
            path = directory.parent / "metadata" / "report.json"
            path.write_bytes(path.read_bytes() + b"\n")
            return result

        monkeypatch.setattr(workflow, "encode_aac", changed)
        failed_stage = None
    output = tmp_path / "album"
    with pytest.raises(workflow.WorkflowFailed) as error:
        workflow.import_album(output, local_cover, saved_capture=captured[0], apple_album_id=123)
    result = json.loads((output / "report.json").read_bytes())
    assert result == error.value.report
    assert result["status"] == "failed"
    assert result["result"] is None
    assert error.value.exit_code == (130 if failure == "interrupt" else 1)
    assert result["stages"][0]["status"] == "complete"
    assert (output / "capture" / "audio.bin").read_bytes() == captured[1]
    if failed_stage:
        failed_index = next(i for i, s in enumerate(result["stages"]) if s["name"] == failed_stage)
        assert result["stages"][failed_index]["status"] == "failed"
        assert all(s["status"] == "pending" for s in result["stages"][failed_index + 1 :])
        assert not (output / "tagged").exists()


def test_usage_environment_main_returns_only_json_on_stdout(
    local_pipeline, captured, local_cover, tmp_path, capsys
):
    output = tmp_path / "spaces 日本語"
    code = workflow.main(
        {
            "usage_capture": str(captured[0]),
            "usage_output": str(output),
            "usage_artwork": str(local_cover),
            "usage_apple_album_id": "123",
            "usage_edition": "初回限定盤",
            "usage_target_lufs": "-15",
        }
    )
    assert code == 0
    streams = capsys.readouterr()
    result = json.loads(streams.out)
    assert result == json.loads((output / "report.json").read_bytes())
    assert result["parameters"]["target_lufs"] == -15
    assert "split:" in streams.err
    assert "tag:" in streams.err


@pytest.mark.parametrize(
    "environment",
    [{}, {"usage_output": "out", "usage_artwork": "cover.jpg", "usage_passes": "bad"}],
)
def test_main_preflight_error_is_json(environment, capsys):
    assert workflow.main(environment) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["result"] is None


def test_mise_usage_help_required_flags_and_argument_forwarding(tmp_path):
    mise = shutil.which("mise")
    if mise is None:
        pytest.skip("mise is not installed")
    root = Path(__file__).resolve().parents[1]
    global_config = tmp_path / "global-mise.toml"
    global_config.write_text("")
    env = os.environ | {
        "MISE_TRUSTED_CONFIG_PATHS": str(root),
        "MISE_GLOBAL_CONFIG_FILE": str(global_config),
        "NO_COLOR": "1",
    }
    command = [mise, "run", "import"]
    help_result = subprocess.run(
        command + ["--help"], cwd=root, env=env, capture_output=True, text=True, timeout=30
    )
    assert help_result.returncode == 0
    assert "--output" in help_result.stdout
    assert "--artwork" in help_result.stdout
    assert "--capture" in help_result.stdout
    missing = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert missing.returncode != 0
    # A fake uv records parsed Usage values without invoking CD or network operations.
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_uv = binary / "uv"
    fake_uv.write_text(
        "#!/bin/bash\n"
        'if [[ -n "${FAKE_EXIT_CODE:-}" ]]; then exit "$FAKE_EXIT_CODE"; fi\n'
        'printf \'%s\\n\' "$usage_output" "$usage_capture" "$usage_artwork" '
        '"$usage_target_lufs" "$usage_apple_album_id"\n'
    )
    fake_uv.chmod(0o755)
    path_env = env | {"PATH": str(binary) + os.pathsep + env["PATH"]}
    # mise can prepend its tool paths, so invoke a copied task in a tools-free project.
    project = tmp_path / "project"
    (project / "mise-tasks").mkdir(parents=True)
    (project / "mise.toml").write_text("")
    shutil.copyfile(root / "mise-tasks" / "import", project / "mise-tasks" / "import")
    (project / "mise-tasks" / "import").chmod(0o755)
    path_env["MISE_TRUSTED_CONFIG_PATHS"] = str(project)
    values = [str(tmp_path / "出力 $(not-a-command)"), "saved rip", "画像 file.jpg", "-15", "123"]
    parsed = subprocess.run(
        command
        + [
            "--output",
            values[0],
            "--capture",
            values[1],
            "--artwork",
            values[2],
            "--target-lufs=-15",
            "--apple-album-id",
            "123",
        ],
        cwd=project,
        env=path_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stderr
    assert parsed.stdout.splitlines() == values
    for flags in (
        ["--capture", "saved", "--artwork", "cover.jpg"],
        ["--capture", "saved", "--output", "out"],
        ["--output", "out", "--artwork", "cover.jpg", "--unknown-flag"],
    ):
        rejected = subprocess.run(
            command + flags, cwd=project, env=path_env, capture_output=True, text=True, timeout=30
        )
        assert rejected.returncode != 0
        assert rejected.stdout == ""  # Usage rejects input before invoking uv.
    for code in (1, 130):
        failed = subprocess.run(
            command + ["--output", "out", "--capture", "saved", "--artwork", "cover.jpg"],
            cwd=project,
            env=path_env | {"FAKE_EXIT_CODE": str(code)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert failed.returncode == code


@pytest.mark.parametrize("exception,code", [(RuntimeError, 1), (KeyboardInterrupt, 130)])
def test_main_runtime_failure_and_interruption_are_saved_json(
    local_pipeline, captured, local_cover, tmp_path, monkeypatch, capsys, exception, code
):
    def fail(*args, **kwargs):
        raise exception("injected")

    monkeypatch.setattr(workflow, "encode_aac", fail)
    output = tmp_path / "album"
    assert (
        workflow.main(
            {
                "usage_capture": str(captured[0]),
                "usage_output": str(output),
                "usage_artwork": str(local_cover),
                "usage_apple_album_id": "123",
            }
        )
        == code
    )
    result = json.loads(capsys.readouterr().out)
    assert result == json.loads((output / "report.json").read_bytes())
    assert result["status"] == "failed"
    assert result["result"] is None
    assert result["stages"][4]["status"] == "failed"


def test_changed_final_audio_cannot_be_reported_as_complete(
    local_pipeline, captured, local_cover, tmp_path, monkeypatch
):
    original = workflow.tag_album

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        output = args[2]
        (output / result["tracks"][0]["file"]).write_bytes(b"corrupt")
        return result

    monkeypatch.setattr(workflow, "tag_album", changed)
    output = tmp_path / "album"
    with pytest.raises(workflow.WorkflowFailed, match="Final AAC changed"):
        workflow.import_album(output, local_cover, saved_capture=captured[0], apple_album_id=123)
    result = json.loads((output / "report.json").read_bytes())
    assert result["status"] == "failed" and result["result"] is None


def test_symlink_archive_files_are_not_followed(
    local_pipeline, fake_metadata, captured, local_cover, tmp_path
):
    (captured[0] / "reads.jsonl").symlink_to(captured[0] / "toc.bin")
    with pytest.raises(workflow.WorkflowFailed, match="must not be symlinks"):
        workflow.import_album(
            tmp_path / "album", local_cover, saved_capture=captured[0], apple_album_id=123
        )
    assert fake_metadata.calls == []


def test_capture_changes_during_copy_are_detected(
    local_pipeline, captured, local_cover, tmp_path, monkeypatch
):
    original = shutil.copyfileobj

    def changed(source, destination):
        original(source, destination)
        if Path(source.name).name == "audio.bin":
            Path(source.name).write_bytes(b"changed during copy")

    monkeypatch.setattr(shutil, "copyfileobj", changed)
    with pytest.raises(workflow.WorkflowFailed, match="Archive changed during copy"):
        workflow.import_album(
            tmp_path / "album", local_cover, saved_capture=captured[0], apple_album_id=123
        )


@pytest.mark.parametrize("damage", ["metadata", "snapshot"])
def test_input_snapshots_cannot_change_during_processing(
    local_pipeline, captured, local_cover, tmp_path, monkeypatch, damage
):
    original = workflow.encode_aac

    def changed(directory, output, **kwargs):
        result = original(directory, output, **kwargs)
        root = directory.parent
        if damage == "metadata":
            path = root / "metadata" / "album.json"
            album = json.loads(path.read_bytes())
            album["title"] = "changed after lookup"
            write_json(path, album)
        else:
            (root / "input-cover.jpg").write_bytes(b"changed snapshot")
        return result

    monkeypatch.setattr(workflow, "encode_aac", changed)
    output = tmp_path / "album"
    with pytest.raises(workflow.WorkflowFailed, match="changed"):
        workflow.import_album(output, local_cover, saved_capture=captured[0], apple_album_id=123)
    result = json.loads((output / "report.json").read_bytes())
    assert result["status"] == "failed" and result["result"] is None
    if damage == "metadata":
        assert not (output / "tagged").exists()


@pytest.mark.parametrize("kind", ["symlink", "traversal"])
def test_result_artifacts_cannot_escape_output(tmp_path, kind):
    root = tmp_path / "album"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("not an output")
    if kind == "symlink":
        path = root / "link"
        path.symlink_to(outside)
    else:
        path = root / ".." / "outside"
    with pytest.raises(ValueError, match="inside output"):
        workflow._artifact(root, path)

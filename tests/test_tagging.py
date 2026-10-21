import hashlib
import json

import pytest

from diskflow.tagging import audio_signature, tag_album, track_filename


def test_tagging_writes_titles_cover_and_preserves_audio(encoded_wavs, album_metadata, tmp_path):
    from mutagen.mp4 import MP4

    before = {p.name: p.read_bytes() for p in encoded_wavs.iterdir()}
    signatures = {
        number: audio_signature(encoded_wavs / f"track-{number:02}.m4a") for number in (1, 2)
    }
    output = tmp_path / "tagged"
    result = tag_album(encoded_wavs, album_metadata, output)
    assert result["status"] == "tagged"
    assert result["metadata_tagged"] is True
    assert result["loudness_normalized"] is False
    assert result["archival_verified"] is False
    assert result["metadata_source"]["edition"] == "digital"
    cover = (album_metadata / "cover.jpg").read_bytes()
    for track in result["tracks"]:
        path = output / track["file"]
        tagged = MP4(path)
        assert tagged["\xa9nam"] == [track["title"]]
        assert tagged["\xa9ART"] == tagged["aART"] == ["aiko"]
        assert tagged["\xa9alb"] == ["テストアルバム"]
        assert tagged["\xa9day"] == ["2026-10-21"]
        assert tagged["trkn"] == [(track["number"], 2)]
        assert tagged["disk"] == [(1, 1)]
        assert tagged["\xa9gen"] == ["J-Pop"]
        assert tagged["cprt"] == ["℗ Test"]
        assert tagged["----:com.apple.iTunes:keep_me"] == [b"preserve unrelated tags"]
        assert bytes(tagged["covr"][0]) == cover
        assert audio_signature(path) == signatures[track["number"]]
        assert track["audio_unchanged"] is True
        assert track["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert {p.name: p.read_bytes() for p in encoded_wavs.iterdir()} == before
    assert (output / "01 君の言う通り.m4a").exists()
    assert (output / "02 淡い.m4a").exists()
    assert (output / "album.json").exists()
    assert (output / "cover.jpg").read_bytes() == cover
    assert not list(output.glob("*.part"))


def test_png_cover_is_embedded_with_correct_mp4_type(
    captured, encoded_wavs, fake_metadata, tmp_path
):
    import io

    from mutagen.mp4 import MP4, MP4Cover
    from PIL import Image

    from diskflow.metadata import fetch_metadata

    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (40, 80, 120, 255)).save(buffer, "PNG")
    fake_metadata.cover = buffer.getvalue()
    metadata, output = tmp_path / "metadata", tmp_path / "tagged"
    fetch_metadata(captured[0], metadata, apple_album_id=123)
    result = tag_album(encoded_wavs, metadata, output)
    cover = MP4(output / result["tracks"][0]["file"])["covr"][0]
    assert cover.imageformat == MP4Cover.FORMAT_PNG
    assert bytes(cover) == fake_metadata.cover
    assert (output / "cover.png").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "toc",
        "frames",
        "track_number",
        "path",
        "cover",
        "date",
        "edition",
        "implicit_edition",
        "selection",
    ],
)
def test_metadata_mismatch_never_promotes_audio(encoded_wavs, album_metadata, tmp_path, damage):
    path = album_metadata / "album.json"
    data = json.loads(path.read_text())
    if damage == "toc":
        data["source_toc_sha256"] = "0" * 64
    elif damage == "frames":
        data["tracks"][0]["frames"] += 1
    elif damage == "track_number":
        data["tracks"][0]["number"] = 2
    elif damage == "path":
        data["artwork"]["file"] = "../cover.jpg"
    elif damage == "cover":
        data["artwork"]["sha256"] = "0" * 64
    elif damage == "date":
        data["date"] = "invalid"
    elif damage == "edition":
        data["edition"] = {"label": "初回限定盤", "basis": "TOC-inferred"}
    elif damage == "implicit_edition":
        data["edition"] = {"label": "初回限定盤", "basis": "user-specified"}
    else:
        data["artwork"]["selection"] = "unknown"
    path.write_text(json.dumps(data))
    output = tmp_path / "tagged"
    with pytest.raises(ValueError):
        tag_album(encoded_wavs, album_metadata, output)
    assert json.loads((output / "report.json").read_text())["status"] == "failed"
    assert not list(output.glob("*.m4a"))


def test_changed_audio_or_provenance_is_refused(encoded_wavs, album_metadata, tmp_path):
    (encoded_wavs / "track-01.m4a").write_bytes(b"corrupt")
    output = tmp_path / "tagged"
    with pytest.raises(ValueError, match="Source AAC changed"):
        tag_album(encoded_wavs, album_metadata, output)
    assert not list(output.glob("*.m4a"))


def test_changed_parent_report_is_refused(encoded_wavs, album_metadata, wavs, tmp_path):
    path = wavs / "report.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="provenance report changed"):
        tag_album(encoded_wavs, album_metadata, tmp_path / "tagged")


def test_accidental_audio_changes_are_detected(encoded_wavs, album_metadata, tmp_path, monkeypatch):
    import diskflow.tagging as tagging

    original = audio_signature

    def changed_signature(path):
        result = original(path)
        if path.name.endswith(".part"):
            result["packet_sha256"] = "0" * 64
        return result

    monkeypatch.setattr(tagging, "audio_signature", changed_signature)
    output = tmp_path / "tagged"
    with pytest.raises(ValueError, match="AAC packets, timing or codec changed"):
        tag_album(encoded_wavs, album_metadata, output)
    assert not list(output.glob("*.m4a"))
    assert list(output.glob("*.part"))


def test_interrupted_or_existing_output_is_safe(encoded_wavs, album_metadata, tmp_path):
    def interrupt(*_):
        raise KeyboardInterrupt

    output = tmp_path / "tagged"
    with pytest.raises(KeyboardInterrupt):
        tag_album(encoded_wavs, album_metadata, output, progress=interrupt)
    assert json.loads((output / "report.json").read_text())["status"] == "failed"
    assert not list(output.glob("*.m4a"))
    with pytest.raises(FileExistsError):
        tag_album(encoded_wavs, album_metadata, output)


@pytest.mark.parametrize("title", ["../../escape", "a/b:c\\d?*", "長" * 300, "...", "e\u0301"])
def test_filenames_are_unicode_safe_and_cannot_escape(title):
    name = track_filename(1, title)
    assert name.startswith("01 ") and name.endswith(".m4a")
    assert not any(char in name for char in '/\\<>:"|?*')
    assert len(name.encode("utf-8")) < 256
    assert "/" not in name


def test_cli_metadata_and_tagging(captured, fake_metadata, encoded_wavs, tmp_path, capsys):
    from diskflow.cli import main

    meta, output = tmp_path / "metadata", tmp_path / "tagged"
    assert (
        main(["metadata", str(captured[0]), "--apple-album-id", "123", "--output", str(meta)]) == 0
    )
    assert main(["tag", str(encoded_wavs), "--metadata", str(meta), "--output", str(output)]) == 0
    assert "AAC packets, codec configuration and timing are unchanged" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["file", "url"])
@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_cli_explicit_cover_and_edition_are_embedded_without_reencoding(
    captured, fake_metadata, encoded_wavs, tmp_path, capsys, kind, image_format
):
    import io

    from mutagen.mp4 import MP4, MP4Cover
    from PIL import Image

    from diskflow.cli import main

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (120, 40, 80)).save(buffer, image_format)
    fake_metadata.cover = buffer.getvalue()
    suffix = "jpg" if image_format == "JPEG" else "png"
    local = tmp_path / "private-folder" / f"limited.{suffix}"
    local.parent.mkdir()
    local.write_bytes(fake_metadata.cover)
    url = f"https://artist.example/limited.{suffix}"
    option = ["--artwork", str(local)] if kind == "file" else ["--artwork-url", url]
    before = {p.name: p.read_bytes() for p in encoded_wavs.iterdir()}
    meta, output = tmp_path / "custom-metadata", tmp_path / "tagged"
    assert (
        main(
            [
                "metadata",
                str(captured[0]),
                "--apple-album-id",
                "123",
                "--output",
                str(meta),
                "--edition",
                "初回限定盤",
                *option,
            ]
        )
        == 0
    )
    assert main(["tag", str(encoded_wavs), "--metadata", str(meta), "--output", str(output)]) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["edition"]["label"] == "初回限定盤"
    assert report["artwork"]["selection"] == f"explicit_{kind}"
    for entry in report["tracks"]:
        path = output / entry["file"]
        tags = MP4(path)
        assert tags["----:com.apple.iTunes:diskflow_edition"] == ["初回限定盤".encode()]
        source = json.loads(tags["----:com.apple.iTunes:diskflow_metadata_source"][0])
        assert source["edition"] == "digital"
        assert source["provider"] == "apple"
        art_source = tags["----:com.apple.iTunes:diskflow_artwork_source"][0]
        assert b"private-folder" not in art_source
        art = json.loads(art_source)
        assert art["selection"] == f"explicit_{kind}"
        assert art["source_url"] == (None if kind == "file" else url)
        assert art["sha256"] == hashlib.sha256(fake_metadata.cover).hexdigest()
        assert bytes(tags["covr"][0]) == fake_metadata.cover
        assert tags["covr"][0].imageformat == (
            MP4Cover.FORMAT_JPEG if image_format == "JPEG" else MP4Cover.FORMAT_PNG
        )
        assert entry["audio_unchanged"] is True
        assert audio_signature(path) == audio_signature(encoded_wavs / entry["source_file"])
    assert {p.name: p.read_bytes() for p in encoded_wavs.iterdir()} == before
    assert local.read_bytes() == fake_metadata.cover
    assert "Edition (user-specified): 初回限定盤" in capsys.readouterr().out


def test_legacy_metadata_without_artwork_selection_still_tags(
    encoded_wavs, album_metadata, tmp_path
):
    path = album_metadata / "album.json"
    album = json.loads(path.read_text())
    album["artwork"].pop("selection")
    path.write_text(json.dumps(album))
    assert tag_album(encoded_wavs, album_metadata, tmp_path / "tagged")["status"] == "tagged"

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from diskflow.metadata import MetadataClient, cover_info, disc_id, fetch_metadata
from diskflow.toc import TOC, Session, Track


def test_musicbrainz_disc_id_real_toc_vector():
    starts = [
        0,
        23839,
        44269,
        66952,
        86881,
        109232,
        127297,
        143833,
        166146,
        185845,
        205808,
        225958,
        248735,
    ]
    toc = TOC(
        1,
        1,
        (Session(1, 1, 13, 265649),),
        tuple(Track(n, 1, 0, start) for n, start in enumerate(starts, 1)),
    )
    assert disc_id(toc) == "Y9s8NdRjjb.sQovlAPRfnec7gS0-"


def test_apple_metadata_checks_tracks_and_keeps_digital_art_source(
    captured, fake_metadata, tmp_path
):
    output = tmp_path / "metadata"
    result = fetch_metadata(captured[0], output, apple_album_id=123)
    album = json.loads((output / "album.json").read_text())
    assert result["status"] == "metadata_ready"
    assert album["artist"] == "aiko"
    assert album["tracks"][0]["title"] == "君の言う通り"
    assert [t["frames"] for t in album["tracks"]] == [2940, 3528]
    assert album["maximum_duration_difference_ms"] < 0.5
    assert album["source"]["edition"] == "digital"
    assert album["artwork"]["width"] == album["artwork"]["height"] == 32
    assert (output / "cover.jpg").read_bytes() == fake_metadata.cover
    assert any("1200x1200bb.jpg" in url for url in fake_metadata.calls)
    assert not (output / "cover.download").exists()


@pytest.mark.parametrize(
    "change", ["duration", "missing", "duplicate", "different_album", "multi_disc"]
)
def test_apple_mismatches_cannot_produce_ready_metadata(captured, fake_metadata, tmp_path, change):
    rows = fake_metadata.apple["results"]
    if change == "duration":
        rows[1]["trackTimeMillis"] = 9999
    elif change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows[2]["trackNumber"] = 1
    elif change == "different_album":
        rows[1]["collectionId"] = 999
    else:
        rows[1]["discCount"] = 2
    output = tmp_path / "metadata"
    with pytest.raises(ValueError):
        fetch_metadata(captured[0], output, apple_album_id=123)
    assert json.loads((output / "report.json").read_text())["status"] == "failed"
    assert not (output / "album.json").exists()


def test_unique_musicbrainz_release_is_toc_bound(captured, fake_metadata, tmp_path):
    output = tmp_path / "metadata"
    result = fetch_metadata(captured[0], output)
    album = json.loads((output / "album.json").read_text())
    assert result["provider"] == "musicbrainz"
    assert album["source"]["id"] == fake_metadata.mb_release["id"]
    assert (output / "candidates.json").exists()
    assert any("coverartarchive.org" in url for url in fake_metadata.calls)


def test_musicbrainz_recording_artist_credit_is_not_replaced_with_album_artist(
    captured, fake_metadata, tmp_path
):
    fake_metadata.mb_release["artist-credit"] = [{"name": "Various Artists"}]
    fake_metadata.mb_release["media"][0]["tracks"][0]["recording"] = {
        "artist-credit": [{"name": "A", "joinphrase": " feat. "}, {"name": "B"}],
    }
    output = tmp_path / "metadata"
    fetch_metadata(captured[0], output)
    album = json.loads((output / "album.json").read_text())
    assert album["album_artist"] == "Various Artists"
    assert album["tracks"][0]["artist"] == "A feat. B"


def test_ambiguous_release_requires_selection(captured, fake_metadata, tmp_path):
    fake_metadata.mb_disc["releases"].append(
        {"id": "22222222-2222-4222-8222-222222222222", "title": "Other edition"}
    )
    output = tmp_path / "metadata"
    with pytest.raises(ValueError, match="ambiguous"):
        fetch_metadata(captured[0], output)
    assert (output / "candidates.json").exists()
    assert not (output / "album.json").exists()
    selected = tmp_path / "selected"
    assert (
        fetch_metadata(captured[0], selected, release_id=fake_metadata.mb_release["id"])["status"]
        == "metadata_ready"
    )


def test_musicbrainz_not_found_has_actionable_error(captured, tmp_path, monkeypatch):
    def not_found(*args, **kwargs):
        raise HTTPError("https://musicbrainz.org/test", 404, "not found", None, io.BytesIO(b"{}"))

    monkeypatch.setattr(MetadataClient, "get", not_found)
    output = tmp_path / "metadata"
    with pytest.raises(ValueError, match="explicit --apple-album-id"):
        fetch_metadata(captured[0], output)
    assert json.loads((output / "report.json").read_text())["status"] == "failed"


def test_cover_data_and_existing_output_are_safe(captured, fake_metadata, tmp_path):
    fake_metadata.cover = b"<html>not an image</html>"
    output = tmp_path / "metadata"
    with pytest.raises(ValueError, match="Invalid cover"):
        fetch_metadata(captured[0], output, apple_album_id=123)
    assert not (output / "album.json").exists()
    with pytest.raises(FileExistsError):
        fetch_metadata(captured[0], output, apple_album_id=123)


def test_non_image_and_oversized_cover_rejected(cover_bytes, monkeypatch):
    assert cover_info(cover_bytes)["format"] == "JPEG"
    monkeypatch.setattr("diskflow.metadata.MAX_COVER_BYTES", len(cover_bytes) - 1)
    with pytest.raises(ValueError, match="oversized"):
        cover_info(cover_bytes)


def test_network_client_records_errors_and_bounds_responses(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
        url = "https://example.org/cover"

    monkeypatch.setattr("diskflow.metadata.urlopen", lambda *args, **kwargs: Response(b"x" * 30))
    monkeypatch.setattr("diskflow.metadata.MAX_COVER_BYTES", 10)
    client = MetadataClient(tmp_path)
    with pytest.raises(ValueError, match="size limit"):
        client.get("https://example.org/cover", "cover", image=True)
    assert not (tmp_path / "cover").exists()
    event = json.loads((tmp_path / "requests.jsonl").read_text())
    assert "size limit" in event["error"]
    with pytest.raises(ValueError, match="HTTPS"):
        client.get("file:///private/file", "no")


def test_musicbrainz_requests_are_spaced_and_use_app_user_agent(tmp_path, monkeypatch):
    import diskflow.metadata as metadata

    class Response(io.BytesIO):
        status = 200
        url = "https://musicbrainz.org/test"

    requests, sleeps = [], []

    def open_request(request, **kwargs):
        requests.append(request)
        assert kwargs["timeout"] == 30
        return Response(b"{}")

    times = iter([100.0, 100.25, 100.25])
    monkeypatch.setattr(metadata, "urlopen", open_request)
    monkeypatch.setattr(metadata.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(metadata.time, "sleep", sleeps.append)
    client = MetadataClient(tmp_path)
    client.json("https://musicbrainz.org/one", "one.json")
    client.json("https://musicbrainz.org/two", "two.json")
    assert sleeps == [pytest.approx(0.85)]
    assert all("diskflow/0.1.0" in request.get_header("User-agent") for request in requests)


def test_http_error_body_and_status_are_retained(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError(
            "https://musicbrainz.org/test", 404, "not found", None, io.BytesIO(b"missing")
        )

    monkeypatch.setattr("diskflow.metadata.urlopen", fail)
    with pytest.raises(HTTPError):
        MetadataClient(tmp_path).get("https://musicbrainz.org/test", "lookup.json")
    assert (tmp_path / "lookup.json.error").read_bytes() == b"missing"
    assert json.loads((tmp_path / "requests.jsonl").read_text())["status"] == 404


@pytest.mark.parametrize("provider", ["apple", "musicbrainz"])
def test_explicit_artwork_and_edition_do_not_replace_track_metadata_source(
    captured, fake_metadata, tmp_path, provider
):
    output = tmp_path / "metadata"
    url = "https://artist.example/limited.jpg"
    options = {"apple_album_id": 123} if provider == "apple" else {}
    result = fetch_metadata(captured[0], output, artwork_url=url, edition="初回限定盤", **options)
    album = json.loads((output / "album.json").read_text())
    assert album["source"]["provider"] == provider
    assert album["source"]["edition"] == ("digital" if provider == "apple" else "selected release")
    assert album["edition"]["label"] == "初回限定盤"
    assert album["edition"]["basis"] == "user-specified"
    assert result["edition"] == album["edition"]
    assert album["artwork"]["selection"] == "explicit_url"
    assert album["artwork"]["source_url"] == url
    assert album["artwork"]["provider_source_url"] != url
    assert (output / "cover.jpg").read_bytes() == fake_metadata.cover
    assert url in fake_metadata.calls
    assert not any(
        "mzstatic.com" in call or "coverartarchive.org" in call for call in fake_metadata.calls
    )


@pytest.mark.parametrize(
    "options",
    [
        {"edition": "初回限定盤"},
        {"edition": "", "artwork_url": "https://artist.example/cover.jpg"},
        {"edition": "初回\n限定", "artwork_url": "https://artist.example/cover.jpg"},
        {"artwork": Path("cover.jpg"), "artwork_url": "https://artist.example/cover.jpg"},
        {"artwork_url": "http://artist.example/cover.jpg"},
        {"artwork_url": "file:///private/cover.jpg"},
        {"artwork_url": "https:///cover.jpg"},
        {"artwork_url": "https://user:secret@artist.example/cover.jpg"},
        {"artwork_url": "https://artist.example/cover image.jpg"},
    ],
)
def test_bad_artwork_options_fail_before_network_or_output(
    captured, fake_metadata, tmp_path, options
):
    output = tmp_path / "metadata"
    with pytest.raises(ValueError):
        fetch_metadata(captured[0], output, apple_album_id=123, **options)
    assert not output.exists()
    assert fake_metadata.calls == []


@pytest.mark.parametrize("damage", ["missing", "directory", "html", "oversized"])
def test_invalid_local_artwork_never_falls_back_to_provider(
    captured, fake_metadata, tmp_path, monkeypatch, damage
):
    local = tmp_path / "input.jpg"
    if damage == "directory":
        local.mkdir()
    elif damage == "html":
        local.write_bytes(b"<html>not an image</html>")
    elif damage == "oversized":
        local.write_bytes(fake_metadata.cover)
        monkeypatch.setattr("diskflow.metadata.MAX_COVER_BYTES", len(fake_metadata.cover) - 1)
    output = tmp_path / "metadata"
    with pytest.raises(ValueError):
        fetch_metadata(captured[0], output, apple_album_id=123, artwork=local, edition="初回限定盤")
    assert json.loads((output / "report.json").read_text())["status"] == "failed"
    assert not (output / "album.json").exists()
    assert not any("mzstatic.com" in call for call in fake_metadata.calls)


def test_missing_provider_artwork_can_be_supplied_explicitly(captured, fake_metadata, tmp_path):
    fake_metadata.apple["results"][0].pop("artworkUrl100")
    with pytest.raises(ValueError, match="specify --artwork or --artwork-url"):
        fetch_metadata(captured[0], tmp_path / "missing-cover", apple_album_id=123)
    output = tmp_path / "custom"
    fetch_metadata(
        captured[0], output, apple_album_id=123, artwork_url="https://artist.example/cover.jpg"
    )
    art = json.loads((output / "album.json").read_text())["artwork"]
    assert art["selection"] == "explicit_url"
    assert art["provider_source_url"] is None


def test_failed_explicit_artwork_download_never_falls_back(
    captured, fake_metadata, tmp_path, monkeypatch
):
    original = MetadataClient.get
    url = "https://artist.example/limited.jpg"

    def get(self, requested, name, *, image=False):
        if requested == url:
            fake_metadata.calls.append(requested)
            raise HTTPError(url, 404, "not found", None, io.BytesIO(b"missing"))
        return original(self, requested, name, image=image)

    monkeypatch.setattr(MetadataClient, "get", get)
    output = tmp_path / "metadata"
    with pytest.raises(HTTPError):
        fetch_metadata(
            captured[0], output, apple_album_id=123, artwork_url=url, edition="初回限定盤"
        )
    assert not (output / "album.json").exists()
    assert json.loads((output / "report.json").read_text())["status"] == "failed"
    assert not any("mzstatic.com" in call for call in fake_metadata.calls)

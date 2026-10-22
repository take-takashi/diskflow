import io
import json
from urllib.error import HTTPError

import pytest

from diskflow.metadata import MetadataClient, fetch_metadata


@pytest.fixture
def unregistered(fake_metadata, monkeypatch):
    original = MetadataClient.get

    def get(self, url, name, *, image=False):
        if "/discid/" in url:
            raise HTTPError(url, 404, "not found", None, io.BytesIO(b"{}"))
        return original(self, url, name, image=image)

    monkeypatch.setattr(MetadataClient, "get", get)
    return fake_metadata


def test_apple_id_is_found_from_album_hint_and_full_toc(captured, unregistered, tmp_path):
    output = tmp_path / "metadata"
    result = fetch_metadata(
        captured[0],
        output,
        automatic=True,
        hints={
            "album": "テストアルバム",
            "track_titles": {1: "君の言う通り", 2: "淡い"},
        },
    )
    assert result["status"] == "metadata_ready"
    album = json.loads((output / "album.json").read_bytes())
    assert album["source"]["id"] == "123"
    assert album["source"]["selection"] == "automatic_toc_match"
    assert album["source"]["edition"] == "digital"
    assert (output / "apple-search.json").exists()
    assert (output / "apple-123.json").exists()
    assert any("/search?" in url for url in unregistered.calls)


@pytest.mark.parametrize("damage", ["duration", "title"])
def test_auto_search_does_not_accept_different_tracks(captured, unregistered, tmp_path, damage):
    if damage == "duration":
        unregistered.apple["results"][1]["trackTimeMillis"] = 9999
    else:
        unregistered.apple["results"][1]["trackName"] = "別の曲"
    output = tmp_path / "metadata"
    with pytest.raises(ValueError, match="照合できません"):
        fetch_metadata(
            captured[0],
            output,
            automatic=True,
            hints={
                "album": "テストアルバム",
                "track_titles": {1: "君の言う通り", 2: "淡い"},
            },
        )
    assert not (output / "album.json").exists()


def test_unknown_cd_can_ask_for_album_name_not_an_id(captured, unregistered, tmp_path):
    result = fetch_metadata(
        captured[0],
        tmp_path / "metadata",
        automatic=True,
        query_provider=lambda: "aiko テストアルバム",
    )
    assert result["status"] == "metadata_ready"


def test_no_hints_or_terminal_stops_without_guessing(captured, unregistered, tmp_path):
    with pytest.raises(ValueError, match="アルバム名"):
        fetch_metadata(captured[0], tmp_path / "metadata", automatic=True)
    assert not any("/search?" in url for url in unregistered.calls)


def test_musicbrainz_ambiguity_can_be_resolved_by_number(captured, fake_metadata, tmp_path):
    fake_metadata.mb_disc["releases"].append(
        {"id": "22222222-2222-4222-8222-222222222222", "title": "別の版"}
    )
    result = fetch_metadata(
        captured[0], tmp_path / "metadata", automatic=True, choose=lambda *args: 0
    )
    assert result["provider"] == "musicbrainz"


def test_musicbrainz_outage_does_not_silently_pick_apple(captured, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError("https://musicbrainz.org", 503, "unavailable", None, io.BytesIO(b"{}"))

    monkeypatch.setattr(MetadataClient, "get", fail)
    with pytest.raises(HTTPError):
        fetch_metadata(captured[0], tmp_path / "metadata", automatic=True, hints={"album": "test"})


def test_ambiguous_apple_matches_never_pick_first_automatically(
    captured, unregistered, tmp_path, monkeypatch
):
    import copy

    original = MetadataClient.get

    def get(self, url, name, *, image=False):
        if "/search?" in url:
            data = {
                "results": [
                    {"collectionId": 123, "trackCount": 2},
                    {"collectionId": 456, "trackCount": 2},
                ]
            }
        elif "lookup?id=456" in url:
            data = copy.deepcopy(unregistered.apple)
            for row in data["results"]:
                row["collectionId"] = 456
            data["results"][0]["collectionName"] = "別の版"
        else:
            return original(self, url, name, image=image)
        raw = json.dumps(data).encode()
        (self.output / name).write_bytes(raw)
        return raw

    monkeypatch.setattr(MetadataClient, "get", get)
    with pytest.raises(ValueError, match="一意"):
        fetch_metadata(captured[0], tmp_path / "ambiguous", automatic=True, query="test")
    chosen = tmp_path / "chosen"
    fetch_metadata(captured[0], chosen, automatic=True, query="test", choose=lambda *args: 1)
    assert json.loads((chosen / "album.json").read_bytes())["source"]["id"] == "456"


def test_apple_requests_are_spaced(tmp_path, monkeypatch):
    import diskflow.metadata as metadata

    class Response(io.BytesIO):
        status = 200
        url = "https://itunes.apple.com/test"

    ticks = iter([100.0, 100.5, 100.5])
    sleeps = []
    monkeypatch.setattr(metadata, "urlopen", lambda *a, **kw: Response(b"{}"))
    monkeypatch.setattr(metadata.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(metadata.time, "sleep", sleeps.append)
    client = MetadataClient(tmp_path)
    client.json("https://itunes.apple.com/search", "search.json")
    client.json("https://itunes.apple.com/lookup", "lookup.json")
    assert sleeps == [pytest.approx(2.6)]

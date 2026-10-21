"""TOC-bound album metadata, with explicit selection and recorded network sources."""

import base64
import hashlib
import io
import json
import re
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from diskflow.archive import now, write_json
from diskflow.derived import operation_report
from diskflow.image import SavedImage
from diskflow.toc import TOC

USER_AGENT = "diskflow/0.1.0 (https://github.com/take-takashi/diskflow)"
DURATION_TOLERANCE_MS = 2000
MAX_COVER_BYTES = 8 * 1024 * 1024


def disc_id(toc: TOC) -> str:
    session = toc.audio_session()
    offsets = {track.number: track.start_lba + 150 for track in toc.tracks}
    value = f"{session.first_track:02X}{session.last_track:02X}{session.leadout_lba + 150:08X}"
    value += "".join(f"{offsets.get(number, 0):08X}" for number in range(1, 100))
    return (
        base64.b64encode(hashlib.sha1(value.encode("ascii")).digest())
        .decode()
        .translate(str.maketrans({"+": ".", "/": "_", "=": "-"}))
    )


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError(f"Missing or invalid metadata text: {field}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"Control characters in metadata: {field}")
    return unicodedata.normalize("NFC", value.strip())


def _objects(value, field: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Invalid metadata object list: {field}")
    return value


def validate_tracks(toc: TOC, tracks: list[dict]) -> float:
    if len(tracks) != len(toc.tracks):
        raise ValueError("Metadata track count disagrees with CD TOC")
    ends = [t.start_lba for t in toc.tracks[1:]] + [toc.audio_session().leadout_lba]
    maximum = 0.0
    for cd, end, track in zip(toc.tracks, ends, tracks, strict=True):
        frames = (end - cd.start_lba) * 588
        if track.get("number") != cd.number:
            raise ValueError("Metadata track numbers disagree with CD TOC")
        _text(track.get("title"), "track title")
        _text(track.get("artist"), "track artist")
        length = track.get("duration_ms")
        if type(length) is not int or length <= 0:
            raise ValueError("Missing track duration; cannot verify CD correspondence")
        difference = abs(length - frames * 1000 / 44100)
        if difference > DURATION_TOLERANCE_MS:
            raise ValueError(f"Track {cd.number} metadata duration disagrees with CD TOC")
        track["frames"] = frames
        track["duration_difference_ms"] = difference
        maximum = max(maximum, difference)
    return maximum


def cover_info(data: bytes) -> dict:
    from PIL import Image

    if not data or len(data) > MAX_COVER_BYTES:
        raise ValueError("Missing or oversized cover image")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG"):
                raise ValueError("Cover must be JPEG or PNG")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 16_000_000:
                raise ValueError("Cover dimensions exceed safety limit")
            kind = image.format
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid cover image") from exc
    return {
        "format": kind,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class MetadataClient:
    def __init__(self, output: Path):
        self.output = output
        self.last_musicbrainz = None

    def get(self, url: str, name: str, *, image: bool = False) -> bytes:
        if urlsplit(url).scheme != "https":
            raise ValueError("Metadata downloads require HTTPS")
        if urlsplit(url).hostname == "musicbrainz.org":
            if self.last_musicbrainz is not None:
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_musicbrainz)))
            self.last_musicbrainz = time.monotonic()
        limit = MAX_COVER_BYTES if image else 2 * 1024 * 1024
        record = {"time": now(), "url": url, "file": name}
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/*" if image else "application/json",
                },
            )
            with urlopen(request, timeout=30) as response:
                record.update(status=response.status, final_url=response.url)
                if urlsplit(response.url).scheme != "https":
                    raise ValueError("Metadata redirect left HTTPS")
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Metadata response exceeds size limit")
            (self.output / name).write_bytes(data)
            record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            return data
        except HTTPError as exc:
            record.update(status=exc.code, error=str(exc))
            (self.output / (name + ".error")).write_bytes(exc.read(65536))
            raise
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            with (self.output / "requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")

    def json(self, url: str, name: str) -> dict:
        result = json.loads(self.get(url, name))
        if not isinstance(result, dict):
            raise ValueError("Expected a metadata JSON object")
        return result


def _apple(client: MetadataClient, album_id: int) -> tuple[dict, str | None]:
    if type(album_id) is not int or album_id <= 0:
        raise ValueError("Apple album ID must be a positive integer")
    url = "https://itunes.apple.com/lookup?" + urlencode(
        {"id": album_id, "entity": "song", "country": "JP", "limit": 200}
    )
    response = client.json(url, "apple.json")
    rows = _objects(response.get("results", []), "Apple results")
    albums = [r for r in rows if r.get("wrapperType") == "collection"]
    if len(albums) != 1 or albums[0].get("collectionId") != album_id:
        raise ValueError("Apple lookup did not identify the requested album")
    album = albums[0]
    tracks = [r for r in rows if r.get("wrapperType") == "track"]
    if len(tracks) != album.get("trackCount") or any(
        t.get("collectionId") != album_id or t.get("discNumber") != 1 or t.get("discCount") != 1
        for t in tracks
    ):
        raise ValueError("Apple fallback supports one-disc albums with a complete track list")
    normalized = [
        {
            "number": t.get("trackNumber"),
            "title": _text(t.get("trackName"), "title"),
            "artist": _text(t.get("artistName"), "artist"),
            "duration_ms": t.get("trackTimeMillis"),
            "provider_track_id": str(t.get("trackId", "")),
        }
        for t in tracks
    ]
    if any(type(t["number"]) is not int for t in normalized):
        raise ValueError("Invalid Apple track numbers")
    normalized.sort(key=lambda t: t["number"])
    cover_url = None
    if album.get("artworkUrl100"):
        art = _text(album["artworkUrl100"], "artwork URL")
        parts = urlsplit(art)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".mzstatic.com"):
            raise ValueError("Expected an Apple artwork CDN URL")
        path = re.sub(r"/\d+x\d+bb\.(jpg|png)$", "/1200x1200bb.jpg", parts.path)
        cover_url = urlunsplit(parts._replace(path=path))
    return {
        "title": _text(album.get("collectionName"), "album"),
        "artist": _text(album.get("artistName"), "artist"),
        "album_artist": _text(album.get("artistName"), "album artist"),
        "date": _text(album.get("releaseDate"), "date").split("T")[0],
        "genre": album.get("primaryGenreName"),
        "copyright": album.get("copyright"),
        "disc_number": 1,
        "disc_total": 1,
        "tracks": normalized,
        "source": {
            "provider": "apple",
            "id": str(album_id),
            "url": url,
            "catalog_url": album.get("collectionViewUrl"),
            "edition": "digital",
            "note": "Explicit album ID; CD pressing/physical artwork edition not identified",
        },
    }, cover_url


def _credit(value) -> str:
    if not isinstance(value, list) or not value or any(not isinstance(v, dict) for v in value):
        raise ValueError("Missing MusicBrainz artist credit")
    pieces = []
    for credit in value:
        artist = credit.get("artist", {})
        if not isinstance(artist, dict):
            raise ValueError("Invalid MusicBrainz artist")
        join = credit.get("joinphrase", "")
        if not isinstance(join, str) or any(ord(char) < 32 for char in join):
            raise ValueError("Invalid MusicBrainz artist join phrase")
        pieces.append(_text(credit.get("name") or artist.get("name"), "artist") + join)
    return _text("".join(pieces), "artist credit")


def _musicbrainz(
    client: MetadataClient, identifier: str, release_id: str | None
) -> tuple[dict, str]:
    url = f"https://musicbrainz.org/ws/2/discid/{identifier}?" + urlencode(
        {"inc": "artists recordings", "fmt": "json"}
    )
    try:
        response = client.json(url, "musicbrainz-disc.json")
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError(
                "Disc ID is not registered in MusicBrainz; use an explicit --apple-album-id"
            ) from exc
        raise
    candidates = [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "date": r.get("date"),
            "country": r.get("country"),
        }
        for r in _objects(response.get("releases", []), "MusicBrainz releases")
    ]
    write_json(client.output / "candidates.json", {"releases": candidates})
    ids = [candidate["id"] for candidate in candidates]
    if release_id is None:
        if len(ids) != 1:
            raise ValueError(
                "MusicBrainz release is ambiguous/missing; "
                "inspect candidates.json and use --release-id"
            )
        release_id = ids[0]
    release_id = str(uuid.UUID(str(release_id)))
    if release_id not in ids:
        raise ValueError("Selected MusicBrainz release does not contain this Disc ID")
    release_url = f"https://musicbrainz.org/ws/2/release/{release_id}?" + urlencode(
        {"inc": "artists recordings artist-credits discids", "fmt": "json"}
    )
    release = client.json(release_url, "musicbrainz-release.json")
    media = [
        m
        for m in _objects(release.get("media", []), "MusicBrainz media")
        if any(d.get("id") == identifier for d in _objects(m.get("discs", []), "discs"))
    ]
    if release.get("id") != release_id or len(media) != 1:
        raise ValueError("MusicBrainz release does not uniquely match this disc")
    medium = media[0]
    artist = _credit(release.get("artist-credit"))
    tracks = []
    for track in _objects(medium.get("tracks", []), "MusicBrainz tracks"):
        recording = track.get("recording", {})
        if not isinstance(recording, dict):
            raise ValueError("Invalid MusicBrainz recording")
        tracks.append(
            {
                "number": track.get("position"),
                "title": _text(track.get("title") or recording.get("title"), "title"),
                "artist": _credit(track.get("artist-credit") or recording["artist-credit"])
                if track.get("artist-credit") or recording.get("artist-credit")
                else artist,
                "duration_ms": track.get("length") or recording.get("length"),
                "provider_track_id": recording.get("id", ""),
            }
        )
    return {
        "title": _text(release.get("title"), "album"),
        "artist": artist,
        "album_artist": artist,
        "date": _text(release.get("date"), "date"),
        "genre": None,
        "copyright": None,
        "disc_number": medium.get("position"),
        "disc_total": len(release["media"]),
        "tracks": tracks,
        "source": {
            "provider": "musicbrainz",
            "id": release_id,
            "url": release_url,
            "catalog_url": f"https://musicbrainz.org/release/{release_id}",
            "edition": "selected release",
        },
    }, f"https://coverartarchive.org/release/{release_id}/front-500"


def _artwork(
    client: MetadataClient,
    provider_url: str | None,
    *,
    local: Path | None,
    url: str | None,
) -> dict:
    provenance = {"provider_source_url": provider_url, "selected_at": now()}
    if local is not None:
        path = local.expanduser().resolve()
        if not path.is_file():
            raise ValueError("Local artwork must be a regular JPEG/PNG file")
        with path.open("rb") as source:
            data = source.read(MAX_COVER_BYTES + 1)
        # Do not embed private absolute paths into files that may later be shared.
        provenance.update(selection="explicit_file", source_name=path.name, source_url=None)
    else:
        chosen = url or provider_url
        if chosen is None:
            raise ValueError("Provider has no cover; specify --artwork or --artwork-url")
        data = client.get(chosen, "cover.download", image=True)
        provenance.update(
            selection="explicit_url" if url else "provider_default", source_url=chosen
        )
    info = cover_info(data)
    name = "cover.jpg" if info["format"] == "JPEG" else "cover.png"
    (client.output / name).write_bytes(data)
    if local is None:
        (client.output / "cover.download").unlink()
    return info | provenance | {"file": name}


def fetch_metadata(
    capture: Path,
    output: Path,
    *,
    apple_album_id: int | None = None,
    release_id: str | None = None,
    artwork: Path | None = None,
    artwork_url: str | None = None,
    edition: str | None = None,
) -> dict:
    if apple_album_id is not None and release_id is not None:
        raise ValueError("Select either Apple or MusicBrainz, not both")
    if artwork is not None and artwork_url is not None:
        raise ValueError("Select either --artwork or --artwork-url, not both")
    if edition is not None:
        edition = _text(edition, "edition")
        if artwork is None and artwork_url is None:
            raise ValueError(
                "--edition is a label, not artwork lookup; also specify --artwork or --artwork-url"
            )
    if artwork_url is not None:
        artwork_url = _text(artwork_url, "artwork URL")
        parts = urlsplit(artwork_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(char.isspace() for char in artwork_url)
        ):
            raise ValueError(
                "Artwork URL must use HTTPS without embedded credentials or whitespace"
            )
    with operation_report(output, "metadata", capture) as report:
        image = SavedImage.load(capture)
        identifier = disc_id(image.toc)
        client = MetadataClient(output)
        report.update(
            musicbrainz_disc_id=identifier,
            source_toc_sha256=hashlib.sha256(image.raw_toc).hexdigest(),
        )
        album, cover_url = (
            _apple(client, apple_album_id)
            if apple_album_id is not None
            else _musicbrainz(client, identifier, release_id)
        )
        maximum = validate_tracks(image.toc, album["tracks"])
        if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", album["date"]):
            raise ValueError("Invalid album release date")
        if (
            type(album["disc_number"]) is not int
            or type(album["disc_total"]) is not int
            or not 1 <= album["disc_number"] <= album["disc_total"] <= 99
        ):
            raise ValueError("Invalid album disc number/count")
        report.update(
            album=album["title"], artist=album["artist"], provider=album["source"]["provider"]
        )
        art = _artwork(client, cover_url, local=artwork, url=artwork_url)
        if edition is not None:
            album["edition"] = {
                "label": edition,
                "basis": "user-specified",
                "note": "Packaging/pressing is not inferred or verified from the CD TOC",
            }
            report["edition"] = album["edition"]
        if artwork is not None:
            report["artwork_input_path"] = str(artwork.expanduser().resolve())
        album.update(
            schema_version=1,
            musicbrainz_disc_id=identifier,
            source_toc_sha256=report["source_toc_sha256"],
            artwork=art,
            duration_tolerance_ms=DURATION_TOLERANCE_MS,
            maximum_duration_difference_ms=maximum,
        )
        write_json(output / "album.json", album)
        report.update(
            status="metadata_ready",
            album_sha256=hashlib.sha256((output / "album.json").read_bytes()).hexdigest(),
            artwork=album["artwork"],
            track_count=len(album["tracks"]),
            maximum_duration_difference_ms=maximum,
        )
    return report

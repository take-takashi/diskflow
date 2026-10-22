"""Discover optical media and collect non-authoritative metadata search hints."""

import hashlib
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from diskflow.toc import parse_toc

Chooser = Callable[[str, list[dict]], int]


def inspect_system() -> dict:
    from diskflow.cli import inspect

    return inspect()


def mount_hints(mount: dict) -> dict:
    info = mount.get("disk_info", {})
    title = info.get("VolumeName") or Path(mount["mount"]).name
    title = unicodedata.normalize("NFC", str(title))
    if title.casefold() in ("audio cd", "音楽cd", "untitled", "名称未設定"):
        title = ""
    tracks = {}
    for item in mount.get("track_files", []):
        match = re.fullmatch(r"(\d{1,2})\s+(.+)\.(?:aiff?|aifc)", item.get("name", ""), re.I)
        if match:
            name = unicodedata.normalize("NFC", match[2])
            if not re.fullmatch(r"(?:audio\s*track|track|トラック)\s*\d*", name, re.I):
                tracks[int(match[1])] = name
    return {"album": title, "track_titles": tracks}


def discover_cd(*, choose: Chooser | None = None) -> dict:
    state = inspect_system()
    media = state.get("cd_media")
    mounts = state.get("mounts", [])
    if not isinstance(media, list):
        raise RuntimeError("光学ドライブの情報を取得できませんでした。接続を確認してください。")
    candidates = {}
    # Never choose a generic disk from diskutil's AllDisks list.
    for item in media:
        name = item.get("BSD Name")
        if isinstance(name, str) and re.fullmatch(r"disk\d+", name):
            candidates[name] = {"device": f"/dev/r{name}", "label": name, "hints": {}}
    for mount in mounts:
        raw = mount.get("toc_plist", {}).get("Format 0x02 TOC Data")
        if not isinstance(raw, bytes):
            continue
        toc = parse_toc(raw)
        toc.audio_session()
        info = mount.get("disk_info", {})
        name = info.get("ParentWholeDisk") or info.get("DeviceIdentifier")
        if not isinstance(name, str) or name not in candidates:
            continue
        if info.get("FilesystemType") != "cddafs":
            continue
        candidates[name] = {
            "device": f"/dev/r{name}",
            "mount": mount["mount"],
            "label": f"{info.get('MediaName', '音楽CD')} / {Path(mount['mount']).name}",
            "hints": mount_hints(mount),
            "toc_sha256": hashlib.sha256(raw).hexdigest(),
        }
    values = list(candidates.values())
    if not values:
        raise ValueError("音楽CDが見つかりません。CDを挿入し、ドライブの接続を確認してください。")
    if len(values) == 1:
        return values[0]
    if choose is None:
        raise ValueError("音楽CDが複数あります。取り込むCD以外を取り出して再実行してください。")
    index = choose("取り込むCDを選んでください", values)
    if type(index) is not int or not 0 <= index < len(values):
        raise ValueError("CDの選択を中止しました。")
    return values[index]

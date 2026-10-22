import unicodedata

import pytest

from diskflow import discovery


def state(make_toc, names=("disk9",)):
    return {
        "cd_media": [{"BSD Name": n} for n in names],
        "diskutil_list": {"AllDisks": ["disk0", "disk1", *names]},
        "mounts": [
            {
                "mount": "/Volumes/アルバム",
                "toc_plist": {"Format 0x02 TOC Data": make_toc()},
                "disk_info": {
                    "ParentWholeDisk": names[0],
                    "FilesystemType": "cddafs",
                    "VolumeName": "アルバム",
                },
                "track_files": [{"name": "1 曲.aiff"}, {"name": "2 Audio Track 2.aiff"}],
            }
        ]
        if names
        else [],
    }


def test_auto_selects_only_optical_media_and_collects_nfc_hints(monkeypatch, make_toc):
    monkeypatch.setattr(discovery, "inspect_system", lambda: state(make_toc))
    found = discovery.discover_cd()
    assert found["device"] == "/dev/rdisk9"
    assert found["hints"]["album"] == unicodedata.normalize("NFC", "アルバム")
    assert found["hints"]["track_titles"] == {1: "曲"}
    assert len(found["toc_sha256"]) == 64


def test_no_cd_never_guesses_a_hard_disk(monkeypatch, make_toc):
    value = state(make_toc, ())
    value["mounts"] = [
        {
            "mount": "/Volumes/not-a-cd",
            "toc_plist": {"Format 0x02 TOC Data": make_toc()},
            "disk_info": {"ParentWholeDisk": "disk0", "FilesystemType": "apfs"},
        }
    ]
    monkeypatch.setattr(discovery, "inspect_system", lambda: value)
    with pytest.raises(ValueError, match="見つかりません"):
        discovery.discover_cd()


def test_multiple_drives_require_human_selection(monkeypatch, make_toc):
    monkeypatch.setattr(discovery, "inspect_system", lambda: state(make_toc, ("disk9", "disk10")))
    with pytest.raises(ValueError, match="複数"):
        discovery.discover_cd()
    assert discovery.discover_cd(choose=lambda *args: 1)["device"] == "/dev/rdisk10"
    with pytest.raises(ValueError, match="中止"):
        discovery.discover_cd(choose=lambda *args: -1)


def test_inspection_failure_is_not_treated_as_no_cd(monkeypatch):
    monkeypatch.setattr(discovery, "inspect_system", lambda: {"cd_media": {"error": "failed"}})
    with pytest.raises(RuntimeError, match="取得できません"):
        discovery.discover_cd()

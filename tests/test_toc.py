import pytest

from diskflow.toc import cue_time, msf_to_lba, parse_toc


def test_binary_msf_not_bcd():
    assert msf_to_lba(0, 2, 0) == 0
    assert msf_to_lba(1, 12, 30) == 5280
    assert cue_time(5280) == "01:10:30"
    with pytest.raises(ValueError):
        msf_to_lba(0, 60, 0)


def test_parse_full_toc_and_relative_cue(make_toc):
    toc = parse_toc(make_toc(starts=(0, 4500), leadout=9000, controls=(1, 2)))
    assert toc.sessions[0].leadout_lba == 9000
    assert [t.start_lba for t in toc.tracks] == [0, 4500]
    cue = toc.cue()
    assert "INDEX 01 00:00:00" in cue
    assert "INDEX 01 01:00:00" in cue
    assert "FLAGS PRE" in cue
    assert "FLAGS DCP" in cue
    assert "INDEX 00" not in "\n".join(cue.splitlines()[1:])


def test_hidden_audio_before_first_index(make_toc):
    toc = parse_toc(make_toc(starts=(100,), leadout=200))
    assert "INDEX 00 00:00:00" in toc.cue()
    assert "INDEX 01 00:01:25" in toc.cue()


@pytest.mark.parametrize("length", [0, 1, 3, 4, 20, 58])
def test_truncated_toc_rejected(make_toc, length):
    with pytest.raises(ValueError):
        parse_toc(make_toc()[:length])


def test_missing_descriptor_rejected(make_toc):
    raw = bytearray(make_toc())
    raw[4 + 3 * 11 + 3] = 0xB0  # Hide track 1, preserving a valid buffer length.
    with pytest.raises(ValueError, match="Missing track 1"):
        parse_toc(bytes(raw))


@pytest.mark.parametrize("starts,leadout", [((5, 0), 11), ((0, 5), 5), ((0, 0), 11)])
def test_non_increasing_boundaries_rejected(make_toc, starts, leadout):
    with pytest.raises(ValueError, match="positions"):
        parse_toc(make_toc(starts=starts, leadout=leadout))


@pytest.mark.parametrize("control", [4, 8])
def test_unsupported_track_types_refused(make_toc, control):
    toc = parse_toc(make_toc(controls=(0, control)))
    with pytest.raises(ValueError, match="not supported"):
        toc.audio_session()


def test_multisession_refused(make_toc):
    first = make_toc(starts=(0,), leadout=5)
    second = make_toc(starts=(10,), leadout=20, session=2)
    payload = bytes((1, 2)) + first[4:] + second[4:]
    toc = parse_toc(len(payload).to_bytes(2, "big") + payload)
    assert len(toc.sessions) == 2
    with pytest.raises(ValueError, match="Multisession"):
        toc.audio_session()

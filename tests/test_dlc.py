import struct
from pathlib import Path

import pytest

from furbyhack import dlc
from furbyhack.dlc import Dlc, Section

SAMPLES = sorted((Path(__file__).parents[1] / "samples").glob("*.dlc"))


def _amf(tracks: list[bytes]) -> bytes:
    d = Dlc([Section(9, "AMF", b"")])
    d.tracks = tracks
    return d.section("AMF").data


def _apl(playlists: list[list[int]]) -> bytes:
    table = 8 + 4 * len(playlists)
    offsets, body = [], b""
    for p in playlists:
        offsets.append((table + len(body)) // 2)
        body += struct.pack(f"<{len(p) + 1}H", *p, dlc.APL_END)
    head = struct.pack("<HHI", len(playlists), len(playlists) + 0x546, 4)
    return head + struct.pack(f"<{len(offsets)}I", *offsets) + body


def _lps(entries: list[list[int]]) -> bytes:
    d = Dlc([Section(11, "LPS", struct.pack("<HI", 0, 3))])
    d.lip_sync = entries
    return d.section("LPS").data


def _synthetic() -> Dlc:
    track = dlc.A18_TRACK_PREFIX + bytes(range(40)) * 3
    return Dlc(
        [
            Section(1, "PAL", b"\x01\x02" * 8),
            Section(9, "AMF", _amf([track, dlc.A18_TRACK_PREFIX])),
            Section(10, "APL", _apl([[0, 0x1010, 1]])),
            Section(11, "LPS", _lps([[0x8000 | 100, 200]])),
        ]
    )


def test_synthetic_round_trip() -> None:
    raw = _synthetic().build()
    assert Dlc.parse(raw).build() == raw


def test_header_layout() -> None:
    d = _synthetic()
    raw = d.build()
    assert raw.startswith(dlc.MAGIC)
    assert len(raw) == dlc.HEADER_SIZE + sum(len(s.data) for s in d.sections)


def test_tracks_and_playlists() -> None:
    d = Dlc.parse(_synthetic().build())
    assert len(d.tracks) == 2
    assert dlc.track_duration_ms(d.tracks[0]) == 60
    assert d.playlists == [[0, 0x1010, 1]]


def test_replace_track() -> None:
    d = _synthetic()
    new = dlc.A18_TRACK_PREFIX + b"\xaa" * 400
    d.tracks = [new, d.tracks[1]]
    assert Dlc.parse(d.build()).tracks[0] == new


def test_lip_sync_round_trip() -> None:
    d = Dlc.parse(_synthetic().build())
    assert d.lip_sync == [[0x8000 | 100, 200]]
    d.lip_sync = [[0x8000 | 50, 300, 0x8000 | 20]]
    assert Dlc.parse(d.build()).lip_sync == [[0x8000 | 50, 300, 0x8000 | 20]]


def test_lip_sync_from_levels() -> None:
    levels = [0, 0, 0, 1, 1, 1, 1, 0, 0, 0]
    assert dlc.lip_sync_from_levels(levels, 20) == [0x8000 | 60, 80, 0x8000 | 60]


def test_lip_sync_merges_short_runs() -> None:
    # A single 20 ms blip of silence inside speech is folded into the open run.
    levels = [1, 1, 1, 1, 0, 1, 1, 1]
    assert dlc.lip_sync_from_levels(levels, 20) == [160]


def test_lip_sync_total_matches_audio() -> None:
    levels = [0.0, 0.9, 0.1, 0.8, 0.8, 0.0, 0.0, 0.7] * 10
    words = dlc.lip_sync_from_levels(levels, 20)
    assert sum(w & dlc.LPS_MAX_MS for w in words) == len(levels) * 20


def test_track_cuts_short_audio_is_one_track() -> None:
    assert dlc.track_cuts([1.0] * 100, 20, max_ms=12_000) == []


def test_track_cuts_prefer_quiet_moments() -> None:
    levels = [1.0] * 1000  # 20 s at 20 ms steps
    levels[520] = 0.0  # a pause at 10.4 s, inside the last quarter of the first 12 s
    cuts = dlc.track_cuts(levels, 20, max_ms=12_000)
    assert cuts[0] == 520
    bounds = [0, *cuts, len(levels)]
    assert all((b - a) * 20 <= 12_000 for a, b in zip(bounds, bounds[1:], strict=False))


def test_playlists_setter_round_trip() -> None:
    d = Dlc.parse(_synthetic().build())
    d.playlists = [[1, 0x1020, 0, 1]]
    assert Dlc.parse(d.build()).playlists == [[1, 0x1020, 0, 1]]
    with pytest.raises(ValueError):
        d.playlists = [[0], [1]]


def test_encode_track_format() -> None:
    track = dlc.encode_track(bytes(640 * 5))  # 5 frames of silence
    assert track.startswith(dlc.A18_TRACK_PREFIX)
    assert dlc.track_duration_ms(track) == 100


def test_checksum_is_adler32() -> None:
    assert dlc.checksum(b"Wikipedia") == 0x11E60398  # reference Adler-32 value


def test_bad_magic() -> None:
    with pytest.raises(ValueError):
        Dlc.parse(b"\x00" * dlc.HEADER_SIZE)


# Safety gate before any upload: parse + build must reproduce real files exactly.
# Samples are gitignored, so these run only where they were downloaded.
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_sample_round_trip(path: Path) -> None:
    raw = path.read_bytes()
    d = Dlc.parse(raw)
    assert d.build() == raw
    d.tracks = d.tracks  # re-serialize AMF from decoded tracks
    d.lip_sync = d.lip_sync  # and LPS
    d.playlists = d.playlists  # and APL
    assert d.build() == raw


HACKED = Path(__file__).parents[1] / "samples" / "hacked.dlc"


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_action_playlist_on_hacked_sample() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    # Traced by hand: XLS 74/0/3/4 -> SEQ 50 -> playlist 36 -> track 11
    assert d.action_playlist(*dlc.SPEECH_ACTION) == 36
    assert d.playlists[36] == [11]


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_set_speech_changes_only_audio_and_lip_sync() -> None:
    raw = HACKED.read_bytes()
    d = Dlc.parse(raw)
    pl = d.set_speech(dlc.SPEECH_ACTION, bytes(640 * 50), [0.0, 1.0] * 25, 20)
    new = Dlc.parse(d.build())
    old = Dlc.parse(raw)
    assert dlc.track_duration_ms(new.tracks[11]) == 1000
    assert new.lip_sync[pl] != old.lip_sync[pl]
    changed = {s.name for s, o in zip(new.sections, old.sections, strict=True) if s.data != o.data}
    assert changed == {"AMF", "LPS"}


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_long_speech_becomes_several_tracks_in_one_playlist() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    steps = 1000  # 20 s
    pl = d.set_speech(dlc.SPEECH_ACTION, bytes(640 * steps), [1.0] * steps, 20)
    new = Dlc.parse(d.build())
    items = new.playlists[pl]
    assert len(items) == 2 and items[0] == 11 and items[1] == 146  # extra track appended
    assert all(dlc.track_duration_ms(new.tracks[i]) <= dlc.MAX_TRACK_MS for i in items)
    assert sum(dlc.track_duration_ms(new.tracks[i]) for i in items) == 20_000
    assert sum(w & dlc.LPS_MAX_MS for w in new.lip_sync[pl]) == 20_000
    assert new.action_playlist(*dlc.SPEECH_ACTION) == pl


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_stop_motion_keeps_layout() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    before = d.section("SEQ").data
    d.stop_motion(dlc.SPEECH_ACTION)
    after = d.section("SEQ").data
    assert len(after) == len(before)
    assert b"\x09\xc4" in before and b"\x09\xc4" not in after  # 0xC409, little-endian
    assert d.action_playlist(*dlc.SPEECH_ACTION) == 36


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_sequences_round_trip() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    raw = d.section("SEQ").data
    d.sequences = d.sequences
    assert d.section("SEQ").data == raw


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_sync_motion_times_the_stop_to_the_speech() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    d.sync_motion(dlc.SPEECH_ACTION, 9_500)
    new = Dlc.parse(d.build())
    words = new.sequences[50]  # the speech action's sequence in hacked.dlc
    assert words[:3] == [0x3000, 0x456A, 0xC409]  # header, playlist, original motion
    delays = [w & 0x0FFF for w in words if w >> 12 == 0x1]
    assert sum(delays) == 9_500 and max(delays) <= 0x0FFF
    assert words[-1] == dlc.STOP_MOTION
    assert new.action_playlist(*dlc.SPEECH_ACTION) == 36  # still plays our playlist


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_minimal_speech_dlc() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    pl = d.set_speech(dlc.SPEECH_ACTION, bytes(640 * 50), [1.0] * 50, 20)
    d.drop_eye_images()
    d.silence_tracks_except(set(d.playlists[pl]))
    raw = d.build()
    small = Dlc.parse(raw)

    assert {s.name for s in small.sections} == {"XLS", "AMF", "APL", "LPS", "SEQ", "MTR"}
    # other sections keep their table slots and counters
    assert [s.slot for s in small.sections] == [4, 9, 10, 11, 12, 13]
    assert dlc.track_duration_ms(small.tracks[11]) == 1000
    assert all(dlc.track_duration_ms(t) == 20 for i, t in enumerate(small.tracks) if i != 11)
    assert small.action_playlist(*dlc.SPEECH_ACTION) == pl  # action chain still intact
    seq = small.section("SEQ").data
    words = struct.unpack_from(f"<{len(seq) // 2}H", seq)
    assert 0x8401 not in words and 0x8051 in words
    assert len(seq) == len(Dlc.parse(HACKED.read_bytes()).section("SEQ").data)
    assert len(raw) < 70_000


def _tree_shape(d: Dlc) -> list[tuple[int, ...]]:
    """Counts at every level of the XLS tree, in traversal order."""
    x = d.section("XLS").data
    shape: list[tuple[int, ...]] = []
    (inputs,) = struct.unpack_from("<H", x)
    for i in range(inputs):
        n2, off2 = struct.unpack_from("<HI", x, 6 + 6 * i)
        for j in range(n2):
            n3, off3 = struct.unpack_from("<HI", x, off2 * 2 + 6 * j)
            for k in range(n3):
                n4 = struct.unpack_from("<H", x, off3 * 2 + 20 * k + 4)[0]
                shape.append((i, j, k, n4))
    return shape


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_route_all_actions_keeps_tree_shape() -> None:
    before = Dlc.parse(HACKED.read_bytes())
    d = Dlc.parse(HACKED.read_bytes())
    d.route_all_actions_to(dlc.SPEECH_ACTION)
    after = Dlc.parse(d.build())

    assert _tree_shape(after) == _tree_shape(before)
    assert len(after.section("XLS").data) < len(before.section("XLS").data) // 2
    # every action now plays the speech playlist, including other inputs
    for action in [dlc.SPEECH_ACTION, (75, 0, 0, 0), (75, 0, 8, 0), (1, 0, 0, 0), (18, 0, 0, 1)]:
        assert after.action_playlist(*action) == 36


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_clear_lip_sync_except() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    keep = d.lip_sync[36]
    d.clear_lip_sync_except(36)
    lps = Dlc.parse(d.build()).lip_sync
    assert lps[36] == keep
    assert all(w == [dlc.LPS_CLOSED | 20] for i, w in enumerate(lps) if i != 36)


def test_cel_round_trip() -> None:
    indices = bytes(i % 64 for i in range(64 * 64))
    data = dlc.encode_cel(indices)
    assert len(data) == dlc.CEL_BYTES
    assert data[:3] == bytes([0x00, 0x10, 0x83])  # pixels 0,1,2,3 -> 6 bits each, MSB first
    assert dlc.decode_cel(data) == indices


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_eye_images_replace_both_eyes() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    images = [bytes([k + 1]) * (128 * 128) for k in range(3)]  # solid colours 1, 2, 3
    palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    d.set_eye_images(images, palette)

    assert len(d.section("CEL").data) == (dlc.KEPT_CELS + 3 * 4) * dlc.CEL_BYTES
    spr = d.section("SPR").data
    for k in range(dlc.SPR_ANIMS):
        count, off = struct.unpack_from("<HI", spr, dlc.SPR_RECORD * k)
        cels = []
        for j in range(count):
            (frame,) = struct.unpack_from("<I", spr, (off + 2 * j) * 2)
            cels += [struct.unpack_from("<H", spr, (frame + 2 * q) * 2)[0] for q in range(4)]
        if k in dlc.EYE_ANIMS:
            assert cels[:4] == [2, 3, 4, 5] and cels[-4:] == [10, 11, 12, 13]
        else:
            assert max(cels) < dlc.KEPT_CELS  # never points at a cel that no longer exists
    pal = d.section("PAL").data
    for p in (4, 5):  # the palettes of the two eye animations
        assert struct.unpack_from("<4H", pal, p * 128) == (0x7C00, 0x03E0, 0x001F, 0x7FFF)
    assert d.section("PAL").data[:128] == Dlc.parse(HACKED.read_bytes()).section("PAL").data[:128]


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_speech_with_eyes_keeps_images_and_shows_them() -> None:
    images = [bytes(128 * 128)]
    raw = dlc.build_speech_dlc(
        HACKED.read_bytes(), bytes(640 * 300), [0.5] * 300, 20, eyes=images, eye_palette=[(0, 0, 0)]
    )
    d = Dlc.parse(raw)
    assert {"PAL", "SPR", "CEL"} <= {s.name for s in d.sections}
    seq = d.sequences[d._sequence_no(dlc.SPEECH_ACTION)]
    eye_words = [w for w in seq if w & 0xF000 == dlc.SEQ_EYE_TAG]
    ours, last = eye_words[:-1], eye_words[-1]
    assert set(ours) == {dlc.SEQ_EYE_TAG | dlc.CUSTOM_EYE_ANIM}
    assert len(ours) == 1 + (6000 - 2432) // 630  # each re-send really takes ~0.63 s
    assert last == dlc.SEQ_EYE_TAG | dlc.BUILTIN_EYE_ANIM  # back to normal eyes


def test_track_cuts_leave_no_tiny_last_track() -> None:
    levels = [1.0] * 1550  # 31 s at 20 ms
    for quiet in (485, 945, 1540):  # quiet spots like the song's; 1540 would leave 0.2 s
        levels[quiet] = 0.0
    cuts = dlc.track_cuts(levels, 20)
    bounds = [0, *cuts, len(levels)]
    lengths = [b - a for a, b in zip(bounds, bounds[1:], strict=False)]
    assert max(lengths) <= 600 and min(lengths) >= 100  # every track 2..12 s


def test_eye_images_from_flat_gif_pad_the_palette(tmp_path: Path) -> None:
    from PIL import Image

    gif = tmp_path / "flat.gif"
    Image.new("RGB", (32, 32), (10, 20, 30)).save(gif)
    eyes, palette = dlc.eye_images_from_gif(gif, 2)
    assert len(eyes) == 2
    assert len(palette) == dlc.PALETTE_COLOURS


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_eye_every_ms_must_fit_a_delay_word() -> None:
    d = Dlc.parse(HACKED.read_bytes())
    with pytest.raises(ValueError):
        d.sync_motion(dlc.SPEECH_ACTION, 5000, dlc.CUSTOM_EYE_ANIM, 5000)


@pytest.mark.skipif(not HACKED.exists(), reason="samples/hacked.dlc not downloaded")
def test_level4_record_checks_every_level_first() -> None:
    xls = Dlc.parse(HACKED.read_bytes()).section("XLS").data
    with pytest.raises(KeyError):
        Dlc._level4_record(xls, (75, 999, 0, 0))
    with pytest.raises(KeyError):
        Dlc._level4_record(xls, (999, 0, 0, 0))
    assert len(Dlc._level4_record(xls, dlc.SPEECH_ACTION)) == 10

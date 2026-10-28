"""Furby Connect DLC package: parse and build. Pure module, no I/O except the dev CLI.

Layout (verified on sample files):
  0x000  magic, 0x28 bytes
  0x028  16 section entries x 38 bytes: UTF-16LE name "DLC_0000.XXX", counter, length, pad
  0x288  section bodies, back to back, in table order
Furby does not check checksums.

Editable: the container, AMF (audio tracks) and LPS (lip-sync). Other sections
are kept as raw bytes, so build() reproduces them unchanged.
"""

import argparse
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from furbyhack.protocol import slot_checksum

MAGIC = "FURBY".encode("utf-16-le") + bytes(22) + bytes.fromhex("7856341202000800")
HEADER_SIZE = 0x288
ENTRY_COUNT = 16
ENTRY_SIZE = 38
_NAME_SIZE = 26
_NAME_PREFIX = "DLC_0000."
# TODO: meaning unknown. hacked.dlc starts here and adds 0x1A per section, but
# official Hasbro files differ (tr003430.dlc), so parsed values are kept as they are.
# These defaults are only used for sections created from scratch.
_COUNTER_START = 0x0040CFB5
_COUNTER_STEP = 0x1A

# A1800 audio: 16 kbit/s, 40-byte frames of 20 ms.
# Each AMF track starts with a u16 = 16000 (bitrate), then frames.
A18_FRAME_BYTES = 40
A18_FRAME_MS = 20
A18_TRACK_PREFIX = struct.pack("<H", 16000)

APL_END = 0xF000
APL_PAUSE_FLAG = 0x1000

# LPS: per playlist, u16 durations in ms ending with 0xFFFF. Bit 0x8000 = mouth
# closed, otherwise open. Verified against the loudness of an official DLC.
LPS_END = 0xFFFF
LPS_CLOSED = 0x8000
LPS_MAX_MS = 0x7FFF
# Shorter mouth states look like jitter; official data rarely goes below ~50 ms.
LPS_MIN_SEGMENT_MS = 60

# In hacked.dlc this action plays SEQ 50 -> playlist 36 -> a single track (verified on hardware).
SPEECH_ACTION = (75, 0, 3, 4)
# Single tracks longer than ~16 s hiss partway on hardware, at 16000 and at 8000 bit/s;
# <= 12 s is clean, and a 2-track playlist of <= 12 s tracks played seamlessly at 16000.
MAX_TRACK_MS = 12_000
MAX_TRACK_BYTES = 24_000  # 12 s at 16000 bit/s; kept as a second cap
# Official tracks are 16000 bit/s (2 KB per second). Lower bitrates only play right as
# the first sound after the Furby starts: at 12000 and 8000 bit/s later phrases (or later
# tracks) were cut, hissed and once crashed it, while 16000 phrases in a row are fine.
# Hypothesis: its decoder doesn't switch bitrate per track. So speech is always 16000.
SPEECH_BITRATE = 16_000


def max_track_ms(bitrate: int) -> int:
    # The time limit holds at any bitrate: 8 kbit/s tracks of 17-21 s (only 17-21 KB)
    # also started hissing partway, while 4 s ones were clean.
    return min(MAX_TRACK_MS, MAX_TRACK_BYTES * 8000 // bitrate)


# SEQ word 0x8xxx = eye animation. Official audio-only DLCs only use ids up to 0x96,
# so those are firmware built-ins. 0x51 is common in official data; how it looks is untested.
SEQ_EYE_TAG = 0x8000
BUILTIN_EYE_MAX = 0x96
BUILTIN_EYE_ANIM = 0x51
# Short DLC motion (MTR entry 11, 5 words) played at the end of speech so the ears stop
# with it. EXPERIMENT: that it ends the running motion is a hypothesis.
STOP_MOTION = 0xC40B
# True: ears move during speech (sync_motion, experimental); False: ears stay still.
EAR_MOTION = True

# --- Eye images, worked out on the samples (tu003410_*.dlc, hacked.dlc) by rendering ---
# CEL: quarter-eye images of 64x64 pixels, 6-bit palette indices, 4 pixels in 3 bytes
# (most significant bits first), no header. An eye is 128x128 = 4 quarters TL, TR, BL, BR.
CEL_SIZE = 64
CEL_BYTES = CEL_SIZE * CEL_SIZE * 6 // 8
EYE_SIZE = 2 * CEL_SIZE
# PAL: 6 palettes x 64 colours, u16 RGB555 with red in bits 10-14.
PALETTE_COLOURS = 64
# SPR starts with 16 animation records of 14 bytes: u16 frame count, u32 word offset of a
# u32 list of frame word offsets, then 4 u16 (palette base?, ?, 64, 0). A frame is
# 4 x (u16 cel, u16 attr) + 0xFFFF; the quarter's palette is (attr >> 10) - 1.
SPR_ANIMS = 16
SPR_RECORD = 14
# Custom eye animation 0x401 draws SPR animations 12 and 13, one per eye (hacked.dlc shows
# "Hac" / "ked"); verified on hardware with our images.
CUSTOM_EYE_ANIM = 0x401
EYE_ANIMS = (12, 13)
# The animation lasts ~2.4 s, then the Furby shows its own idle eyes, and it seems to take
# an eye id only in the short gap between animations. Re-sent every 508 ms (like hacked.dlc)
# our eyes and idle eyes alternated ~2 s each; every 2432 ms ours showed only at start and
# end; every 100 ms ours held for a whole 27.6 s song (verified on hardware).
# A test that looped SPR 12 to 448 frames and set SPR 13's 4th word (64 in every sample)
# to 778 crashed the Furby and forced a factory reset: keep SPR within official values.
EYE_RETRIGGER_MS = 100
EYE_ANIM_MS = 38 * 64  # ~2.4 s seen on hardware; *hypothesis:* 38 frames x 64 ms
# Measured: 287 re-sends written as 100 ms apart ran ~3 min, and the Furby ignored commands
# until the sequence ended; so one "delay 100 + eye id" pair really takes ~0.63 s.
EYE_RESEND_REAL_MS = 630
# Cels 0 and 1 of hacked.dlc are solid fills used by its other animations; kept as they are.
KEPT_CELS = 2


@dataclass
class Section:
    slot: int  # position in the 16-entry table
    name: str  # e.g. "AMF"
    data: bytes
    counter: int | None = None  # header field of unknown meaning; None = compute default


@dataclass
class Dlc:
    sections: list[Section] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: bytes) -> "Dlc":
        if raw[: len(MAGIC)] != MAGIC:
            raise ValueError("Not a Furby DLC (bad magic)")
        sections = []
        offset = HEADER_SIZE
        for slot in range(ENTRY_COUNT):
            entry = raw[0x28 + slot * ENTRY_SIZE : 0x28 + (slot + 1) * ENTRY_SIZE]
            name = entry[:_NAME_SIZE].decode("utf-16-le").rstrip("\0")
            if not name:
                continue
            counter, length = struct.unpack_from("<II", entry, _NAME_SIZE)
            body = raw[offset : offset + length]
            sections.append(Section(slot, name.removeprefix(_NAME_PREFIX), body, counter))
            offset += length
        if offset != len(raw):
            raise ValueError(f"Section lengths add up to {offset}, file is {len(raw)} bytes")
        return cls(sections)

    def build(self) -> bytes:
        entries = [bytes(ENTRY_SIZE)] * ENTRY_COUNT
        default_counter = _COUNTER_START
        for s in self.sections:
            name = (_NAME_PREFIX + s.name).encode("utf-16-le").ljust(_NAME_SIZE, b"\0")
            counter = default_counter if s.counter is None else s.counter
            entries[s.slot] = name + struct.pack("<III", counter, len(s.data), 0)
            default_counter += _COUNTER_STEP
        header = MAGIC + b"".join(entries)
        assert len(header) == HEADER_SIZE
        return header + b"".join(s.data for s in self.sections)

    def section(self, name: str) -> Section:
        for s in self.sections:
            if s.name == name:
                return s
        raise KeyError(name)

    # --- AMF: audio tracks ---

    @property
    def tracks(self) -> list[bytes]:
        """Track payloads (bitrate prefix + A1800 frames), without the length field."""
        amf = self.section("AMF").data
        (count,) = struct.unpack_from("<I", amf)
        offsets = struct.unpack_from(f"<{count}I", amf, 4)
        tracks = []
        for off in offsets:
            (length,) = struct.unpack_from("<I", amf, off)
            tracks.append(amf[off + 4 : off + 4 + length])
        return tracks

    @tracks.setter
    def tracks(self, tracks: list[bytes]) -> None:
        amf = self.section("AMF")
        # u32 count, then u32 byte offsets from section start, then length-prefixed tracks.
        table_size = 4 + 4 * len(tracks)
        offsets, body, pos = [], b"", table_size
        for t in tracks:
            offsets.append(pos)
            body += struct.pack("<I", len(t)) + t
            pos += 4 + len(t)
        amf.data = struct.pack(f"<I{len(tracks)}I", len(tracks), *offsets) + body

    # --- APL: playlists ---

    @property
    def playlists(self) -> list[list[int]]:
        """Each playlist is a list of words: track index, or APL_PAUSE_FLAG | pause length."""
        apl = self.section("APL").data
        (count,) = struct.unpack_from("<H", apl)
        offsets = struct.unpack_from(f"<{count}I", apl, 8)
        playlists = []
        for off in offsets:
            pos, words = off * 2, []  # offsets are in 16-bit words
            while (w := struct.unpack_from("<H", apl, pos)[0]) != APL_END:
                words.append(w)
                pos += 2
            playlists.append(words)
        return playlists

    @playlists.setter
    def playlists(self, playlists: list[list[int]]) -> None:
        section = self.section("APL")
        count, id_end, entry_len = struct.unpack_from("<HHI", section.data)
        if len(playlists) != count:
            # id_end = first playlist id + count; changing the count would shift ids
            raise ValueError("the number of playlists can't change")
        table_end = 8 + 4 * count
        offsets, body = [], b""
        for words in playlists:
            offsets.append((table_end + len(body)) // 2)
            body += struct.pack(f"<{len(words) + 1}H", *words, APL_END)
        head = struct.pack(f"<HHI{count}I", count, id_end, entry_len, *offsets)
        section.data = head + body

    # --- LPS: lip-sync, one entry per playlist ---

    @property
    def lip_sync(self) -> list[list[int]]:
        """Raw u16 words per playlist (see LPS_CLOSED), without the terminator."""
        lps = self.section("LPS").data
        (count,) = struct.unpack_from("<H", lps)
        offsets = struct.unpack_from(f"<{count}I", lps, 6)
        entries = []
        for off in offsets:
            pos, words = 6 + off * 2, []  # word offsets, relative to byte 6
            while (w := struct.unpack_from("<H", lps, pos)[0]) != LPS_END:
                words.append(w)
                pos += 2
            entries.append(words)
        return entries

    @lip_sync.setter
    def lip_sync(self, entries: list[list[int]]) -> None:
        section = self.section("LPS")
        (unknown,) = struct.unpack_from("<I", section.data, 2)  # 3 in all samples, kept as is
        table_end = 6 + 4 * len(entries) + 4  # + 0xFFFFFFFF after the offset table
        offsets, body = [], b""
        for words in entries:
            offsets.append((table_end - 6 + len(body)) // 2)
            body += struct.pack(f"<{len(words) + 1}H", *words, LPS_END)
        section.data = (
            struct.pack(f"<HI{len(entries)}I", len(entries), unknown, *offsets)
            + b"\xff\xff\xff\xff"
            + body
        )

    # --- XLS -> SEQ -> APL: which playlist an action plays (read-only) ---

    def action_playlist(self, input: int, index: int, subindex: int, specific: int) -> int:
        """Playlist index played by action(input, index, subindex, specific)."""
        seq = self.section("SEQ").data
        pos = self._sequence_pos((input, index, subindex, specific))
        apl = self.section("APL").data
        apl_count, apl_base_end = struct.unpack_from("<HH", apl)
        apl_base = apl_base_end - apl_count  # playlist ids start here (0x546 in samples)
        while (w := struct.unpack_from("<H", seq, pos)[0]) != 0:
            if w & 0xF000 == 0x4000:
                return (w & 0x0FFF) - (apl_base & 0x0FFF)
            pos += 2
        raise ValueError(f"the action's sequence plays no playlist: {input, index, subindex}")

    def stop_motion(self, action: tuple[int, int, int, int]) -> None:
        """Replace the motion words of `action`'s sequence with the built-in eye animation.

        The template's motion (0xC409 in hacked.dlc) kept the ears going from the first
        word until well after our (shorter or longer) speech ended. Replacing word for word
        keeps the layout; the mouth still follows LPS.
        """
        seq = bytearray(self.section("SEQ").data)
        pos = self._sequence_pos(action)
        while (w := struct.unpack_from("<H", seq, pos)[0]) != 0:
            if w >> 12 in (0xC, 0xE):  # motion words (MTR)
                struct.pack_into("<H", seq, pos, SEQ_EYE_TAG | BUILTIN_EYE_ANIM)
            pos += 2
        self.section("SEQ").data = bytes(seq)

    def sync_motion(
        self,
        action: tuple[int, int, int, int],
        speech_ms: int,
        eye: int = BUILTIN_EYE_ANIM,
        eye_every_ms: int = 0,
    ) -> None:
        """Keep the template's ear motion only while the speech lasts. EXPERIMENT.

        Sequence words: 0x3000 header, 0x45xx playlist, 0xC4xx DLC motion (MTR entry),
        0xE0xx built-in motion, 0x8xxx eyes, 0x1xxx delay (ms, *assumed*). The template's
        motion 0xC409 runs far longer than our speech, so the sequence becomes: header,
        playlist, that motion, eyes, delays for `speech_ms`, then a short motion that
        should take over and stop the ears (hypothesis: a new motion replaces the running one).
        With `eye_every_ms`, the eye word is repeated that often, as hacked.dlc does for 0x401.
        """
        if not 0 <= eye_every_ms <= 0x0FFF:
            raise ValueError(f"eye_every_ms must fit a delay word (0..4095), got {eye_every_ms}")
        sequences = self.sequences
        k = self._sequence_no(action)
        words = sequences[k]
        new = [w for w in words if w >> 12 in (0x3, 0x4)]  # header + playlist
        new += [w for w in words if w >> 12 in (0xC, 0xE)][:1]  # the original motion
        new.append(SEQ_EYE_TAG | eye)
        if eye_every_ms:
            # Each re-send really takes ~EYE_RESEND_REAL_MS, so size the count to the
            # speech (minus the last animation), or the Furby stays busy long after it.
            for _ in range(max(0, speech_ms - EYE_ANIM_MS) // EYE_RESEND_REAL_MS):
                new += [0x1000 | eye_every_ms, SEQ_EYE_TAG | eye]
            new.append(0x1000 | EYE_ANIM_MS)
            new.append(SEQ_EYE_TAG | BUILTIN_EYE_ANIM)  # back to normal eyes
        else:
            remaining = speech_ms
            while remaining > 0:
                step = min(remaining, 0x0FFF)
                new.append(0x1000 | step)
                remaining -= step
        new.append(STOP_MOTION)
        sequences[k] = new
        self.sequences = sequences

    @property
    def sequences(self) -> list[list[int]]:
        """Words of every sequence, without the 0x0000 terminator."""
        seq = self.section("SEQ").data
        (count,) = struct.unpack_from("<H", seq)
        result = []
        for (off,) in struct.iter_unpack("<I", seq[6 : 6 + 4 * count]):
            pos, words = off * 2, []
            while (w := struct.unpack_from("<H", seq, pos)[0]) != 0:
                words.append(w)
                pos += 2
            result.append(words)
        return result

    @sequences.setter
    def sequences(self, sequences: list[list[int]]) -> None:
        section = self.section("SEQ")
        count, unknown = struct.unpack_from("<HI", section.data)
        if len(sequences) != count:
            raise ValueError("the number of sequences can't change")
        table_end = 6 + 4 * count
        offsets, body = [], b""
        for words in sequences:
            offsets.append((table_end + len(body)) // 2)
            body += struct.pack(f"<{len(words) + 1}H", *words, 0)
        section.data = struct.pack(f"<HI{count}I", count, unknown, *offsets) + body

    def _sequence_no(self, action: tuple[int, int, int, int]) -> int:
        seq = self.section("SEQ").data
        pos = self._sequence_pos(action)
        (count,) = struct.unpack_from("<H", seq)
        offsets = struct.unpack_from(f"<{count}I", seq, 6)
        return offsets.index(pos // 2)

    def _sequence_pos(self, action: tuple[int, int, int, int]) -> int:
        """Byte position in SEQ of the words of the sequence `action` plays.

        XLS levels: [u16 n, u32 word-offset] records (6 bytes) for input and index,
        20-byte records for subindex (n at +4, offset at +6), 10-byte records for
        specific whose first word is 0x1000 + SEQ number. Action input N is XLS entry N-1.
        """
        input, index, subindex, specific = action
        xls = self.section("XLS").data

        def level(record_offset: int, size: int, n_at: int, off_at: int, i: int) -> int:
            count = struct.unpack_from("<H", xls, record_offset + n_at)[0]
            (off,) = struct.unpack_from("<I", xls, record_offset + off_at)
            if not 0 <= i < count:
                raise KeyError(f"action level index {i} out of range 0..{count - 1}")
            return off * 2 + size * i

        (inputs,) = struct.unpack_from("<H", xls)
        if not 1 <= input <= inputs:
            raise KeyError(f"input {input} out of range 1..{inputs}")
        rec = 6 + 6 * (input - 1)
        rec = level(rec, 6, 0, 2, index)
        rec = level(rec, 20, 0, 2, subindex)
        rec = level(rec, 10, 4, 6, specific)
        seq_ref = struct.unpack_from("<H", xls, rec)[0]
        if seq_ref & 0xF000 != 0x1000:
            raise ValueError(f"unexpected sequence reference {seq_ref:#06x}")

        seq = self.section("SEQ").data
        (seq_count,) = struct.unpack_from("<H", seq)
        seq_no = seq_ref & 0x0FFF
        if seq_no >= seq_count:
            raise KeyError(f"sequence {seq_no} out of range")
        return struct.unpack_from("<I", seq, 6 + 4 * seq_no)[0] * 2

    def set_speech(
        self,
        action: tuple[int, int, int, int],
        pcm16k: bytes,
        levels: Sequence[float],
        level_step_ms: int,
        bitrate: int = 16000,
    ) -> int:
        """Make `action` play our audio with matching lip-sync. Returns the playlist index.

        The action's playlist must start as a single track. Audio whose track would
        exceed MAX_TRACK_BYTES at `bitrate` is cut into several tracks at quiet moments;
        the playlist then plays them back to back (extra tracks are appended to AMF). LPS
        covers the whole playlist, so it is built from all levels at once.
        """
        pl = self.action_playlist(*action)
        playlists = self.playlists
        items = playlists[pl]
        if len(items) != 1 or items[0] & APL_PAUSE_FLAG:
            raise ValueError(f"playlist {pl} must be a single track, got {items}")

        step_bytes = 16 * 2 * level_step_ms  # 16 kHz int16 PCM per level step
        bounds = [0, *track_cuts(levels, level_step_ms, max_track_ms(bitrate)), len(levels)]
        chunks = [
            pcm16k[a * step_bytes : b * step_bytes]
            for a, b in zip(bounds, bounds[1:], strict=False)
        ]
        chunks[-1] = pcm16k[bounds[-2] * step_bytes :]  # keep any sub-step remainder

        tracks = self.tracks
        tracks[items[0]] = encode_track(chunks[0], bitrate)
        indices = [items[0]]
        for chunk in chunks[1:]:
            indices.append(len(tracks))
            tracks.append(encode_track(chunk, bitrate))
        self.tracks = tracks
        playlists[pl] = indices
        self.playlists = playlists
        lps = self.lip_sync
        lps[pl] = lip_sync_from_levels(levels, level_step_ms)
        self.lip_sync = lps
        return pl

    def set_eye_images(
        self, images: Sequence[bytes], palette: Sequence[tuple[int, int, int]]
    ) -> None:
        """Show `images` in both eyes as custom eye animation 0x401.

        Each image is 128x128 palette indices (row-major, < 64). The frames of EYE_ANIMS
        step through the images evenly, and their palettes become `palette`. CEL keeps only
        the template's two fill cels plus ours; other animations' cels are pointed at fill 1.
        Made for hacked.dlc as the template.
        """
        if not images or len(palette) > PALETTE_COLOURS:
            raise ValueError("need at least one image and at most 64 colours")
        cels = bytearray(self.section("CEL").data[: KEPT_CELS * CEL_BYTES])
        first: list[int] = []
        for img in images:
            if len(img) != EYE_SIZE * EYE_SIZE or max(img) >= PALETTE_COLOURS:
                raise ValueError("eye images must be 128x128 indices below 64")
            first.append(len(cels) // CEL_BYTES)
            for y0, x0 in ((0, 0), (0, CEL_SIZE), (CEL_SIZE, 0), (CEL_SIZE, CEL_SIZE)):
                rows = (img[(y0 + y) * EYE_SIZE + x0 :][:CEL_SIZE] for y in range(CEL_SIZE))
                cels += encode_cel(b"".join(rows))

        spr = bytearray(self.section("SPR").data)
        palettes: set[int] = set()
        for k in range(SPR_ANIMS):
            count, off = struct.unpack_from("<HI", spr, SPR_RECORD * k)
            for j in range(count):
                (frame,) = struct.unpack_from("<I", spr, (off + 2 * j) * 2)
                for q in range(4):
                    pos = (frame + 2 * q) * 2
                    cel, attr = struct.unpack_from("<HH", spr, pos)
                    if k in EYE_ANIMS:
                        struct.pack_into("<H", spr, pos, first[j * len(images) // count] + q)
                        palettes.add((attr >> 10) - 1)
                    elif cel >= KEPT_CELS:
                        struct.pack_into("<H", spr, pos, KEPT_CELS - 1)

        words = [(r >> 3) << 10 | (g >> 3) << 5 | b >> 3 for r, g, b in palette]
        words += [0] * (PALETTE_COLOURS - len(words))
        pal = bytearray(self.section("PAL").data)
        for p in palettes:
            struct.pack_into(f"<{PALETTE_COLOURS}H", pal, p * PALETTE_COLOURS * 2, *words)
        self.section("CEL").data = bytes(cels)
        self.section("SPR").data = bytes(spr)
        self.section("PAL").data = bytes(pal)

    # --- Shrinking, so uploads take seconds instead of minutes ---

    def drop_eye_images(self, replacement_eye_anim: int = BUILTIN_EYE_ANIM) -> None:
        """Remove PAL/SPR/CEL and point sequences at a built-in eye animation instead.

        Official audio-only DLCs (e.g. tr003430.dlc) ship without these sections and only
        use eye ids 0x03..0x96, which must come from firmware. Custom ids above that need
        the DLC's own sprites, so they are replaced word-for-word (layout unchanged).
        """
        self.sections = [s for s in self.sections if s.name not in ("PAL", "SPR", "CEL")]
        seq = self.section("SEQ")
        data = bytearray(seq.data)
        (count,) = struct.unpack_from("<H", data)
        for (off,) in struct.iter_unpack("<I", data[6 : 6 + 4 * count]):
            pos = off * 2
            while (w := struct.unpack_from("<H", data, pos)[0]) != 0:
                if w & 0xF000 == SEQ_EYE_TAG and (w & 0x0FFF) > BUILTIN_EYE_MAX:
                    struct.pack_into("<H", data, pos, SEQ_EYE_TAG | replacement_eye_anim)
                pos += 2
        seq.data = bytes(data)

    def route_all_actions_to(self, action: tuple[int, int, int, int]) -> None:
        """Make every action play what `action` plays, keeping the tree's shape.

        The XLS tree also maps the firmware's own reactions (it references far more
        sequences than the DLC contains). Levels 1-3 are kept byte-for-byte, so every
        count the firmware may index with stays the same; only the level-4 arrays
        (~70% of XLS) are replaced by one shared array of copies of `action`'s record.
        While such a DLC is active, any reaction plays our sequence.
        """
        xls = self.section("XLS").data
        target = self._level4_record(xls, action)
        (inputs,) = struct.unpack_from("<H", xls)
        head = bytearray(xls[: 6 + 6 * inputs])  # count, unknown u32, level-1 table

        # Re-lay out levels 2 and 3 after the level-1 table, then the shared level-4 array.
        body = bytearray()
        level3_fixups: list[int] = []  # positions (in body) of level-3 records
        max_specifics = 0
        for i in range(inputs):
            n2, off2 = struct.unpack_from("<HI", xls, 6 + 6 * i)
            struct.pack_into("<I", head, 6 + 6 * i + 2, (len(head) + len(body)) // 2)
            level2 = bytearray(xls[off2 * 2 : off2 * 2 + 6 * n2])
            level2_pos = len(body)
            body += level2
            for j in range(n2):
                n3, off3 = struct.unpack_from("<HI", level2, 6 * j)
                new_off3 = (len(head) + len(body)) // 2
                struct.pack_into("<I", body, level2_pos + 6 * j + 2, new_off3)
                for k in range(n3):
                    rec = xls[off3 * 2 + 20 * k : off3 * 2 + 20 * (k + 1)]
                    max_specifics = max(max_specifics, struct.unpack_from("<H", rec, 4)[0])
                    level3_fixups.append(len(body))
                    body += rec
        shared_offset = (len(head) + len(body)) // 2
        for pos in level3_fixups:
            struct.pack_into("<I", body, pos + 6, shared_offset)
        body += target * max_specifics
        self.section("XLS").data = bytes(head + body)

    @staticmethod
    def _level4_record(xls: bytes, action: tuple[int, int, int, int]) -> bytes:
        input, index, subindex, specific = action
        (inputs,) = struct.unpack_from("<H", xls)
        if not 1 <= input <= inputs:
            raise KeyError(f"action {action} not in XLS")
        # Check each level before reading the next, so a bad index can't read garbage.
        n2, off2 = struct.unpack_from("<HI", xls, 6 + 6 * (input - 1))
        if not 0 <= index < n2:
            raise KeyError(f"action {action} not in XLS")
        n3, off3 = struct.unpack_from("<HI", xls, off2 * 2 + 6 * index)
        if not 0 <= subindex < n3:
            raise KeyError(f"action {action} not in XLS")
        n4, off4 = struct.unpack_from("<HI", xls, off3 * 2 + 20 * subindex + 4)
        if not 0 <= specific < n4:
            raise KeyError(f"action {action} not in XLS")
        return xls[off4 * 2 + 10 * specific : off4 * 2 + 10 * (specific + 1)]

    def clear_lip_sync_except(self, keep: int) -> None:
        """Shrink every other playlist's lip-sync to "mouth closed for 20 ms".

        Not empty: we have never seen an empty entry in official data.
        """
        filler = [LPS_CLOSED | A18_FRAME_MS]
        self.lip_sync = [w if i == keep else filler for i, w in enumerate(self.lip_sync)]

    def silence_tracks_except(self, keep: set[int]) -> None:
        """Replace every other audio track with one frame of silence (42 bytes)."""
        silence = encode_track(bytes(320 * 2))  # 320 int16 samples = one 20 ms frame
        self.tracks = [t if i in keep else silence for i, t in enumerate(self.tracks)]


def track_cuts(levels: Sequence[float], step_ms: int, max_ms: int = MAX_TRACK_MS) -> list[int]:
    """Level indices where to cut audio into tracks of at most max_ms each.

    Each cut is the quietest step in the last quarter before the limit, so words
    aren't split mid-sound.
    """
    max_steps = max_ms // step_ms
    cuts: list[int] = []
    start = 0
    while (rest := len(levels) - start) > max_steps:
        if rest <= 2 * max_steps:
            # Last cut: keep a quarter of the rest on each side, so the final track isn't
            # a fraction of a second (a 0.2 s track came out of a 31 s song).
            window = range(
                max(len(levels) - max_steps, start + rest // 4),
                min(start + max_steps, len(levels) - rest // 4),
            )
        else:
            window = range(start + max_steps * 3 // 4, start + max_steps)
        cut = min(window, key=lambda i: levels[i])
        cuts.append(cut)
        start = cut
    return cuts


def build_speech_dlc(
    template: bytes,
    pcm16k: bytes,
    levels: Sequence[float],
    level_step_ms: int,
    action: tuple[int, int, int, int] = SPEECH_ACTION,
    minimal: bool = True,
    bitrate: int = SPEECH_BITRATE,
    eyes: Sequence[bytes] = (),
    eye_palette: Sequence[tuple[int, int, int]] = (),
    eye_every_ms: int = EYE_RETRIGGER_MS,
) -> bytes:
    """Template DLC + our speech (16 kHz PCM, loudness levels) -> DLC bytes where `action` says it.

    minimal=True gives the small file used for fast uploads (see Dlc shrinking methods).
    `eyes` (see Dlc.set_eye_images) are shown while it plays; otherwise built-in eyes.
    """
    dlc = Dlc.parse(template)
    pl = dlc.set_speech(action, pcm16k, levels, level_step_ms, bitrate)
    if eyes:
        dlc.set_eye_images(eyes, eye_palette)
    if minimal:
        if not eyes:
            dlc.drop_eye_images()
        if EAR_MOTION:
            ms = len(pcm16k) * 1000 // (16000 * 2)
            if eyes:
                dlc.sync_motion(action, ms, CUSTOM_EYE_ANIM, eye_every_ms)
            else:
                dlc.sync_motion(action, ms)
        else:
            dlc.stop_motion(action)
        dlc.silence_tracks_except(set(dlc.playlists[pl]))
        dlc.clear_lip_sync_except(pl)
        dlc.route_all_actions_to(action)
    return dlc.build()


def lip_sync_from_levels(
    levels: Sequence[float], step_ms: int, threshold: float = 0.25
) -> list[int]:
    """Mouth open while loudness (0..1 per step_ms) is above threshold. Returns LPS words."""
    runs: list[list[int]] = []  # [is_open, ms]
    for level in levels:
        is_open = int(level > threshold)
        if runs and runs[-1][0] == is_open:
            runs[-1][1] += step_ms
        else:
            runs.append([is_open, step_ms])
    # Fold too-short runs into the previous one to avoid a chattering mouth.
    merged: list[list[int]] = []
    for is_open, ms in runs:
        if merged and (ms < LPS_MIN_SEGMENT_MS or merged[-1][0] == is_open):
            merged[-1][1] += ms
        else:
            merged.append([is_open, ms])
    words = []
    for is_open, ms in merged:
        while ms > 0:
            part = min(ms, LPS_MAX_MS)
            words.append(part if is_open else LPS_CLOSED | part)
            ms -= part
    return words


def encode_cel(indices: bytes) -> bytes:
    """64x64 palette indices (row-major, < 64) -> CEL bytes: 4 pixels in 3 bytes, MSB first."""
    out = bytearray()
    for i in range(0, CEL_SIZE * CEL_SIZE, 4):
        a, b, c, d = indices[i : i + 4]
        out += (a << 18 | b << 12 | c << 6 | d).to_bytes(3, "big")
    return bytes(out)


def decode_cel(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, CEL_BYTES, 3):
        v = int.from_bytes(data[i : i + 3], "big")
        out += bytes([v >> 18 & 63, v >> 12 & 63, v >> 6 & 63, v & 63])
    return bytes(out)


def eye_images_from_gif(
    path: Path, count: int, crop: tuple[int, int, int] | None = None
) -> tuple[list[bytes], list[tuple[int, int, int]]]:
    """`count` evenly spaced frames of an animated image -> 128x128 eye images with one
    shared 64-colour palette. `crop` = (centre x, centre y, square size); default: the
    largest centred square."""
    import numpy as np
    from PIL import Image

    im = Image.open(path)
    n = getattr(im, "n_frames", 1)
    if crop is None:
        crop = (im.width // 2, im.height // 2, min(im.size))
    cx, cy, size = crop
    x0, y0 = cx - size // 2, cy - size // 2
    frames = []
    for k in range(count):
        im.seek(k * n // count)
        # Stretch the edge pixels, so a square bigger than the image still looks natural.
        rgb = np.pad(np.asarray(im.convert("RGB")), ((size, size), (size, size), (0, 0)), "edge")
        square = Image.fromarray(rgb[y0 + size : y0 + 2 * size, x0 + size : x0 + 2 * size])
        frames.append(square.resize((EYE_SIZE, EYE_SIZE), Image.Resampling.LANCZOS))
    strip = Image.new("RGB", (EYE_SIZE, EYE_SIZE * count))
    for k, f in enumerate(frames):
        strip.paste(f, (0, k * EYE_SIZE))
    indexed = strip.quantize(PALETTE_COLOURS)
    # Pillow returns only the used colours, so a flat-coloured GIF gives a short palette.
    flat = (indexed.getpalette() or [])[: 3 * PALETTE_COLOURS]
    flat += [0] * (3 * PALETTE_COLOURS - len(flat))
    palette = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, 3 * PALETTE_COLOURS, 3)]
    data = indexed.tobytes()
    step = EYE_SIZE * EYE_SIZE
    return [data[k * step : (k + 1) * step] for k in range(count)], palette


def encode_track(pcm16k: bytes, bitrate: int = 16000) -> bytes:
    """16 kHz mono int16 PCM -> AMF track payload (u16 bitrate + A1800 frames).

    A1800 allows 4800..32000 bit/s in steps of 800; official tracks use 16000.
    """
    import a1800_codec

    # Native module without type stubs, so pyright can't see its functions.
    encode = a1800_codec.encode  # pyright: ignore[reportAttributeAccessIssue]
    a18 = bytes(encode(pcm16k, bitrate=bitrate))
    return a18[4:]  # drop the codec's u32 length; AMF stores its own


def checksum(raw: bytes) -> int:
    """The checksum Furby reports for a slot after loading it (see protocol.slot_checksum)."""
    return slot_checksum(raw)


def track_duration_ms(track: bytes) -> int:
    """Track length from its own bitrate word (16000 bit/s -> 40-byte frames of 20 ms)."""
    (bitrate,) = struct.unpack_from("<H", track)
    frame_bytes = bitrate * A18_FRAME_MS // 8000
    return (len(track) - len(A18_TRACK_PREFIX)) // frame_bytes * A18_FRAME_MS


# --- Dev CLI: `uv run python -m furbyhack.dlc info samples/x.dlc` ---


def _info(dlc: Dlc) -> None:
    for s in dlc.sections:
        print(f"{s.name}  slot {s.slot:2}  {len(s.data):7} bytes")
    tracks = dlc.tracks
    total = sum(track_duration_ms(t) for t in tracks) / 1000
    print(f"\n{len(tracks)} audio tracks, {total:.1f} s total")
    for i, t in enumerate(tracks):
        if track_duration_ms(t) >= 1000:
            print(f"  track {i:3}: {track_duration_ms(t) / 1000:5.1f} s")
    print(f"\n{len(dlc.playlists)} playlists")
    for i, p in enumerate(dlc.playlists[:10]):
        items = [f"pause {w & 0xFFF}" if w & APL_PAUSE_FLAG else f"track {w}" for w in p]
        print(f"  {i:2}: {', '.join(items)}")


def _save_eye_preview(eyes: list[bytes], palette: list[tuple[int, int, int]], path: Path) -> None:
    from PIL import Image

    flat = [c for rgb in palette for c in rgb]
    strip = Image.new("RGB", (EYE_SIZE * len(eyes), EYE_SIZE))
    for k, img in enumerate(eyes):
        frame = Image.frombytes("P", (EYE_SIZE, EYE_SIZE), img)
        frame.putpalette(flat)
        strip.paste(frame.convert("RGB"), (k * EYE_SIZE, 0))
    strip.save(path)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Furby DLC files")
    sub = parser.add_subparsers(dest="cmd", required=True)
    info = sub.add_parser("info")
    info.add_argument("file", type=Path)
    extract = sub.add_parser("extract", help="dump audio tracks as .a18 files")
    extract.add_argument("file", type=Path)
    extract.add_argument("out_dir", type=Path)
    speech = sub.add_parser("speech", help="build a DLC where an action says your text")
    speech.add_argument("template", type=Path, help="known-good DLC, e.g. samples/hacked.dlc")
    speech.add_argument("text")
    speech.add_argument("out", type=Path)
    speech.add_argument("--action", type=int, nargs=4, default=list(SPEECH_ACTION))
    speech.add_argument(
        "--minimal",
        action="store_true",
        help="drop eye images, silence other tracks, route every action to this one "
        "(much smaller upload; while active, any Furby reaction says the text)",
    )
    song = sub.add_parser("song", help="build a DLC where an action plays a song (Furby pitch)")
    song.add_argument("template", type=Path, help="known-good DLC, e.g. samples/hacked.dlc")
    song.add_argument("audio", type=Path, help="any file ffmpeg reads, e.g. song.mp3")
    song.add_argument("out", type=Path)
    song.add_argument("--start", type=float, default=0.0, help="seconds into the audio")
    song.add_argument("--end", type=float, required=True, help="seconds into the audio")
    song.add_argument("--pitch", type=float, default=1.5, help="higher and faster by this")
    song.add_argument("--action", type=int, nargs=4, default=list(SPEECH_ACTION))
    song.add_argument("--wav", type=Path, help="also save the Furby version as WAV")
    song.add_argument("--eyes", type=Path, help="animated GIF to show in both eyes")
    song.add_argument("--eye-frames", type=int, default=6, help="GIF frames to keep (12 KB each)")
    song.add_argument(
        "--eye-crop", type=int, nargs=3, metavar=("X", "Y", "SIZE"), help="square to cut out"
    )
    song.add_argument("--eye-preview", type=Path, help="save the eye frames as a PNG strip")
    song.add_argument(
        "--eye-every", type=int, default=EYE_RETRIGGER_MS, help="re-send the eye id every N ms"
    )
    args = parser.parse_args()

    if args.cmd == "song":
        from furbyhack import tts

        step_ms = A18_FRAME_MS
        samples = tts.load_audio(args.audio, args.start, args.end)
        wav = tts.song_wav(samples, args.pitch)
        if args.wav:
            args.wav.write_bytes(wav)
        pcm = tts.pcm_of(wav)
        action = tuple(args.action)
        eyes, palette = [], []
        if args.eyes:
            crop = tuple(args.eye_crop) if args.eye_crop else None
            eyes, palette = eye_images_from_gif(args.eyes, args.eye_frames, crop)
            if args.eye_preview:
                _save_eye_preview(eyes, palette, args.eye_preview)
        # Minimal, as for speech: the full template plus a long song is far too big.
        raw = build_speech_dlc(
            args.template.read_bytes(),
            pcm,
            tts.envelope(wav, step_ms / 1000),
            step_ms,
            action,
            eyes=eyes,
            eye_palette=palette,
            eye_every_ms=args.eye_every,
        )
        args.out.write_bytes(raw)
        built = Dlc.parse(raw)
        tracks = built.playlists[built.action_playlist(*action)]
        seconds = len(pcm) / 2 / tts.FURBY_RATE
        print(f"Wrote {args.out}: {len(raw)} bytes, {seconds:.1f} s in {len(tracks)} track(s)")
        print(f"  expected slot checksum: {checksum(raw):08x}")
        print(f"  play with: a {' '.join(map(str, action))}")
        return

    if args.cmd == "speech":
        from furbyhack import tts

        step_ms = A18_FRAME_MS
        wav = tts.synthesize_for_furby(args.text)
        levels = tts.envelope(wav, step_ms / 1000)
        action = tuple(args.action)
        raw = build_speech_dlc(
            args.template.read_bytes(), tts.pcm_of(wav), levels, step_ms, action, args.minimal
        )
        args.out.write_bytes(raw)
        built = Dlc.parse(raw)
        pl = built.action_playlist(*action)
        seconds = len(tts.pcm_of(wav)) / 2 / tts.FURBY_RATE
        print(f"Wrote {args.out}: {len(raw)} bytes, {seconds:.1f} s of speech in playlist {pl}")
        print(f"  lip-sync: {built.lip_sync[pl]}")
        print(f"  expected slot checksum: {checksum(raw):08x}")
        print(f"  play with: a {' '.join(map(str, action))}")
        return

    dlc = Dlc.parse(args.file.read_bytes())
    if args.cmd == "info":
        _info(dlc)
    else:  # extract
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for i, t in enumerate(dlc.tracks):
            # Standard .a18 file = u32 frame-data length + track payload (a1800_codec format)
            a18 = struct.pack("<I", len(t) - len(A18_TRACK_PREFIX)) + t
            (args.out_dir / f"track{i:03}.a18").write_bytes(a18)
        print(f"Wrote {len(dlc.tracks)} tracks to {args.out_dir}")


if __name__ == "__main__":
    _main()

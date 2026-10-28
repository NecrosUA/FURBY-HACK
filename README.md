# FurbyHack

A modern desktop hub for the **Hasbro Furby Connect** (the BLE model). Hasbro discontinued the toy and pulled its companion app, so this project brings it back to life.

It is written in async Python for Windows, with plans for a voice-driven AI assistant on top. It builds on earlier community research into the Furby Connect protocol (see [Credits](#credits)).

> **Non-commercial hobby project.** It is not affiliated with, endorsed by or sponsored by Hasbro. Furby and Furby Connect are trademarks of Hasbro. This repository contains no Hasbro content (DLC files, audio, images).

## Status

| Feature | State |
|---|---|
| BLE connection, keepalive, auto-reconnect | ✅ works |
| Tray app: status icon, *Say text…*, *Exit* | ✅ works |
| Offline Furby-like voice (Piper TTS) in **English, Ukrainian and Czech**, detected from the text | ✅ works |
| DLC parser/builder, A1800 audio codec | ✅ works (offline) |
| DLC upload and slot management | ✅ works, but slow (~0.6 KB/s, a 141 KB DLC takes ~3.5 min) |
| Furby speaks typed text through its **own** speaker (via DLC), with lip-sync | ✅ in the tray app: one phrase of up to 130 characters (~10 s), one fast upload of a few seconds |
| Song DLC: any song in a Furby voice ([how](#song-dlc-experiment)) | ✅ works (verified once: 27.6 s in 3 tracks, 80 KB) |
| Custom eye images (animated GIF in both eyes, in song DLCs) | ✅ works (verified: eyes held for a whole 27.6 s song) |

## Requirements

- Windows 10/11 with Bluetooth LE
- Python 3.13 (3.12+ works)
- [uv](https://docs.astral.sh/uv/): `winget install astral-sh.uv`
- Optional, for [song DLCs](#song-dlc-experiment): ffmpeg, `winget install Gyan.FFmpeg`

## Setup

```powershell
uv sync
# Offline voice models (~63-77 MB each, downloaded once into models/): English, Ukrainian, Czech
uv run python -m piper.download_voices --data-dir models en_US-kristin-medium uk_UA-ukrainian_tts-medium cs_CZ-kasandra-medium
# Template for speech DLCs (Hasbro-derived content, so it is not in the repo)
mkdir samples -Force
Invoke-WebRequest https://raw.githubusercontent.com/pdjstone/furby-web-bluetooth/master/dlc/hacked.dlc -OutFile samples/hacked.dlc
```

The voice models are not in the repo because of their size. `download_voices` fetches them from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) on Hugging Face. To download them by hand, put both files of each voice (`.onnx` and `.onnx.json`) into `models/`:

| Language | Voice | Files | Dataset license |
|---|---|---|---|
| English | `en_US-kristin-medium` | [en/en_US/kristin/medium](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/kristin/medium) | public domain (LibriVox) |
| Ukrainian | `uk_UA-ukrainian_tts-medium` | [uk/uk_UA/ukrainian_tts/medium](https://huggingface.co/rhasspy/piper-voices/tree/main/uk/uk_UA/ukrainian_tts/medium) | CC0 |
| Czech | `cs_CZ-kasandra-medium` | [cs/cs_CZ/kasandra/medium](https://huggingface.co/rhasspy/piper-voices/tree/main/cs/cs_CZ/kasandra/medium) | CC BY 4.0, Ondřej Šimek |

The A1800 audio codec comes from a prebuilt wheel in `wheels/`, so Rust is not needed. To rebuild it, install Rust temporarily, run `uv build --wheel --out-dir wheels <extracted sdist>`, then remove Rust again.

## Run

```powershell
uv run furbyhack                        # tray app, no console window
uv run python -m furbyhack              # tray app with console log (it shows the log only, you can't type commands)
uv run python -m furbyhack.furby        # interactive Furby console: slots, upload, load... (see Developer tools)
uv run python -m furbyhack.furby --debug  # same, and logs every BLE packet
uv run python -m furbyhack.tts "Hello"  # try the Furby voice on the PC speakers
uv run python -m furbyhack.dlc info FILE  # inspect, build or extract DLC files (see Developer tools)
```

The console is a separate program with its own BLE connection, so it can't run at the same time as the tray app (see below).

**Tray icon states:**
- **Connected:** pink Furby, green bubble, open eyes.
- **Disconnected or searching:** gray bubble, closed eyes.

**Using the tray icon:**
- **Left-click** or **right-click → Say text…** opens a box (up to 130 characters, with a counter). Type a sentence and press Enter, and the Furby says it **through its own speaker**, moving its mouth in sync. Ctrl+V works in any keyboard layout.
  - Behind the scenes, the app builds a small DLC, uploads it (~4 s for a short phrase), switches to it, plays it, then reactivates your previous DLC. About 5.5 s pass from Enter to speech for short phrases.
  - One phrase is one single-track DLC of at most ~11.5 s. If a text within the 130-character limit still comes out longer (for example numbers spelled as words), it is cut to whole sentences, and a Windows notification shows what was skipped.
  - The tooltip shows the stages: preparing voice → uploading → speaking.
  - Before each phrase the app deletes its previous `FHSPEEC*` copy, so the Furby stores every phrase in the same slot. It never deletes anything while audio plays, and never touches other DLCs.
  - If the BLE link drops mid-phrase, the Furby keeps the speech DLC active. Every idle chatter or touch then says the phrase, because all its actions point at it. The app saves the name of the DLC to restore in `%LOCALAPPDATA%\FurbyHack\state.json` and reactivates it on the next connect, even after an app restart. To do it by hand in the console: `deactivate N` (the active `FHSPEEC*` slot), then `load 7`, `activate`.
  - If something fails, a Windows notification says so. Unplug the Furby for 15–20 s before trying again.
- **Right-click → Disconnect** releases the Furby but keeps the app in the tray; the item then reads **Connect**.
- **Right-click → Exit** disconnects cleanly and quits. If a phrase is in progress, the previous DLC is restored first. **Ctrl+C** in the console does the same.

**The Furby accepts only one BLE connection.** To use the developer console below while the tray app runs, click **Disconnect** first, then **Connect** when you are done.

Logs are written to `%LOCALAPPDATA%\FurbyHack\furbyhack.log` (rotated at 1 MB, two old files kept); uncaught errors end up there too.

## Developer tools

### Furby console: `uv run python -m furbyhack.furby`

It connects to the Furby and accepts commands. Add `--debug` to log every BLE packet sent and received. Wait for `State: connected`, then type:

| Command | What it does |
|---|---|
| `a I X S P` | trigger an action (input index subindex specific), e.g. `a 29 0 0 0` says hello |
| `c R G B` | antenna color 0..255, e.g. `c 0 255 0` |
| `l on` / `l off` | eye LCD backlight |
| `slots` | list all 14 DLC slots: state (`empty` / `filled` / `active`), name, size, checksum. Read-only |
| `upload FILE [NAME.DLC] [barrier=N] [chunk=N]` | upload a DLC file (asks for confirmation). `barrier`: every Nth chunk waits for a write response; `1` (default) is safe but slow, `0` loses data. `chunk`: bytes per write, default 20, at most MTU−3 (the console logs the MTU on connect); anything above 20 is experimental. After `load`, the slot checksum shown by `slots` must equal the "expected checksum" printed after the upload (Adler-32 of the file). |
| `load N` | load the DLC in slot N (needed before `activate`; also refreshes its checksum) |
| `activate` | make the loaded DLC the active one. Takes **no** slot number: `load N` first |
| `deactivate N` | switch off the DLC in slot N; it stays stored |
| `delete N` | delete the DLC in slot N for good (asks for confirmation) |
| `x HEX` / `n HEX` | raw GeneralPlus / Nordic command. **Experts only**: some raw commands change or erase data on the Furby. |
| `q` | disconnect and quit |

Useful actions:
- `a 18 0 0 1`…`17`: silent ear wiggle and blink.
- `a 75 0 X Y`: actions provided by the active DLC.

### Working with DLC slots

The Furby has **14 slots (0–13)**. Each holds one DLC file: a package of sounds, eye images and the reactions that use them. `slots` shows every slot as:
- `empty`: nothing stored;
- `filled`: stored but not in use;
- `active`: the DLC the Furby uses right now. At most one slot is active.

The Furby, not the app, picks the slot for an upload: as observed, the first free one from the top (13, then 12…). The protocol has no way to choose a slot or to reserve a bigger one.

Switching to a DLC always takes three steps: switch off the active one, `load` the new one, then `activate` it:

| Task | Commands |
|---|---|
| See what is on the Furby | `slots` |
| Switch from the active DLC in slot M to slot N | `deactivate M`, `load N`, `activate` |
| Go back to your previous DLC in slot M, if you had one | `deactivate N`, `load M`, `activate` |
| Play a sound from the active DLC | `a 75 0 3 4` (the speech DLCs use this action) |
| Remove a leftover speech DLC | `delete N`, only for `FHSPEEC*` slots |
| Check that an upload is intact | `load N`, then `slots`: the checksum must match the one printed after the upload |

**Never `delete` an official DLC.** It can't be re-downloaded (see [Safety](#safety)). If you are unsure what a slot holds, leave it alone: deactivating is always safe.

### Voice: `uv run python -m furbyhack.tts "Hello, I am Furby!"`

It speaks the text in the Furby voice on the PC speakers.

The language comes from the text:
- Cyrillic → Ukrainian (`uk_UA-ukrainian_tts-medium`, speaker `tetiana`);
- Czech letters such as `ěščřž` → Czech (`cs_CZ-kasandra-medium`);
- anything else → English (`en_US-kristin-medium`). Czech written without diacritics is detected as English.

The Ukrainian model knows only Cyrillic, so before synthesis:
- **numbers** become plain nominative words (`1990-х` → «тисяча дев'ятсот дев'яносто»; case endings are dropped, which is deliberately simple);
- **Latin words** are spelled in Cyrillic (`Furby` → «фербі»).

In every language `!` becomes `.` (and `?!` becomes `?`), because the voices audibly "said" the exclamation mark.

Each voice gets its own pitch factor, so all languages end up at the same Furby pitch (~291 Hz): `pitch = 291 Hz / voice's median F0`. Slowness scales with pitch to keep the same tempo. See `VOICES` in `tts.py`.

Options:
- `--lang uk` forces a language;
- `--model en_US-hfc_female-medium` / `--speaker 0` try another model or speaker;
- `--pitch 1.6` changes how high the voice is (it also speeds speech up);
- `--slow 1.4` makes speech slower without changing pitch (default `1.6`, set by `SLOWNESS` in `tts.py`);
- `--save out.wav` also saves the audio to a file.

### DLC files: `uv run python -m furbyhack.dlc`

- `info FILE.dlc` shows the sections, audio tracks and playlists.
- `extract FILE.dlc OUT_DIR` dumps the audio tracks as `.a18` files, in the standard format that `a1800_codec` reads.
- `speech TEMPLATE.dlc "Text" OUT.dlc [--action 75 0 3 4] [--minimal]` builds a DLC in which the action says your text. It uses the Furby voice with lip-sync generated from the voice's loudness. Only that action's audio track and LPS entry change; the rest of the known-good template stays byte-identical. It prints the expected slot checksum. The template must play a single track for that action, and `samples/hacked.dlc` with the default action does.

- `song TEMPLATE.dlc AUDIO OUT.dlc --start S --end E [--pitch 1.5] [--wav OUT.wav]` builds a minimal DLC in which the action plays a song in the Furby voice; see [Song DLC](#song-dlc-experiment).

Sample DLCs go into `samples/`, which is gitignored because the content is Hasbro-derived.

### Tests and checks

```powershell
uv run pytest
uv run ruff check .
uv run ruff format .
uv run pyright
```

No Furby is needed. BLE is replaced by a fake client in tests.

## Song DLC (experiment)

Turn a song into a DLC that the Furby plays in a squeaky Furby voice, with its mouth moving to the music and, optionally, an animated GIF in both eyes. The repo has only the build command, not the song: music is copyrighted and the DLC is built on the Hasbro-derived template, so everyone builds their own copy into the gitignored `samples/`.

You need `samples/hacked.dlc` from [Setup](#setup) and ffmpeg: `winget install Gyan.FFmpeg`.

**1. Build it.** Example: "Jožin z bažin" from 0:04 to 0:45, with a GIF of an eye:

```powershell
uv run python -m furbyhack.dlc song samples/hacked.dlc "C:\path\to\jozin.mp3" samples/jozin.dlc --start 4 --end 45 --wav samples/jozin.wav --eyes "C:\path\to\eye.gif" --eye-crop 173 88 125 --eye-preview samples/jozin_eyes.png
```

- `--pitch 1.5` (default) makes it 1.5× higher **and faster**, so 41 s of song become ~27.6 s. The last 0.5 s fade out. Still end the clip between words: at 0:47 and 0:50 the last word sounded broken on the Furby, 0:45 is clean.
- The audio is cut into tracks of at most 12 s: ~80 KB without eyes.
- `--eyes` takes `--eye-frames` (default 6) evenly spaced frames of the GIF, cuts out the square `--eye-crop X Y SIZE` (centre and size in GIF pixels; default: the largest centred square; a square bigger than the GIF is filled by stretching its edges), scales it to 128×128 and reduces it to 64 colours. Both eyes show the same image. Each frame adds 12 KB, so the example is ~170 KB. Check the frames in the `--eye-preview` PNG.
- So the eyes are a simplified copy of the GIF, not the GIF itself: a square cut out of it, 64 colours, and only a few frames. The Furby's eye animation has 38 frames of (*hypothesis*) 64 ms, and the frames are spread over them, so the motion runs faster and jumpier than in the GIF.
- It prints the **expected slot checksum**. Write it down.
- Listen to `samples/jozin.wav` on the PC first.

**2. Upload it** in the [Furby console](#furby-console-uv-run-python--m-furbyhackfurby). If the tray app runs, click **Disconnect** first.

```
uv run python -m furbyhack.furby
slots                      # note the active slot M (e.g. an official DLC), if any
upload samples/jozin.dlc   # type yes; safe mode, ~2 min per 80 KB, don't unplug the Furby
slots                      # the new slot N holds JOZIN___
```

**3. Check and activate it:**

```
deactivate M
load N
slots                      # slot N's checksum must equal the expected one
activate                   # no slot number: it activates what 'load' loaded
a 75 0 3 4                 # plays the song
```

If the checksum differs, don't activate it: `delete N`, unplug the Furby for 15–20 s, and upload again.

**4. Go back** to your normal Furby: `deactivate N`, then `load M`, `activate` if you had an active DLC M. The song stays in slot N for next time. To remove it, `delete N`, and only that slot.

Notes:
- While the song DLC is active, every action of the DLC plays the song, so the Furby may also start singing on its own. Without `--eyes`, the eyes show the built-in normal eyes.
- The eye id is re-sent often enough to keep our eyes on for the whole song (slower re-sends let the Furby's own eyes take over), but only as many times as fit into the song: each re-send really takes ~0.63 s, and while the sequence runs, the Furby ignores commands. After the song it returns to its normal eyes and you can play the song again right away. Don't try to make the animation longer by editing SPR: that crashed the Furby (see [Safety](#safety)).
- `--eye-crop 173 88 125` (centred on the iris of the 320×180 example GIF) gave an iris that almost fills the eye with a little white around it. Bigger squares show more of the face.
- Verified once with "Jožin z bažin" (79.9 KB, 3 tracks): uploaded in 124 s with the right checksum and played. Earlier, other DLCs above ~60 KB failed their checksum and multi-track phrases played only partly, so always check the checksum.
- The order matters: `load N`, then `activate`. `activate` followed by another `load` left the song silent although `slots` showed it active.

## Safety

- **Official DLCs cannot be re-downloaded.** Hasbro's servers are gone, and the Furby cannot send a DLC back. Before uploading, run `slots` to see what is on your Furby. Never delete a slot you can't replace.
- **Deactivating a DLC is harmless.** `load N` + `activate` brings it back.
- **Don't cut power during an upload.**
- **After a failed or unconfirmed upload, power-cycle the Furby** (unplug it for 15–20 s) before trying again. The Furby may still be writing the previous file, and starting a new upload on top of that makes it glitch (flickering eyes, hissing). A power cycle keeps all DLC slots.
- An upload counts as good only when the slot checksum matches (see `upload` above).
- **A bad DLC can crash the Furby so hard that only the factory reset helps, and that wipes every slot.** On 2026-10-20 an experimental eye DLC (SPR animations made longer than in any official file) left one eye dark and the Furby frozen and unreachable even after a long power cycle; the reset that fixed it also erased the author's irreplaceable official DLC. Change one thing at a time, stay within values seen in official files, and upload experiments only to a Furby whose DLCs you can afford to lose.
- **If the Furby hangs on a DLC:** unplug it for 1–2 minutes, then connect with the console right away and `deactivate` / `delete` that slot.

**Trying a sample DLC**, e.g. `hacked.dlc`, in the Furby console:
1. `upload samples/hacked.dlc`. The Furby stores it in a free slot; `slots` shows which one (N).
2. `deactivate M`, where M is the slot `slots` shows as active, if any.
3. `load N`, then `activate`.
4. `a 75 0 3 4` or `a 75 0 4 4` plays it.
5. To go back: `deactivate N`, `load M`, `activate`.

## Factory reset (last resort)

This clears **all DLC slots**, including official content that can't be restored. Use it only if the Furby misbehaves after a bad DLC upload.

1. Wake the Furby up.
2. Hold it upside down.
3. Hold down its tongue.
4. Pull its tail and keep holding for about 10 seconds, until the eyes go dark.
5. A couple of quiet pops from the speaker confirm the reset.

## Reverse-engineering notes

What we learned on top of the research listed in [Credits](#credits). Verified on a real Furby Connect or on official files unless marked *hypothesis*.

### BLE

- The Furby advertises as `Furby`. Its "Fluff" service `dab91435-…` holds GeneralPlus write/listen `…1383` / `…1382`, Nordic write/listen `…0757` / `…0756`, and FileWrite `…0758`.
- **Keepalive:** write `00` to GeneralPlus every 3 s, otherwise the Furby starts talking on its own. It is paired in Windows settings, but that doesn't interfere with bleak.
- **Silent "talking" motion:** action `18 0 0 1..17` wiggles the ears and blinks, with no sound.
- **Speed:** Windows grants the "ThroughputOptimized" connection preset (15 ms interval instead of ~45 ms), and the Furby accepts it.
- **The ATT MTU stays at 23**, so the maximum is 20 bytes per write and larger upload chunks are not possible.

### DLC upload

- **Flow:** GeneralPlus `50 00 <size:3 BE> 02 <12-char name> 00 00` → Furby answers `24 02` (ready) → 20-byte chunks to FileWrite → `24 05` (OK). Other `24 xx` codes: `03` timeout, `06` error.
- **Slot commands:**
  - `72` gives the slot bitmasks: filled in bytes 3–4, active in 7–8, big-endian, 14 slots;
  - `73 N` gives one slot: name, u24 length, u32 checksum;
  - `60 N` loads, `61` activates, `62 N` deactivates (all answer `dc`), `74 N` deletes.
- **Slot info (`73 N`) for a big DLC is slow:** ~4.3 s for the 624 KB official one, versus ~50 ms for small ones. The app caches slot names per connection.
- The Furby chooses the slot itself. We observed slot 13 every time, with only slot 7 in use. *Hypothesis:* it picks the highest free slot.
- **`load N` on the slot that is already loaded does not refresh it**, even if that slot was deleted and re-uploaded in between. The old checksum stays, and most likely the old content too. So a new copy must never land in the slot that is loaded. The app restores the official DLC after every phrase, so it is the loaded one; the app then deletes its old copy *before* uploading, and the phrase lands in the same slot every time (verified on hardware: 4 phrases in a row, each passed its checksum on the first try; a stale load would still be caught by the checksum check). Retries and multi-part series still use other slots, cleaned up at the end.
- **Uploading under a name that a slot already holds** makes the Furby answer `24 02` then immediately `24 05` ("OK") without taking any data. The app picks a `FHSPEEC0`…`FHSPEEC9` name that no slot holds, and `upload_dlc` reports a transfer that ends before all bytes are sent as an error.
- **The slot checksum is Adler-32 of the whole file.** It is only updated when the slot is loaded (or after a reboot). Before that it shows `0` or even the checksum of the file previously in that slot. It is the only reliable upload check.
- **Speed:**

  | Write mode | Result |
  |---|---|
  | with response, default interval | 0.3 KB/s |
  | with response, 15 ms interval | 0.6 KB/s (~31 ms per chunk) |
  | without response | ~25 KB/s sent, but the Furby never confirms |
  | response on every 8th chunk | the Furby reports timeout |
  | **without response**, 25–57 KB | **✅ 3.4–7.8 s total**, checksum OK every time |

  The Furby needs **~0.1 s per KB** after the last chunk to commit to flash (25 KB → 2.5 s, 48 KB → 4.9 s, 57 KB → 5.5 s), then answers `24 05`. A 141 KB fast upload never confirmed, although it landed intact. **Fast mode occasionally corrupts even small files:** a 28.8 KB upload, the 4th phrase in a row, failed its checksum after "OK". The app then uploads again under a new name into a different slot (up to 3 attempts), and deletes the bad copy only once a good one is in.

**Fast mode has a hard limit near 60 KB:** 59.8 KB arrived intact, but at 64.7 KB the Furby still answered "OK" while the slot checksum was wrong, so the data was corrupted. The app uses fast mode up to 60 KB and the safe mode above that (minutes; the tooltip shows the percentage). A "received OK" from the Furby proves nothing; only the checksum does.
- **Track length limit (~16 s per track, verified):**

  | Track | Result |
  |---|---|
  | 11.9 s | plays cleanly |
  | 16.1 s | hisses at the very end |
  | 17.8 s | hisses partway through |

  All three checksums were correct and the decoded audio is clean, so the limit is in the Furby's playback, possibly a ~32 KB buffer (16 s of A1800).
  - The app keeps **one DLC per phrase** but cuts the audio into **tracks of at most 12 s** at quiet moments. The action's playlist lists them back to back, with extra tracks appended to AMF.
  - **Verified:** a 17.4 s phrase as 2 tracks in one playlist played seamlessly, with no hiss. The limit is per track, not per playback.
  - Speech is also normalized to 90 % peak with 0.25 s of trailing silence.
- **Several uploads in a row don't work reliably:** we uploaded two DLCs one after another and played them in sequence. Both uploaded and activated with correct checksums, but the second one **stayed silent**. The cause is unknown, so the app uses a single DLC per phrase.

- After failed fast uploads the Furby glitched (flickering eyes, hissing). A power cycle fixed it, and a copy turned out intact, so the Furby can keep committing after the transfer "fails". **Always power-cycle before retrying.**

### DLC file format

- **Container:**
  - 0x28-byte magic;
  - 16 entries × 38 bytes: UTF-16LE `DLC_0000.XXX`, a counter of unknown meaning, a length and padding. In `hacked.dlc` the counter is 0x40CFB5 + 0x1A per section, but official Hasbro files differ, so we keep parsed values as they are;
  - bodies from 0x288.
  - Sections: PAL SPR CEL XLS AMF APL LPS SEQ MTR.
- **Action tree (XLS):**
  - `u16 count(75), u32 ?, count × (u16 n, u32 word-offset)`, going through levels 1 → 2 → 3 (20-byte records: `00 00 64 00, u16 n, u32 word-offset, 05 00, …`) → 4 (10-byte records whose first word `0x1000 + k` selects SEQ[k]).
  - **Action `input` maps to XLS index `input - 1`.** `a 75 i s p` plays the active DLC's entry 74.
- **SEQ:** per sequence, tagged u16 words ending with `0000`:
  - `3000` header;
  - `45xx` playlist `xx - 0x46`, i.e. global id `0x4546` + index;
  - `Cxxx` / `Exxx` motion;
  - `8xxx` eye animation;
  - `1xxx` delay.
- **APL:** a u16 list per playlist ending with `F000`; values with bit `0x1000` are pauses in ms, the rest are track indices.
- **AMF:** a u32 count and u32 offsets, then each track as `u32 length + u16 bitrate + A1800 frames` (20 ms each; 40 bytes at the official 16000 bit/s). **Bitrate experiments:**
- At 12000 bit/s, a single-track phrase played fine. A 3-track phrase (cut every 12 s) hissed after track 1, showed eye artifacts and crashed the Furby.
- At 8000 bit/s, a 4 s track plays fine, but **17–21 s single tracks (only 17–21 KB) also hiss partway**. So the per-track limit is **time (~12–16 s), not bytes**, and tracks are cut at 12 s.
- **Playback starts with a delay** after the action command. With only 1.5 s of slack after the phrase's length, switching the DLC back cut off the end of a 17 s phrase. The app now waits 4 s (*hypothesis* for that cut-off).
- **Ears:** the template's speech sequence has a motion word (`0xC409`) that kept the ears going from the first word until well after the speech. Replacing that word with the built-in eye animation `0x8051` stops the ears completely (verified); the mouth still follows LPS.
  - **SEQ words:** `3000` header, `45xx` playlist, `C4xx` DLC motion (MTR entry xx, 13 entries), `E0xx` built-in motion, `8xxx` eyes, `1xxx` delay (ms, *assumed*).
  - *Experiment* (`EAR_MOTION = True` in `dlc.py`): the speech sequence becomes the original motion, then delays as long as the speech, then the short DLC motion `C40B`, so the ears should stop when the speech ends (*hypothesis:* a new motion replaces the running one). Set it to `False` to keep the ears still.
- **Multi-track playlists are unreliable:** one 2-track phrase played seamlessly, another played only partly; a 3-track song (27.6 s) played. Single tracks ≤ 12 s at 16000 bit/s always played right.
- **Splitting long text into several DLCs didn't work well:**
  - as separate phrases there were ~12 s gaps;
  - as a series (next part uploaded during playback, official DLC off in between) the gaps were still long, and part 2 once stayed silent.
  - So the app says **one phrase = one single-track DLC (≤ 11.5 s)**, and the "Say text" box is limited to **130 characters**. Typing stops there, and pasted text is cut at a word boundary. Ukrainian, the slowest voice, reads ~13 characters per second.
- **Only 16000 bit/s is reliable.** At 8000 and 12000, only the *first* sound after starting the app played correctly; every later phrase or track was cut, hissed or crashed the Furby. At 16000, phrases in a row are fine. *Hypothesis:* the decoder keeps its first bitrate and doesn't switch per track, and the official DLC's 16000 sounds play in between. The app uses 16000 only.
- **Our DLCs above ~60 KB fail their checksum even with the safe (with-response) upload** (68.9 KB, 108 s, mismatch), although the 141 KB `hacked.dlc` once uploaded fine. The cause is unknown, so keep speech DLCs below 60 KB. Later an 80 KB song DLC uploaded fine in safe mode (124 s, 0.6 KB/s), and `slots` showed its correct checksum right after the upload, even before `load`.
- **`activate` then `load` of the same slot** left the DLC silent although `slots` showed it active (*hypothesis:* the extra load resets it). `deactivate N`, `load N`, `activate` fixed it.
  - A1800 is **not** G.722.1/Siren. Use [a1800_codec](https://github.com/John-K/a1800_codec).
- **LPS (lip-sync), one entry per playlist:**
  - a list of u16 **durations in ms** ending with `FFFF`;
  - values with bit `0x8000` are **mouth closed**, the rest **mouth open**;
  - the entries add up to the playlist's audio length.
  - We confirmed this against the loudness of all 60 playlists of an official DLC (73% agreement).
  - `hacked.dlc` kept the original LPS with new audio, so its mouth movement is not synced to its audio.
- **Audio-only DLCs are official:** `tr003430.dlc` has only XLS AMF APL LPS SEQ MTR, with no PAL/SPR/CEL. Its SEQ eye ids (`8xxx`) stay within `0x03..0x96`, so those are firmware built-ins. Custom ids like `0x401` in `hacked.dlc` need the DLC's own sprites. Built-in eye id `0x51` shows normal eyes with pupils.
- `hacked.dlc`'s XLS tree references 1283 sequences but contains only 60, so it also points at firmware sequences. However, rerouting every XLS entry did **not** change touch reactions, so those don't go through the active DLC's XLS.
- `dlc speech --minimal` shrinks a speech DLC from 140 KB to **~25 KB + the speech**, verified on hardware:
  - drops PAL/SPR/CEL and swaps custom eye ids for `0x51`;
  - replaces unused tracks with one frame of silence each;
  - shrinks the other lip-sync entries;
  - keeps XLS levels 1–3 byte-for-byte and points every level-4 record at the speech sequence.
- **Going smaller broke playback.** Shared level-3 arrays in XLS (40 KB → 3.7 KB), with unused tracks either empty or one-frame, made the Furby stutter, hiss, speak garbage and once drop the BLE link, even though the checksums were correct. The previously verified 28.5 KB file still played fine on the same Furby right after, so the compact XLS is to blame and it was reverted. **XLS level 3 must stay intact.**
- Not every DLC has 75 inputs: `tr003430.dlc` has 73.
- **Eye images (PAL/SPR/CEL)**, worked out by rendering the samples; custom eyes not yet confirmed on hardware:
  - **CEL:** no header, just quarter-eye images of **64×64 pixels**, 6-bit palette indices, 4 pixels in 3 bytes (most significant bits first), 3072 bytes each. An eye is **128×128** = 4 quarters (top-left, top-right, bottom-left, bottom-right).
  - **PAL:** 6 palettes × 64 colours, u16 RGB555 with red in bits 10–14.
  - **SPR:** 16 animation records of 14 bytes (`u16 frames, u32 word offset of a u32 frame-offset list, u16 ×4`), then the frames: 4 × (`u16 cel, u16 attr`) + `FFFF`. A quarter's palette is `(attr >> 10) - 1`.
  - In `hacked.dlc`, records 12 and 13 (38 frames each) draw "Hac" and "ked", one per eye, and its speech sequence re-sends custom eye id `0x401` every 508 ms. **Verified:** `0x401` shows records 12 and 13; our images appeared in both eyes. Records 8–11 look like lower layers (magenta = transparent?).
  - The animation lasts ~2 s (38 frames; *hypothesis:* the record's `64` is ms per frame). Then the Furby's idle eyes take over. Re-sending `0x401` every 508 ms gave ~2 s ours / ~2 s idle, every 2432 ms only start and end, and **every 100 ms kept ours on for a whole 27.6 s song** (verified). *Hypothesis:* the Furby takes an eye id only in the short gap between two animations.
  - **A sequence word pair takes real time:** 287 pairs of `1064` (100 ms) + `8401` ran ~3 min, and the Furby ignored all commands until the sequence ended (verified twice). So each pair takes ~0.63 s. The builder now writes `(song - 2.4 s) / 0.63 s` pairs, then waits one animation, sends built-in eyes `0x8051` and the stop motion; the song can then be replayed at once (verified).
  - **Crashed the Furby:** record 12 looped to 448 frames (new frame list appended to SPR) together with record 13's `64` set to 778. Unknown which of the two did it. Official files never exceed 38 frames or change the 64.
- **Samples:** official DLCs on the Internet Archive (`furbyconnectfiles`, Russian `tr*.dlc`) have full audio and LPS. Hasbro's own URLs return 403.

## Project layout

```
src/furbyhack/
  app.py       wiring: tray ↔ Furby ↔ TTS, threads, clean shutdown
  tray.py      tray icon, menu, "Say text" dialog
  icons.py     draws the Furby tray icon
  furby.py     BLE client (bleak): connect, reconnect, commands, DLC upload
  protocol.py  UUIDs, command bytes, response parsers (pure)
  dlc.py       DLC file parse/build (pure)
  tts.py       Piper TTS + Furby voice effect
tests/         unit tests (no hardware)
models/        Piper voices (gitignored)
samples/       sample DLC files (gitignored)
wheels/        prebuilt A1800 codec wheel + its Apache-2.0 license
```

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). This matches Piper (GPL-3.0-or-later), which the app depends on. Third-party components keep their own licenses (see [Credits](#credits)).

## Credits

Protocol and file-format research this project relies on:

- [bluefluff](https://github.com/Jeija/bluefluff) (Jeija): BLE protocol documentation and action list
- [furby-web-bluetooth](https://github.com/pdjstone/furby-web-bluetooth) (pdjstone, MIT): DLC upload flow, sample DLCs, factory reset procedure
- [ctxis/Furby](https://github.com/ctxis/Furby) (Context Information Security): DLC format research

Third-party libraries:

- [a1800_codec](https://github.com/John-K/a1800_codec) (John Kelley, Apache-2.0): A1800 audio codec, redistributed as a prebuilt wheel in `wheels/` with its license in `wheels/LICENSE-a1800_codec.txt`
- [Piper](https://github.com/OHF-Voice/piper1-gpl) (GPL-3.0-or-later): offline TTS
- Piper voices (downloaded separately, see [Setup](#setup)): [kristin](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/kristin/medium) (public domain), [ukrainian_tts](https://huggingface.co/rhasspy/piper-voices/tree/main/uk/uk_UA/ukrainian_tts/medium) (CC0), [kasandra](https://huggingface.co/ondrazarici/Cassandra-Kasandra-piper-cz) by Ondřej Šimek (CC BY 4.0)
- [bleak](https://github.com/hbldh/bleak) (MIT), [pystray](https://github.com/moses-palmer/pystray) (LGPL-3.0), [Pillow](https://github.com/python-pillow/Pillow) (MIT-CMU), [NumPy](https://github.com/numpy/numpy) (BSD-3-Clause)

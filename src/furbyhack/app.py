"""Wires tray, Furby, TTS and DLC building together.

Threads: pystray owns the main thread; all BLE work runs on one asyncio
loop in the "ble" thread. Cross only via run_coroutine_threadsafe and
the thread-safe Tray methods.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from furbyhack import dlc, tts
from furbyhack.furby import FAST_UPLOAD_MAX_BYTES, ConnectionState, Furby
from furbyhack.tray import Tray

log = logging.getLogger(__name__)

# Enough for a clean disconnect even if a speech has to switch DLCs back first.
EXIT_TIMEOUT_S = 15.0

# Known-good DLC used as the skeleton for speech DLCs (see README → Setup).
SPEECH_TEMPLATE = Path(__file__).resolve().parents[2] / "samples" / "hacked.dlc"
# Our DLCs are FHSPEEC0/FHSPEEC1…; only DLCs whose name starts with this get deleted
# (this also covers the older fixed name FHSPEECH).
SPEECH_NAME_PREFIX = "FHSPEEC"

_STATUS_TEXT = {
    ConnectionState.DISCONNECTED: "disconnected",
    ConnectionState.CONNECTING: "searching for Furby…",
    ConnectionState.CONNECTED: "connected",
}
_STAGE_TEXT = {
    "voice": "preparing voice…",
    "uploading": "uploading…",
    "speaking": "speaking…",
}


class App:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._furby = Furby(
            self._on_state_change,
            pending_restore=_restore_name(_load_state()),
            on_pending_restore=lambda name: _save_state({"restore_dlc": name}),
        )
        self._tray = Tray(
            on_say=self._on_say,
            on_toggle_connection=self._on_toggle_connection,
            on_exit=self._on_exit,
        )
        self._ble_thread = threading.Thread(target=self._run_ble, name="ble", daemon=True)
        self._furby_task: asyncio.Task[None] | None = None
        self._speech: asyncio.Task[None] | None = None
        self._paused = False  # user pressed Disconnect
        self._exit_lock = threading.Lock()
        self._exiting = False

    def run(self) -> None:
        self._install_ctrl_c()
        self._ble_thread.start()
        self._tray.run()
        self._ble_thread.join(2.0)  # shutdown already finished (or timed out) by now
        log.info("Exited")

    def _install_ctrl_c(self) -> None:
        """Make Ctrl+C in the console do a clean Exit.

        The tray's message loop blocks the main thread, so Python's own KeyboardInterrupt
        never fires. A Windows console control handler runs on its own thread instead.
        Harmless when started without a console.
        """
        import ctypes
        from ctypes import wintypes

        def handler(event: int) -> bool:
            if event in (0, 1):  # CTRL_C_EVENT, CTRL_BREAK_EVENT
                threading.Thread(target=self._on_exit, name="ctrl-c", daemon=True).start()
                return True
            return False

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        self._ctrl_handler = handler_type(handler)  # must stay referenced
        ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)

    def _run_ble(self) -> None:
        """BLE thread: one loop for the whole app; Furby sessions come and go on it."""
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._start_furby)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _start_furby(self) -> None:
        self._furby_task = self._loop.create_task(self._furby.run())
        self._furby_task.add_done_callback(self._on_furby_done)

    def _on_furby_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception():
            log.error("Furby connection loop crashed", exc_info=task.exception())
            self._tray.set_status(False, "error, see log")

    # Called on the BLE thread
    def _on_state_change(self, state: ConnectionState) -> None:
        text = "disconnected (use Connect)" if self._paused else _STATUS_TEXT[state]
        self._tray.set_status(state is ConnectionState.CONNECTED, text)

    # Called on the dialog thread
    def _on_say(self, text: str) -> None:
        asyncio.run_coroutine_threadsafe(self._start_speech(text), self._loop)

    # Called on the tray thread: must not block (see Tray docstring)
    def _on_toggle_connection(self) -> None:
        asyncio.run_coroutine_threadsafe(self._toggle_connection(), self._loop)

    async def _toggle_connection(self) -> None:
        if self._paused:
            log.info("Connect requested")
            self._paused = False
            self._tray.set_paused(False)
            self._start_furby()
        else:
            log.info("Disconnect requested")
            self._paused = True
            self._tray.set_paused(True)
            await self._stop_furby()
            self._on_state_change(self._furby.state)

    # Called on the tray thread or the ctrl-c thread: must not block
    def _on_exit(self) -> None:
        with self._exit_lock:
            if self._exiting:
                return
            self._exiting = True
        log.info("Exit requested")
        # Safety net: remove the icon even if the clean shutdown hangs.
        watchdog = threading.Timer(EXIT_TIMEOUT_S, self._force_exit)
        watchdog.daemon = True
        watchdog.start()
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        except RuntimeError:  # BLE loop already gone
            self._tray.stop()
            return
        future.add_done_callback(lambda f: self._finish_exit(f, watchdog))

    def _finish_exit(self, future: Any, watchdog: threading.Timer) -> None:
        watchdog.cancel()
        if not future.cancelled() and future.exception():
            log.warning("Clean shutdown failed: %r", future.exception())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._tray.stop()

    def _force_exit(self) -> None:
        log.warning("Clean shutdown took longer than %.0f s, exiting anyway", EXIT_TIMEOUT_S)
        self._tray.stop()

    async def _shutdown(self) -> None:
        await self._stop_furby()

    async def _stop_furby(self) -> None:
        if self._speech:
            # Cancelling still runs the speech's cleanup, which reactivates the previous DLC.
            self._speech.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._speech
        await self._furby.disconnect()
        if self._furby_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._furby_task

    async def _start_speech(self, text: str) -> None:
        if self._speech is not None:  # the tray menu is disabled meanwhile; just in case
            log.warning("Already speaking, ignoring: %s", text)
            return
        self._speech = asyncio.current_task()
        try:
            await self._speak(text)
            self._set_stage(None)
        except Exception as e:
            log.exception("Speaking failed")
            self._set_stage(None)
            self._tray.notify(
                f"Furby couldn't say it: {e}\n"
                "If this repeats, unplug the Furby for 15-20 s before trying again."
            )
        finally:
            self._speech = None
            self._tray.set_busy(False)

    async def _speak(self, text: str) -> None:
        log.info("Saying: %s", text)
        self._set_stage("voice")
        # One phrase = one single-track DLC. Splitting long text into several DLCs gave
        # long gaps or silent parts on hardware, so the dialog limits the text length
        # instead (tray.MAX_TEXT_CHARS); this only trims if it still doesn't fit.
        speech = await asyncio.to_thread(build_speech, text)
        log.info(
            "Speech DLC: %d bytes, %.1f s in %d track(s)",
            len(speech.data),
            speech.seconds,
            speech.tracks,
        )
        if speech.skipped:
            log.warning("Text too long, skipped: %s", speech.skipped)
            self._tray.notify(f"Too long for one phrase. Furby skips: {speech.skipped[:120]}")
        await self._furby.play_temporary_dlcs(
            [(speech.data, speech.seconds)],
            SPEECH_NAME_PREFIX,
            dlc.SPEECH_ACTION,
            self._set_stage,
        )

    def _set_stage(self, stage: str | None, _part: int = 1, _parts: int = 1) -> None:
        connected = self._furby.state is ConnectionState.CONNECTED
        text = _STAGE_TEXT[stage] if stage else _STATUS_TEXT[self._furby.state]
        self._tray.set_status(connected, text)


# Survives app restarts: the DLC a speech switched off and must reactivate (see Furby).
STATE_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FurbyHack" / "state.json"


def _load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _restore_name(state: dict[str, Any]) -> str | None:
    name = state.get("restore_dlc")
    return name if isinstance(name, str) else None


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log.warning("Could not save %s: %r", STATE_FILE, e)


# Our speech DLCs above ~60 KB failed their checksum on hardware in both upload modes.
# Multi-track phrases sometimes played only partly, while single tracks <= 12 s always
# played right, so a phrase stays one track.
MAX_SPEECH_DLC_BYTES = min(52_000, FAST_UPLOAD_MAX_BYTES)
MAX_SPEECH_SECONDS = 11.5


@dataclass
class Speech:
    data: bytes
    seconds: float
    tracks: int
    skipped: str  # text that didn't fit in this phrase, "" if none


def build_speech(text: str, template: bytes | None = None) -> Speech:
    """Blocking: the longest start of `text` that fits one phrase, as whole sentences
    (a first sentence that is too long on its own is cut by words). The rest of the text
    is in `skipped`."""
    if template is None:
        if not SPEECH_TEMPLATE.exists():
            raise FileNotFoundError(
                f"Speech template missing: {SPEECH_TEMPLATE} (see README → Setup)"
            )
        template = SPEECH_TEMPLATE.read_bytes()
    sentences = _split_sentences(text)
    for count in range(len(sentences), 0, -1):
        speech = _build(template, " ".join(sentences[:count]))
        if _fits(speech):
            speech.skipped = " ".join(sentences[count:])
            return speech
    words = sentences[0].split()
    rest = " ".join(sentences[1:])
    count = len(words)
    while count > 1:
        count = max(1, count * 4 // 5)
        speech = _build(template, " ".join(words[:count]))
        if _fits(speech):
            speech.skipped = " ".join([*words[count:], rest]).strip()
            return speech
    raise ValueError("Even one word doesn't fit in a speech DLC")


def _fits(speech: Speech) -> bool:
    return len(speech.data) <= MAX_SPEECH_DLC_BYTES and speech.seconds <= MAX_SPEECH_SECONDS


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text.strip())]
    return [p for p in parts if p] or [text.strip()]


def _build(template: bytes, text: str) -> Speech:
    wav = tts.synthesize_for_furby(text)
    pcm = tts.pcm_of(wav)
    levels = tts.envelope(wav, dlc.A18_FRAME_MS / 1000)
    data = dlc.build_speech_dlc(template, pcm, levels, dlc.A18_FRAME_MS)
    max_ms = dlc.max_track_ms(dlc.SPEECH_BITRATE)
    tracks = len(dlc.track_cuts(levels, dlc.A18_FRAME_MS, max_ms)) + 1
    return Speech(data, len(pcm) / 2 / tts.FURBY_RATE, tracks, "")

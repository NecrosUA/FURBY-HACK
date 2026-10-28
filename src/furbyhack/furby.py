"""Furby Connect BLE client: scan, connect, keepalive, auto-reconnect.

The only module that talks to bleak.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from furbyhack import protocol

log = logging.getLogger(__name__)

SCAN_TIMEOUT_S = 10.0
RECONNECT_DELAY_S = 3.0
# Slot info for the big official DLC took ~4.3 s on hardware.
REQUEST_TIMEOUT_S = 10.0
UPLOAD_READY_TIMEOUT_S = 10.0
# After a fast upload Furby may keep writing flash long after the last chunk: once it
# reported nothing within 30 s, yet the slot turned out intact (checksum OK) after a reboot.
UPLOAD_DONE_TIMEOUT_S = 180.0
# Pause while Furby reports more than this many buffered packets.
UPLOAD_MAX_BUFFERED = 10
UPLOAD_WRITE_RETRIES = 3
# Every Nth chunk is written with response, which acts as a barrier: Windows must deliver
# everything queued before it. Measured on hardware with hacked.dlc (141 KB):
#   N=1 (all with response): OK, 218 s at a 15 ms interval
#   N=0 (never): 5.5 s to send, then no confirmation within 30 s (landed intact later)
# Verify intermediate values with dlc.checksum() after 'load'.
UPLOAD_BARRIER_EVERY = 1
# No write responses: verified on hardware up to 59.8 KB (checksum OK); the Furby needs
# ~0.1 s per KB to commit. At 64.7 KB the Furby still said "received OK" but the checksum
# was wrong (corrupted data), and 141 KB never confirmed. Larger files use the safe mode.
FAST_UPLOAD_MAX_BYTES = 60_000
# Wait after the speech before switching DLCs; it is part of the gap between sentences.
# The cut-off ends once seen were 16-17 s tracks, which are now avoided, and the audio
# already ends with 0.25 s of silence.
PLAY_MARGIN_S = 1.0
UPLOAD_ATTEMPTS = 3  # per speech DLC, when the checksum after 'load' doesn't match
# Between activating a speech DLC and triggering its action. Without it, part 2 of a
# series once stayed silent; with 1.0 s all parts played. 0.5 s is being tried.
ACTIVATE_SETTLE_S = 0.5
# After deleting our old copies, before the next upload. TODO: assumption; it is unknown
# whether the Furby has finished erasing when it answers the delete command.
DELETE_SETTLE_S = 0.5

# Errors that mean "this connection attempt/session is over, try again".
BLE_ERRORS = (BleakError, OSError, TimeoutError)


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class NotConnectedError(Exception):
    pass


class UploadError(Exception):
    pass


StateCallback = Callable[[ConnectionState], None]
Listener = Callable[[bytes], None]
ProgressCallback = Callable[[int, int], None]
StageCallback = Callable[[str, int, int], None]  # stage, part number, part count
FindDevice = Callable[[], Awaitable[Any]]
ClientFactory = Callable[[Any, Callable[[Any], None]], Any]


async def _find_furby() -> Any:
    return await BleakScanner.find_device_by_name(protocol.DEVICE_NAME, timeout=SCAN_TIMEOUT_S)


def _make_client(device: Any, on_disconnect: Callable[[Any], None]) -> BleakClient:
    return BleakClient(device, disconnected_callback=on_disconnect)


class _StageTimer:
    """Collects "stage: seconds since previous mark" for one log line."""

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._marks: list[str] = []

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self._marks.append(f"{name} {now - self._last:.1f}s")
        self._last = now

    def __str__(self) -> str:
        return ", ".join(self._marks)


def _winrt_device(client: Any) -> Any:
    # bleak has no public API for this; _backend._requester is its WinRT BluetoothLEDevice.
    return getattr(getattr(client, "_backend", None), "_requester", None)


def connection_interval_ms(client: Any) -> float | None:
    """Current BLE connection interval (Windows only), None if unknown."""
    device = _winrt_device(client)
    if device is None:
        return None
    try:
        return device.get_connection_parameters().connection_interval * 1.25
    except Exception:  # older Windows builds lack the API
        return None


def _request_fast_connection(client: Any) -> Any:
    """Ask Windows for a 15 ms connection interval (default is 30-60 ms).

    Uploads write with response, so each 20-byte chunk costs about one interval:
    a shorter interval is the only safe way to speed them up. Returns the WinRT
    request object, which must stay alive (and be closed later) for the preference
    to hold. None if unsupported. The peripheral may still decline.
    """
    device = _winrt_device(client)
    if device is None:
        return None
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters

        request = device.request_preferred_connection_parameters(
            BluetoothLEPreferredConnectionParameters.throughput_optimized
        )
        log.info("Requested fast BLE connection: %s", request.status)
        return request
    except Exception as e:
        log.warning("Fast BLE connection not available: %r", e)
        return None


async def _wait_any(*events: asyncio.Event) -> None:
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


class Furby:
    """Keeps a connection to the first Furby found, reconnecting until disconnect() is called.

    find_device / client_factory are injectable so tests can run without hardware.
    """

    def __init__(
        self,
        on_state_change: StateCallback | None = None,
        *,
        find_device: FindDevice = _find_furby,
        client_factory: ClientFactory = _make_client,
        pending_restore: str | None = None,
        on_pending_restore: Callable[[str | None], None] | None = None,
    ) -> None:
        """pending_restore / on_pending_restore let the caller persist the name of a DLC
        that a speech switched off, so it gets reactivated even after an app restart."""
        self._on_state_change = on_state_change
        self._find_device = find_device
        self._client_factory = client_factory
        self._pending_restore = pending_restore
        self._on_pending_restore = on_pending_restore
        self._client: Any = None
        self._state = ConnectionState.DISCONNECTED
        self._stop = asyncio.Event()
        self._stopped = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._upload_lock = asyncio.Lock()
        self._last_write = 0.0
        self._listeners: list[tuple[str, bytes, Listener]] = []
        self._mtu: int | None = None
        self._slot_names: dict[int, str] = {}  # slot -> DLC name, see _slot_name()
        self._slot_name_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def mtu(self) -> int | None:
        """Negotiated ATT MTU of the current connection, if known."""
        return self._mtu

    async def run(self) -> None:
        self._task = asyncio.current_task()
        self._stop.clear()
        self._stopped.clear()
        try:
            while not self._stop.is_set():
                self._set_state(ConnectionState.CONNECTING)
                try:
                    await self._session()
                except BLE_ERRORS as e:
                    log.warning("Connection attempt failed: %r", e)
                self._set_state(ConnectionState.DISCONNECTED)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), RECONNECT_DELAY_S)
        finally:
            self._set_state(ConnectionState.DISCONNECTED)
            self._task = None
            self._stopped.set()

    async def disconnect(self) -> None:
        """Stop reconnecting and cleanly close the connection. Safe to call anytime."""
        self._stop.set()
        task = self._task
        if task is None:
            return
        if self._state is not ConnectionState.CONNECTED:
            # Scanning/connecting can take a while, don't wait for it.
            task.cancel()
        await self._stopped.wait()

    async def send(self, command: bytes, *, nordic: bool = False) -> None:
        uuid = protocol.NORDIC_WRITE if nordic else protocol.GP_WRITE
        log.debug("%s -> %s", "nordic" if nordic else "gp", command.hex(" "))
        await self._write(uuid, command)

    async def _write(self, uuid: str, data: bytes, *, response: bool | None = None) -> None:
        client = self._client
        if client is None:
            raise NotConnectedError("Furby is not connected")
        async with self._write_lock:
            await client.write_gatt_char(uuid, data, response)
            self._last_write = asyncio.get_running_loop().time()

    def listen(self, source: str, prefix: bytes, callback: Listener) -> Callable[[], None]:
        """Call callback for "gp"/"nordic" notifications starting with prefix. Returns remover."""
        entry = (source, prefix, callback)
        self._listeners.append(entry)
        return lambda: self._listeners.remove(entry)

    async def request(self, command: bytes, response_prefix: bytes) -> bytes:
        """Send a GeneralPlus command and wait for the notification that answers it."""
        answer: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def on_answer(data: bytes) -> None:
            if not answer.done():
                answer.set_result(data)

        remove = self.listen("gp", response_prefix, on_answer)
        try:
            await self.send(command)
            return await asyncio.wait_for(answer, REQUEST_TIMEOUT_S)
        finally:
            remove()

    # --- DLC slots ---

    async def dlc_slots(self) -> list[protocol.SlotState]:
        resp = await self.request(protocol.dlc_slots_query(), bytes([protocol.RESP_DLC_SLOTS]))
        return protocol.parse_slots(resp)

    async def dlc_slot_info(self, slot: int) -> protocol.SlotInfo | None:
        prefix = bytes([protocol.RESP_DLC_SLOT_INFO, slot])
        return protocol.parse_slot_info(
            await self.request(protocol.dlc_slot_info_query(slot), prefix)
        )

    async def dlc_load(self, slot: int) -> None:
        await self.request(protocol.dlc_load(slot), bytes([protocol.RESP_DLC_LOAD_ACTIVATE]))

    async def dlc_activate(self) -> None:
        await self.request(protocol.dlc_activate(), bytes([protocol.RESP_DLC_LOAD_ACTIVATE]))

    async def dlc_deactivate(self, slot: int) -> None:
        await self.request(protocol.dlc_deactivate(slot), bytes([protocol.RESP_DLC_LOAD_ACTIVATE]))

    async def dlc_delete(self, slot: int) -> None:
        await self.request(protocol.dlc_delete(slot), bytes([protocol.RESP_DLC_DELETE]))
        self._slot_names.pop(slot, None)

    # --- DLC upload ---

    async def upload_dlc(
        self,
        data: bytes,
        filename: str,
        on_progress: ProgressCallback | None = None,
        *,
        barrier_every: int = UPLOAD_BARRIER_EVERY,
        chunk_size: int = protocol.UPLOAD_CHUNK_SIZE,
    ) -> None:
        """Upload a DLC file. The Furby picks the slot; find it with dlc_slots() afterwards.

        Flow: start command -> 0x24 READY_TO_RECEIVE -> 20-byte chunks to
        FILE_WRITE, paused while Furby reports too many buffered packets -> 0x24 RECEIVED_OK.
        chunk_size > 20 needs a larger negotiated MTU and is untested on hardware.
        """
        start = protocol.dlc_upload_start(len(data), filename)  # validates before touching BLE
        max_chunk = self.mtu - 3 if self.mtu else protocol.UPLOAD_CHUNK_SIZE
        if not 1 <= chunk_size <= max_chunk:
            raise ValueError(
                f"chunk_size must be 1..{max_chunk} (MTU {self.mtu}), got {chunk_size}"
            )
        async with self._upload_lock:
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()
            done: asyncio.Future[None] = loop.create_future()
            buffered = 0

            def on_transfer(msg: bytes) -> None:
                mode = msg[1] if len(msg) > 1 else None
                log.info("Upload: Furby reports %r", mode)
                if mode == protocol.FileTransferMode.READY_TO_RECEIVE:
                    ready.set()
                elif mode == protocol.FileTransferMode.RECEIVED_OK and not done.done():
                    done.set_result(None)
                elif (
                    mode
                    in (
                        protocol.FileTransferMode.TIMEOUT,
                        protocol.FileTransferMode.RECEIVED_ERR,
                    )
                    and not done.done()
                ):
                    done.set_exception(UploadError(f"Furby reported {mode!r}"))

            # Diagnostics, logged after sending: flow control is not fully understood yet.
            stats = {"acks": 0, "max_buffered": 0, "overloads": 0}

            def on_ack(msg: bytes) -> None:
                nonlocal buffered
                if len(msg) > 1:
                    buffered = msg[1]
                    stats["acks"] += 1
                    stats["max_buffered"] = max(stats["max_buffered"], buffered)

            def on_overload(_msg: bytes) -> None:
                stats["overloads"] += 1  # 0x0a: packet overload

            removers = [
                self.listen("gp", bytes([protocol.RESP_FILE_TRANSFER]), on_transfer),
                self.listen("nordic", bytes([protocol.NORDIC_PACKET_ACK]), on_ack),
                self.listen("nordic", bytes([protocol.NORDIC_PACKET_OVERLOAD]), on_overload),
            ]
            try:
                await self.send(protocol.nordic_packet_ack(True), nordic=True)
                interval = connection_interval_ms(self._client)
                log.info(
                    "Upload: %s, %d bytes, %d-byte chunks, write response every %d chunks, "
                    "BLE interval %s ms",
                    filename,
                    len(data),
                    chunk_size,
                    barrier_every,
                    interval,
                )
                await self.send(start)
                await asyncio.wait_for(ready.wait(), UPLOAD_READY_TIMEOUT_S)

                started = loop.time()
                sent = 0
                chunk_starts = range(0, len(data), chunk_size)
                for n, pos in enumerate(chunk_starts, start=1):
                    while buffered > UPLOAD_MAX_BUFFERED:
                        if done.done():
                            break
                        await asyncio.sleep(0.05)
                    if done.done():  # Furby ended the transfer early
                        break
                    chunk = data[pos : pos + chunk_size]
                    # The last chunk is always a barrier so the tail is delivered too.
                    barrier = barrier_every > 0 and (
                        n % barrier_every == 0 or n == len(chunk_starts)
                    )
                    await self._write_chunk(chunk, barrier)
                    sent += len(chunk)
                    if on_progress:
                        on_progress(sent, len(data))
                if sent < len(data) and done.done() and not done.exception():
                    # Seen on hardware: with a DLC of the same name already in a slot, the
                    # Furby answers "received OK" right after "ready" without taking data.
                    raise UploadError(
                        f"Furby ended the transfer after {sent} of {len(data)} bytes; "
                        f"a DLC named {filename} is probably on it already"
                    )

                sent_at = loop.time()
                log.info(
                    "Upload: all data sent in %.1f s; Nordic acks %d, max buffered %d, "
                    "overloads %d. Waiting for Furby to confirm (up to %.0f s)...",
                    sent_at - started,
                    stats["acks"],
                    stats["max_buffered"],
                    stats["overloads"],
                    UPLOAD_DONE_TIMEOUT_S,
                )
                try:
                    await asyncio.wait_for(done, UPLOAD_DONE_TIMEOUT_S)
                except TimeoutError:
                    raise UploadError(
                        f"No 'received OK' from Furby {UPLOAD_DONE_TIMEOUT_S:.0f} s after "
                        "sending everything. Power-cycle Furby before another upload."
                    ) from None
                elapsed = loop.time() - started
                log.info(
                    "Upload OK in %.1f s (%.1f KB/s), confirmation came %.1f s after last chunk",
                    elapsed,
                    len(data) / 1024 / elapsed,
                    loop.time() - sent_at,
                )
            finally:
                for remove in removers:
                    remove()
                with contextlib.suppress(*BLE_ERRORS, NotConnectedError):
                    await self.send(protocol.nordic_packet_ack(False), nordic=True)

    async def play_temporary_dlcs(
        self,
        parts: Sequence[tuple[bytes, float]],
        name_prefix: str,
        action: tuple[int, int, int, int],
        on_stage: StageCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Play DLCs one after another: for each (data, seconds) upload it, swap it in,
        play `action` and wait `seconds`. The previously active DLC is switched off once
        at the start and restored once at the end.

        Our DLCs are named `name_prefix` + one character (8 chars, e.g. FHSPEEC0). Only
        slots whose name starts with `name_prefix` are ever deleted; other DLCs (e.g.
        official ones that can't be re-downloaded) are only deactivated and reactivated.
        Our old copies are deleted before uploading, and never while audio plays.
        """
        if len(name_prefix) != 7:
            raise ValueError("name_prefix must be 7 chars (8-char DLC name + suffix)")
        stage = on_stage or (lambda _stage, _n, _total: None)
        timer = _StageTimer()

        stage("uploading", 1, len(parts))
        previous, own_slots, foreign = await self._scan_slots(name_prefix)
        if own_slots:
            # Free the slot first, so the Furby puts each phrase into the same slot instead
            # of alternating. Safe while `previous` was loaded last (restored after every
            # speech); otherwise a stale load is caught by the checksum check below.
            await self._delete_own(own_slots, name_prefix)
            own_slots = set()
            await asyncio.sleep(DELETE_SETTLE_S)
        timer.mark("slots checked")
        switched_off = False
        prefetch: asyncio.Task[tuple[int, str]] | None = None
        last: int | None = None  # the copy that stays on the Furby for next time
        try:
            for n, (data, seconds) in enumerate(parts, start=1):
                expected = protocol.slot_checksum(data)
                prefetched = None
                if prefetch is not None:
                    try:
                        prefetched = await prefetch
                    except (UploadError, TimeoutError) as e:
                        log.warning("Background upload of part %d failed: %r", n, e)
                    prefetch = None
                for attempt in range(1, UPLOAD_ATTEMPTS + 1):
                    if prefetched and attempt == 1:
                        ours, own_name = prefetched  # uploaded while the last part played
                    else:
                        stage("uploading", n, len(parts))
                        ours, own_name = await self._upload_next_own(
                            data, name_prefix, foreign, own_slots, on_progress
                        )
                    timer.mark(f"uploaded {n}")
                    if previous is not None and not switched_off:
                        # Remember before touching it: if the link drops mid-speech, the
                        # Furby keeps our DLC active until we restore this on reconnect.
                        self._set_pending_restore(await self._slot_name(previous))
                        await self.dlc_deactivate(previous)
                        switched_off = True
                    await self.dlc_load(ours)
                    info = await self.dlc_slot_info(ours)
                    if info is not None and info.name == own_name and info.checksum == expected:
                        break
                    # Fast uploads occasionally corrupt even small files (seen on hardware
                    # on the 4th phrase in a row). The bad copy stays until a good one is in.
                    got = f"{info.name} {info.checksum:08x}" if info else "empty"
                    message = f"Checksum mismatch: slot {ours} has {got}, want {expected:08x}"
                    own_slots = own_slots | {ours}
                    if attempt == UPLOAD_ATTEMPTS:
                        raise UploadError(message)
                    log.warning("%s; uploading again (attempt %d)", message, attempt + 1)
                # All our copies stay until the end, so the next part lands elsewhere and
                # nothing is erased while audio plays.
                own_slots = own_slots | {ours}
                last = ours
                await self.dlc_activate()
                # In a series, part 2 once stayed silent when the action came right after
                # switching from part 1 (and deleting it); give the Furby a moment.
                await asyncio.sleep(ACTIVATE_SETTLE_S)
                timer.mark(f"switched {n}")
                stage("speaking", n, len(parts))
                try:
                    await self.send(protocol.action(*action))
                    if n < len(parts):
                        # Upload the next part while this one plays, so the gap between
                        # parts is just the switch. EXPERIMENT: flash writes during
                        # playback are untested on hardware.
                        next_data = parts[n][0]
                        prefetch = asyncio.create_task(
                            self._upload_next_own(next_data, name_prefix, foreign, own_slots)
                        )
                    await asyncio.sleep(seconds + PLAY_MARGIN_S)
                finally:
                    await self.dlc_deactivate(ours)
        finally:
            if prefetch is not None:
                prefetch.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prefetch
            # Bad copies and earlier parts, now that nothing plays. *Hypothesis:* erasing
            # flash during playback (as before) glitched the audio.
            with contextlib.suppress(*BLE_ERRORS, NotConnectedError, TimeoutError):
                await self._delete_own(own_slots - {last}, name_prefix)
            if switched_off and previous is not None:
                await self.dlc_load(previous)
                await self.dlc_activate()
                self._set_pending_restore(None)
            timer.mark("restored")
            log.info("Speech DLC timing: %s", timer)

    async def _upload_next_own(
        self,
        data: bytes,
        name_prefix: str,
        foreign: set[int],
        own_slots: set[int],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[int, str]:
        """Upload under a free name of ours into a new slot; returns (slot, name).

        Our existing copies stay until the new one is verified, so it lands in a
        *different* slot: re-loading the slot that is already loaded doesn't refresh it
        (seen on hardware: a stale checksum came back). It also needs a different name,
        see upload_dlc.
        """
        taken = {self._slot_names.get(s) for s in own_slots}
        own_name = next(
            (name_prefix + c for c in "0123456789" if name_prefix + c not in taken), None
        )
        if own_name is None:
            raise UploadError("All 10 DLC names of ours are in use")
        slot = await self._upload_own(data, own_name, foreign | own_slots, on_progress)
        return slot, own_name

    async def _delete_own(self, slots: set[int], name_prefix: str) -> None:
        """Delete our copies. Checks every name again: other DLCs are never deleted."""
        for slot in sorted(slots):
            name = await self._slot_name(slot) or ""
            if not name.startswith(name_prefix):
                log.error("Refusing to delete slot %d (%r): not ours", slot, name)
                continue
            await self.dlc_delete(slot)

    def _set_pending_restore(self, name: str | None) -> None:
        self._pending_restore = name
        if self._on_pending_restore:
            self._on_pending_restore(name)

    async def _resume_pending_restore(self) -> None:
        """After a reconnect: reactivate the DLC a dropped speech left switched off."""
        name = self._pending_restore
        if not name:
            return
        states = await self.dlc_slots()
        target = None
        for slot, state in enumerate(states):
            if state is protocol.SlotState.EMPTY:
                continue
            if await self._slot_name(slot) == name:
                target = slot
            elif state is protocol.SlotState.ACTIVE:
                await self.dlc_deactivate(slot)  # the speech DLC left active
        if target is None:
            log.warning("DLC %s to restore is gone; nothing to reactivate", name)
        elif states[target] is not protocol.SlotState.ACTIVE:
            log.info("Restoring DLC %s in slot %d after an interrupted speech", name, target)
            await self.dlc_load(target)
            await self.dlc_activate()
        self._set_pending_restore(None)

    async def _upload_own(
        self,
        data: bytes,
        own_name: str,
        occupied: set[int],
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Upload as `own_name`.DLC; return the slot it landed in (the one not in `occupied`)."""
        barrier = 0 if len(data) <= FAST_UPLOAD_MAX_BYTES else 1
        await self.upload_dlc(data, own_name + ".DLC", on_progress, barrier_every=barrier)
        # The slot that appeared is ours; no need to query every slot's name again.
        states = await self.dlc_slots()
        added = [
            s
            for s, st in enumerate(states)
            if st is not protocol.SlotState.EMPTY and s not in occupied
        ]
        if len(added) != 1:
            raise UploadError(f"Can't tell which slot got the upload (new: {added})")
        self._slot_names[added[0]] = own_name
        return added[0]

    async def _slot_name(self, slot: int) -> str | None:
        """Name of a non-empty slot. Cached per connection: only this app changes slots,
        and some queries are slow (~4 s for the big official DLC)."""
        async with self._slot_name_lock:  # warm-up and a speech may ask at the same time
            if slot not in self._slot_names:
                info = await self.dlc_slot_info(slot)
                self._slot_names[slot] = info.name if info else ""
        return self._slot_names[slot] or None

    async def _warm_slot_names(self) -> None:
        """Right after connecting: finish an interrupted restore, then fill the slot-name
        cache so the first speech doesn't wait ~4 s for the big official DLC's info."""
        try:
            await self._resume_pending_restore()
            for slot, state in enumerate(await self.dlc_slots()):
                if state is not protocol.SlotState.EMPTY:
                    await self._slot_name(slot)
            log.info("Slot names cached: %s", self._slot_names)
        except (*BLE_ERRORS, NotConnectedError, ValueError) as e:
            log.warning("Post-connect slot check failed: %r", e)

    async def _scan_slots(self, name_prefix: str) -> tuple[int | None, set[int], set[int]]:
        """Returns (other active slot, our leftover slots, other occupied slots).

        An active leftover of ours (e.g. after a crash mid-speech) is deactivated.
        """
        previous, own, foreign = None, set(), set()
        for slot, state in enumerate(await self.dlc_slots()):
            if state is protocol.SlotState.EMPTY:
                self._slot_names.pop(slot, None)
            elif (await self._slot_name(slot) or "").startswith(name_prefix):
                if state is protocol.SlotState.ACTIVE:
                    await self.dlc_deactivate(slot)
                own.add(slot)
            else:
                foreign.add(slot)
                if state is protocol.SlotState.ACTIVE:
                    previous = slot
        return previous, own, foreign

    async def _write_chunk(self, chunk: bytes, with_response: bool) -> None:
        for attempt in range(1, UPLOAD_WRITE_RETRIES + 1):
            try:
                await self._write(protocol.FILE_WRITE, chunk, response=with_response)
                return
            except BLE_ERRORS as e:
                if attempt == UPLOAD_WRITE_RETRIES:
                    raise UploadError(f"FileWrite failed {attempt} times") from e
                log.warning("FileWrite failed (%r), retrying", e)
                await asyncio.sleep(0.02)

    async def _session(self) -> None:
        """One scan + connect + stay connected. Returns on link loss or stop request."""
        log.info("Scanning for %s...", protocol.DEVICE_NAME)
        device = await self._find_device()
        if device is None:
            log.info("Furby not found")
            return

        loop = asyncio.get_running_loop()
        lost = asyncio.Event()

        def on_disconnect(_client: Any) -> None:
            # bleak may call this from another thread.
            loop.call_soon_threadsafe(lost.set)

        client = self._client_factory(device, on_disconnect)

        log.info("Connecting to %s", device)
        await client.connect()
        fast_link = _request_fast_connection(client)
        self._mtu = getattr(client, "mtu_size", None)
        log.info("BLE MTU: %s (max %s bytes per write)", self._mtu, self._mtu and self._mtu - 3)
        try:
            await client.start_notify(protocol.GP_LISTEN, self._notify_handler("gp"))
            await client.start_notify(protocol.NORDIC_LISTEN, self._notify_handler("nordic"))
            self._client = client
            self._set_state(ConnectionState.CONNECTED)
            keepalive = asyncio.create_task(self._keepalive(lost))
            warm_up = asyncio.create_task(self._warm_slot_names())
            try:
                await _wait_any(lost, self._stop)
            finally:
                keepalive.cancel()
                warm_up.cancel()
            if lost.is_set():
                log.warning("Connection lost")
        finally:
            self._client = None
            self._mtu = None
            self._slot_names.clear()  # another app may change slots while we're away
            if fast_link is not None:
                fast_link.close()
            if client.is_connected:
                log.info("Disconnecting")
                try:
                    await client.disconnect()
                except BLE_ERRORS as e:
                    log.warning("Disconnect failed: %r", e)

    async def _keepalive(self, lost: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(protocol.KEEPALIVE_INTERVAL_S)
            if loop.time() - self._last_write < protocol.KEEPALIVE_INTERVAL_S:
                continue  # other traffic (e.g. an upload) already keeps Furby busy
            try:
                # Not via send(): logging it every 3 s would drown everything else.
                await self._write(protocol.GP_WRITE, protocol.keepalive())
            except (*BLE_ERRORS, NotConnectedError) as e:
                log.warning("Keepalive failed: %r", e)
                lost.set()
                return

    def _notify_handler(self, source: str) -> Callable[[Any, bytearray], None]:
        def handler(_char: Any, data: bytearray) -> None:
            log.debug("%s <- %s", source, data.hex(" "))
            msg = bytes(data)
            for src, prefix, callback in list(self._listeners):
                if src == source and msg.startswith(prefix):
                    callback(msg)

        return handler

    def _set_state(self, state: ConnectionState) -> None:
        if state is self._state:
            return
        self._state = state
        log.info("State: %s", state.value)
        if self._on_state_change:
            self._on_state_change(state)


# --- Dev tool for hardware tests: `uv run python -m furbyhack.furby` ---

_HELP = """Commands:
  a I X S P    trigger action (input index subindex specific)
  c R G B      antenna color (0..255)
  l on|off     LCD backlight
  x HEX        raw GeneralPlus command, e.g. x 14ff0000
  n HEX        raw Nordic command
  slots        show DLC slots (read-only)
  upload FILE [NAME.DLC] [barrier=N] [chunk=N]   upload a DLC file (asks to confirm);
               barrier: every Nth chunk waits for a write response (1 = safest);
               chunk: bytes per write, default 20, up to MTU-3 (experimental).
               Verify the checksum after 'load'.
  load N       load DLC from slot N
  activate     activate the loaded DLC (no slot number: 'load N' first)
  deactivate N deactivate DLC in slot N
  delete N     delete DLC in slot N (asks to confirm)
  q            disconnect and quit"""


# Argument counts of the fixed-size console commands. Extra arguments used to be ignored
# silently, so "activate 11" re-activated the loaded slot instead of slot 11.
_ARITY = {"q": 0, "a": 4, "c": 3, "l": 1, "x": 1, "n": 1, "slots": 0}
_ARITY |= {"load": 1, "activate": 0, "deactivate": 1, "delete": 1}


def _default_dlc_name(path: Path) -> str:
    # Exactly 12 chars; official files look like TU003410.DLC
    return path.stem[:8].upper().ljust(8, "_") + ".DLC"


async def _confirm(question: str) -> bool:
    answer = await asyncio.to_thread(input, f"{question} Type 'yes' to continue: ")
    return answer.strip().lower() == "yes"


async def _print_slots(furby: "Furby") -> None:
    for i, state in enumerate(await furby.dlc_slots()):
        info = await furby.dlc_slot_info(i) if state is not protocol.SlotState.EMPTY else None
        detail = f"  {info.name}  {info.length} bytes  checksum {info.checksum:08x}" if info else ""
        print(f"  slot {i:2}: {state.value}{detail}")


async def _upload_with_progress(
    furby: "Furby", data: bytes, name: str, *, barrier_every: int, chunk_size: int
) -> None:
    # Per-packet debug logs would flood the console during upload.
    level = log.level
    log.setLevel(logging.INFO)
    last = -1

    def progress(sent: int, total: int) -> None:
        nonlocal last
        percent = sent * 100 // total
        if percent != last:
            last = percent
            print(f"\r  uploading {percent:3}% ({sent}/{total})", end="", flush=True)

    try:
        await furby.upload_dlc(
            data, name, progress, barrier_every=barrier_every, chunk_size=chunk_size
        )
    finally:
        print()
        log.setLevel(level)


async def _dev_main(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    logging.getLogger("bleak").setLevel(logging.INFO)

    furby = Furby()
    runner = asyncio.create_task(furby.run())
    print(_HELP)
    try:
        while True:
            line = (await asyncio.to_thread(input, "> ")).split()
            if not line:
                continue
            cmd, args = line[0], line[1:]
            if cmd in _ARITY and len(args) != _ARITY[cmd]:
                hint = " (it activates the slot from 'load N')" if cmd == "activate" else ""
                print(f"error: '{cmd}' takes {_ARITY[cmd]} argument(s){hint}")
                continue
            try:
                if cmd == "q":
                    break
                elif cmd == "a":
                    await furby.send(protocol.action(*map(int, args)))
                elif cmd == "c":
                    await furby.send(protocol.antenna(*map(int, args)))
                elif cmd == "l":
                    if args[0] not in ("on", "off"):
                        raise ValueError("l takes 'on' or 'off'")
                    await furby.send(protocol.lcd(args[0] == "on"))
                elif cmd in ("x", "n"):
                    await furby.send(bytes.fromhex(args[0]), nordic=cmd == "n")
                elif cmd == "slots":
                    await _print_slots(furby)
                elif cmd == "upload":
                    from furbyhack.dlc import checksum

                    opts = {"barrier": UPLOAD_BARRIER_EVERY, "chunk": protocol.UPLOAD_CHUNK_SIZE}
                    for a in [a for a in args if a.split("=")[0] in opts and "=" in a]:
                        key, value = a.split("=", 1)
                        opts[key] = int(value)
                        args.remove(a)
                    path = Path(args[0])
                    data = await asyncio.to_thread(path.read_bytes)
                    name = args[1] if len(args) > 1 else _default_dlc_name(path)
                    question = (
                        f"Upload {path} ({len(data)} bytes) as {name} [{opts['chunk']}-byte "
                        f"chunks, write response every {opts['barrier']} chunks]?"
                    )
                    if await _confirm(question):
                        await _upload_with_progress(
                            furby,
                            data,
                            name,
                            barrier_every=opts["barrier"],
                            chunk_size=opts["chunk"],
                        )
                        await _print_slots(furby)
                        print(f"  expected checksum after 'load': {checksum(data):08x}")
                elif cmd == "load":
                    await furby.dlc_load(int(args[0]))
                elif cmd == "activate":
                    await furby.dlc_activate()
                elif cmd == "deactivate":
                    await furby.dlc_deactivate(int(args[0]))
                elif cmd == "delete":
                    if await _confirm(f"Delete DLC in slot {args[0]}?"):
                        await furby.dlc_delete(int(args[0]))
                else:
                    print(_HELP)
            except (
                ValueError,
                TypeError,
                IndexError,
                OSError,
                NotConnectedError,
                UploadError,
                *BLE_ERRORS,
            ) as e:
                print(f"error: {e!r}")
    finally:
        await furby.disconnect()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Interactive Furby console")
    parser.add_argument("--debug", action="store_true", help="log every BLE packet")
    asyncio.run(_dev_main(parser.parse_args().debug))

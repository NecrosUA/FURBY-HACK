import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import pytest

from furbyhack import furby as furby_mod
from furbyhack import protocol
from furbyhack.furby import ConnectionState, Furby, NotConnectedError, UploadError


class FakeClient:
    """Stands in for BleakClient and plays the Furby side of the DLC protocol."""

    def __init__(self, device: Any, on_disconnect: Callable[[Any], None]) -> None:
        self.on_disconnect = on_disconnect
        self.is_connected = False
        self.notify: list[str] = []
        self.callbacks: dict[str, Callable[[Any, bytearray], None]] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.file_responses: list[bool] = []
        self.upload_size = 0
        self.upload_name = ""
        self.received = b""
        self.fail_upload = False
        self.corrupt_upload = False  # every upload
        self.corrupt_next = 0  # only the next N uploads
        # slot -> [name, data, reported checksum]; checksum is only refreshed on load
        self.slots: dict[int, list[Any]] = {}
        self.active: int | None = None
        self.loaded: int | None = None
        self.deleted: list[int] = []
        self.actions: list[bytes] = []

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, uuid: str, cb: Callable[[Any, bytearray], None]) -> None:
        self.notify.append(uuid)
        self.callbacks[uuid] = cb

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        data = bytes(data)
        self.writes.append((uuid, data))
        if uuid == protocol.FILE_WRITE:
            self.file_responses.append(bool(response))
        if uuid == protocol.GP_WRITE:
            self._gp_command(data)
        elif uuid == protocol.FILE_WRITE:
            self.received += data
            if self.fail_upload:
                self._reply(protocol.GP_LISTEN, bytes([0x24, 6]))
            elif len(self.received) == self.upload_size:
                corrupt = self.corrupt_upload or self.corrupt_next > 0
                self.corrupt_next -= 1
                stored = self.received + (b"!" if corrupt else b"")
                slot = max(s for s in range(14) if s not in self.slots)
                self.slots[slot] = [self.upload_name, stored, 0]
                self._reply(protocol.NORDIC_LISTEN, bytes([0x09, 0]))
                self._reply(protocol.GP_LISTEN, bytes([0x24, 5]))

    def _gp_command(self, data: bytes) -> None:
        cmd, arg = data[0], data[1] if len(data) > 1 else 0
        if cmd == 0x50:
            self.upload_size = int.from_bytes(data[2:5], "big")
            self.upload_name = data[6:14].decode()
            self.received = b""
            self._reply(protocol.GP_LISTEN, bytes([0x24, 2]))
            if any(name == self.upload_name for name, _, _ in self.slots.values()):
                # Like the real Furby: same name already stored -> "OK" without data
                self.upload_size = -1
                self._reply(protocol.GP_LISTEN, bytes([0x24, 5]))
        elif cmd == 0x72:
            filled = sum(1 << s for s in self.slots)
            active = 1 << self.active if self.active is not None else 0
            resp = bytes([0x72, 0, 0]) + filled.to_bytes(2, "big") + bytes(2)
            self._reply(protocol.GP_LISTEN, resp + active.to_bytes(2, "big"))
        elif cmd == 0x73:
            name, body, chk = self.slots.get(arg, ["", b"", 0])
            info = name.encode().ljust(8, b"\0") + len(body).to_bytes(3, "big")
            self._reply(protocol.GP_LISTEN, bytes([0x73, arg]) + info + chk.to_bytes(4, "big"))
        elif cmd == 0x74:
            self.slots.pop(arg, None)
            self.deleted.append(arg)
            self._reply(protocol.GP_LISTEN, bytes([0x74, arg]))
        elif cmd == 0x60:
            # Like the real Furby: loading the slot that is already loaded doesn't refresh
            # it, even if it was deleted and re-uploaded in between (stale checksum).
            if arg != self.loaded:
                self.slots[arg][2] = protocol.slot_checksum(self.slots[arg][1])
            self.loaded = arg
            self._reply(protocol.GP_LISTEN, bytes([0xDC]))
        elif cmd == 0x61:
            self.active = self.loaded
            self._reply(protocol.GP_LISTEN, bytes([0xDC]))
        elif cmd == 0x62:
            if self.active == arg:
                self.active = None
            self._reply(protocol.GP_LISTEN, bytes([0xDC]))
        elif cmd == 0x13:
            self.actions.append(data)

    def _reply(self, uuid: str, msg: bytes) -> None:
        asyncio.get_running_loop().call_soon(self.callbacks[uuid], None, bytearray(msg))

    def drop(self) -> None:
        """Simulate the Furby going away."""
        self.is_connected = False
        self.on_disconnect(self)


class Harness:
    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.states: list[ConnectionState] = []
        self.changed = asyncio.Event()
        self.reachable = asyncio.Event()
        self.reachable.set()
        self.furby = Furby(
            self.on_state, find_device=self.find_device, client_factory=self.make_client
        )

    def on_state(self, state: ConnectionState) -> None:
        self.states.append(state)
        self.changed.set()

    async def find_device(self) -> Any:
        await self.reachable.wait()  # tests clear it to keep the Furby "away"
        return "fake-device"

    def make_client(self, device: Any, on_disconnect: Callable[[Any], None]) -> FakeClient:
        client = FakeClient(device, on_disconnect)
        if self.clients:  # a reconnect: the Furby's slots survive
            old = self.clients[-1]
            client.slots, client.active, client.loaded = old.slots, old.active, old.loaded
        self.clients.append(client)
        return client

    async def wait_state(self, state: ConnectionState) -> None:
        async with asyncio.timeout(1):
            while self.furby.state is not state:
                self.changed.clear()
                await self.changed.wait()


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "RECONNECT_DELAY_S", 0.01)
    monkeypatch.setattr(furby_mod, "ACTIVATE_SETTLE_S", 0)
    monkeypatch.setattr(furby_mod, "DELETE_SETTLE_S", 0)
    monkeypatch.setattr(protocol, "KEEPALIVE_INTERVAL_S", 0.01)


def test_connects_subscribes_and_sends() -> None:
    async def scenario() -> None:
        h = Harness()
        runner = asyncio.create_task(h.furby.run())
        await h.wait_state(ConnectionState.CONNECTED)
        client = h.clients[0]
        assert client.notify == [protocol.GP_LISTEN, protocol.NORDIC_LISTEN]

        await h.furby.send(protocol.antenna(0, 255, 0))
        assert (protocol.GP_WRITE, protocol.antenna(0, 255, 0)) in client.writes

        await h.furby.disconnect()
        await runner
        assert not client.is_connected
        assert h.states[-1] is ConnectionState.DISCONNECTED

    asyncio.run(scenario())


def test_sends_keepalive() -> None:
    async def scenario() -> None:
        h = Harness()
        runner = asyncio.create_task(h.furby.run())
        await h.wait_state(ConnectionState.CONNECTED)
        await asyncio.sleep(0.05)
        assert (protocol.GP_WRITE, protocol.keepalive()) in h.clients[0].writes
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_reconnects_after_drop() -> None:
    async def scenario() -> None:
        h = Harness()
        runner = asyncio.create_task(h.furby.run())
        await h.wait_state(ConnectionState.CONNECTED)
        h.clients[0].drop()
        await h.wait_state(ConnectionState.DISCONNECTED)
        await h.wait_state(ConnectionState.CONNECTED)
        assert len(h.clients) == 2
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_send_when_disconnected_raises() -> None:
    async def scenario() -> None:
        with pytest.raises(NotConnectedError):
            await Furby().send(protocol.keepalive())

    asyncio.run(scenario())


def test_disconnect_interrupts_scan() -> None:
    async def scenario() -> None:
        async def never_found() -> Any:
            await asyncio.sleep(60)

        furby = Furby(find_device=never_found)
        runner = asyncio.create_task(furby.run())
        await asyncio.sleep(0.01)
        async with asyncio.timeout(1):
            await furby.disconnect()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        assert furby.state is ConnectionState.DISCONNECTED

    asyncio.run(scenario())


async def _connected() -> tuple[Harness, asyncio.Task[None]]:
    h = Harness()
    runner = asyncio.create_task(h.furby.run())
    await h.wait_state(ConnectionState.CONNECTED)
    return h, runner


def test_upload_dlc_sends_all_chunks() -> None:
    async def scenario() -> None:
        h, runner = await _connected()
        data = bytes(range(55))
        progress: list[tuple[int, int]] = []
        await h.furby.upload_dlc(data, "TU003410.DLC", lambda s, t: progress.append((s, t)))

        client = h.clients[0]
        assert client.received == data
        chunks = [d for u, d in client.writes if u == protocol.FILE_WRITE]
        assert [len(c) for c in chunks] == [20, 20, 15]
        assert progress[-1] == (55, 55)
        nordic = [d for u, d in client.writes if u == protocol.NORDIC_WRITE]
        assert nordic == [protocol.nordic_packet_ack(True), protocol.nordic_packet_ack(False)]

        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_upload_dlc_reports_furby_error() -> None:
    async def scenario() -> None:
        h, runner = await _connected()
        h.clients[0].fail_upload = True
        with pytest.raises(UploadError):
            await h.furby.upload_dlc(bytes(100), "TU003410.DLC")
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_upload_rejects_bad_name_before_sending() -> None:
    async def scenario() -> None:
        h, runner = await _connected()
        with pytest.raises(ValueError):
            await h.furby.upload_dlc(bytes(10), "short.dlc")
        assert not [u for u, _ in h.clients[0].writes if u != protocol.GP_WRITE]
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_dlc_slots_request() -> None:
    async def scenario() -> None:
        h, runner = await _connected()
        h.clients[0].slots = {0: ["A", b"a", 0], 2: ["B", b"b", 0]}
        h.clients[0].active = 2
        slots = await h.furby.dlc_slots()
        assert slots[0] is protocol.SlotState.FILLED
        assert slots[1] is protocol.SlotState.EMPTY
        assert slots[2] is protocol.SlotState.ACTIVE
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_upload_barrier_every_nth_chunk_and_last() -> None:
    async def scenario() -> None:
        h, runner = await _connected()
        await h.furby.upload_dlc(bytes(20 * 7), "TU003410.DLC", barrier_every=3)
        # chunks 3 and 6 are barriers, plus chunk 7 because it is the last one
        assert h.clients[0].file_responses == [False, False, True, False, False, True, True]
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


OFFICIAL = ["TU021980", b"official content", 0]


async def _furby_with_official_dlc() -> tuple[Harness, asyncio.Task[None], FakeClient]:
    h, runner = await _connected()
    client = h.clients[0]
    client.slots = {7: list(OFFICIAL), 12: ["FHSPEECH", b"old speech", 0]}
    client.active = 7
    return h, runner, client


def test_play_temporary_dlc_restores_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        stages: list[tuple[str, int, int]] = []
        data = bytes(range(256)) * 4
        await h.furby.play_temporary_dlcs(
            [(data, 0)], "FHSPEEC", (75, 0, 3, 4), lambda *a: stages.append(a)
        )

        assert client.deleted == [12]  # only our leftover, never the official slot
        assert 7 in client.slots and client.slots[7][0] == "TU021980"
        assert client.active == 7  # official DLC is active again
        assert client.slots[13][1] == data  # our upload stays for next time
        assert client.actions == [protocol.action(75, 0, 3, 4)]
        assert stages == [("uploading", 1, 1), ("uploading", 1, 1), ("speaking", 1, 1)]
        assert not any(client.file_responses)  # small file -> fast mode
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_play_temporary_dlc_checksum_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        client.corrupt_upload = True
        with pytest.raises(UploadError, match="Checksum mismatch"):
            await h.furby.play_temporary_dlcs([(bytes(100), 0)], "FHSPEEC", (75, 0, 3, 4))
        assert client.active == 7
        assert client.actions == []  # never played corrupted data
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_second_speech_skips_slow_slot_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    def info_queries(client: FakeClient, slot: int) -> int:
        return sum(
            1 for u, d in client.writes if u == protocol.GP_WRITE and d == bytes([0x73, slot])
        )

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        for text in (b"first phrase", b"second phrase"):
            await h.furby.play_temporary_dlcs([(text * 50, 0)], "FHSPEEC", (75, 0, 3, 4))
        assert info_queries(client, 7) == 1  # the official slot's name is cached
        assert client.active == 7
        # exactly one copy of our speech besides the official DLC, always in the same slot
        assert sorted(client.slots) == [7, 13]
        assert client.deleted == [12, 13]  # leftover, then the first phrase before the second
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_multi_part_speech_switches_official_dlc_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        parts = [(b"part one" * 40, 0.0), (b"part two" * 40, 0.0), (b"part three" * 40, 0.0)]
        loads_before = sum(1 for u, d in client.writes if d[:1] == b"\x60")
        await h.furby.play_temporary_dlcs(parts, "FHSPEEC", (75, 0, 3, 4))

        assert len(client.actions) == 3
        assert client.active == 7
        ours = [s for s in client.slots if s != 7]
        assert len(ours) == 1  # previous parts were deleted
        assert client.slots[ours[0]][1] == parts[-1][0]
        official_loads = [d for u, d in client.writes if d == bytes([0x60, 7])]
        assert len(official_loads) == 1  # restored once at the end, not between parts
        assert loads_before == 0
        # Same name as a stored DLC makes the Furby skip the data, and earlier parts stay
        # until the end (no deletes during playback), so every part gets its own name.
        uploads = [d[6:14] for u, d in client.writes if u == protocol.GP_WRITE and d[0] == 0x50]
        assert uploads == [b"FHSPEEC0", b"FHSPEEC1", b"FHSPEEC2"]
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_next_part_uploads_while_current_part_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        parts = [(b"one" * 99, 0.05), (b"two" * 99, 0.05)]
        await h.furby.play_temporary_dlcs(parts, "FHSPEEC", (75, 0, 3, 4))
        gp = [d for u, d in client.writes if u == protocol.GP_WRITE]
        first_action = next(i for i, d in enumerate(gp) if d[0] == 0x13)
        second_upload = [i for i, d in enumerate(gp) if d[0] == 0x50][1]
        first_stop = next(i for i, d in enumerate(gp) if d[0] == 0x62 and i > first_action)
        assert first_action < second_upload < first_stop  # uploaded during part 1
        assert len(client.actions) == 2 and client.active == 7
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_corrupted_upload_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        client.corrupt_next = 1
        data = b"speech" * 50
        await h.furby.play_temporary_dlcs([(data, 0)], "FHSPEEC", (75, 0, 3, 4))
        assert client.actions == [protocol.action(75, 0, 3, 4)]  # played after the retry
        assert client.active == 7
        ours = [s for s in client.slots if s != 7]
        assert len(ours) == 1 and client.slots[ours[0]][1] == data  # bad copy removed
        uploads = [d[6:14] for u, d in client.writes if u == protocol.GP_WRITE and d[0] == 0x50]
        assert len(uploads) == 2 and uploads[0] != uploads[1]  # retried under a new name
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_upload_with_existing_name_is_reported() -> None:
    async def scenario() -> None:
        h, runner, _client = await _furby_with_official_dlc()
        with pytest.raises(UploadError, match="probably on it already"):
            await h.furby.upload_dlc(bytes(200), "FHSPEECH.DLC")
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_link_drop_mid_speech_restores_official_dlc_on_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seen on hardware: the link dropped during a phrase, our DLC stayed active and the
    # Furby kept saying the phrase on every touch and in its idle chatter.
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        saved: list[str | None] = []
        restored = asyncio.Event()

        def on_pending(name: str | None) -> None:
            saved.append(name)
            if name is None:
                restored.set()

        h.furby._on_pending_restore = on_pending
        speech = asyncio.create_task(
            h.furby.play_temporary_dlcs([(b"speech" * 50, 0.2)], "FHSPEEC", (75, 0, 3, 4))
        )
        await h.wait_state(ConnectionState.CONNECTED)
        await asyncio.sleep(0.1)  # uploading and switching are instant here; now speaking
        assert client.actions and client.active != 7
        h.reachable.clear()  # the Furby crashes and stays away for a moment
        client.drop()
        with pytest.raises(NotConnectedError):
            await speech
        assert saved == ["TU021980"]  # persisted for the reconnect
        assert client.active != 7  # still stuck on our DLC

        h.reachable.set()
        async with asyncio.timeout(1):
            await restored.wait()  # post-connect restore runs in the background
        assert h.clients[-1].active == 7
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_never_deletes_while_speech_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hypothesis from hardware: erasing flash during playback glitches the audio.
    monkeypatch.setattr(furby_mod, "PLAY_MARGIN_S", 0)

    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        client.corrupt_next = 1  # a bad copy to clean up, too
        for text in (b"first phrase", b"second phrase"):
            await h.furby.play_temporary_dlcs([(text * 50, 0)], "FHSPEEC", (75, 0, 3, 4))
        gp = [d[0] for u, d in client.writes if u == protocol.GP_WRITE]
        playing = False
        for cmd in gp:
            if cmd == 0x13:
                playing = True
            elif cmd == 0x62:
                playing = False
            assert not (playing and cmd == 0x74), "deleted a slot during playback"
        assert 7 in client.slots and client.slots[7][0] == "TU021980"
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())


def test_never_deletes_other_dlcs_even_if_named_like_ours() -> None:
    async def scenario() -> None:
        h, runner, client = await _furby_with_official_dlc()
        await h.furby._delete_own({7}, "FHSPEEC")  # a bug passing the official slot
        assert client.deleted == [] and 7 in client.slots
        await h.furby.disconnect()
        await runner

    asyncio.run(scenario())

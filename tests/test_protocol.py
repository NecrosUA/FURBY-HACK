import pytest

from furbyhack import protocol


def test_keepalive() -> None:
    assert protocol.keepalive() == b"\x00"


def test_antenna() -> None:
    assert protocol.antenna(255, 0, 128) == bytes([0x14, 255, 0, 128])


def test_action() -> None:
    assert protocol.action(18, 0, 0, 1) == bytes([0x13, 0x00, 18, 0, 0, 1])


def test_lcd() -> None:
    assert protocol.lcd(True) == bytes([0xCD, 0x01])
    assert protocol.lcd(False) == bytes([0xCD, 0x00])


def test_nordic_disconnect() -> None:
    assert protocol.nordic_disconnect() == b"\x07"


@pytest.mark.parametrize("bad", [-1, 256])
def test_byte_range_is_checked(bad: int) -> None:
    with pytest.raises(ValueError):
        protocol.antenna(bad, 0, 0)
    with pytest.raises(ValueError):
        protocol.action(0, 0, 0, bad)


def test_uuids_share_fluff_base() -> None:
    suffix = protocol.FLUFF_SERVICE[8:]
    for uuid in (
        protocol.GP_WRITE,
        protocol.GP_LISTEN,
        protocol.NORDIC_WRITE,
        protocol.NORDIC_LISTEN,
    ):
        assert uuid.endswith(suffix)


def test_dlc_upload_start() -> None:
    cmd = protocol.dlc_upload_start(0x04C95A, "TU003410.DLC")
    assert cmd == bytes([0x50, 0x00, 0x04, 0xC9, 0x5A, 0x02]) + b"TU003410.DLC\0\0"


@pytest.mark.parametrize("name", ["short.dlc", "TOOLONGNAME1.DLC", "TU00341Ж.DLC"])
def test_dlc_upload_start_rejects_bad_name(name: str) -> None:
    with pytest.raises(ValueError):
        protocol.dlc_upload_start(100, name)


def test_dlc_slot_commands() -> None:
    assert protocol.dlc_load(3) == bytes([0x60, 3])
    assert protocol.dlc_activate() == bytes([0x61])
    assert protocol.dlc_deactivate(3) == bytes([0x62, 3])
    assert protocol.dlc_delete(3) == bytes([0x74, 3])
    with pytest.raises(ValueError):
        protocol.dlc_delete(14)


def test_parse_slots() -> None:
    # filled mask 0x0005 (slots 0, 2), active mask 0x0004 (slot 2)
    resp = bytes([0x72, 0, 0, 0x00, 0x05, 0, 0, 0x00, 0x04])
    slots = protocol.parse_slots(resp)
    assert len(slots) == protocol.DLC_SLOT_COUNT
    assert slots[:3] == [
        protocol.SlotState.FILLED,
        protocol.SlotState.EMPTY,
        protocol.SlotState.ACTIVE,
    ]


def test_parse_slot_info() -> None:
    resp = bytes([0x73, 2]) + b"TU003410" + bytes([0x04, 0xC9, 0x5A]) + bytes.fromhex("deadbeef")
    info = protocol.parse_slot_info(resp)
    assert info == protocol.SlotInfo("TU003410", 0x04C95A, 0xDEADBEEF)
    empty = bytes([0x73, 2]) + bytes(15)
    assert protocol.parse_slot_info(empty) is None


def test_parsers_reject_short_responses() -> None:
    with pytest.raises(ValueError):
        protocol.parse_slots(bytes(5))
    with pytest.raises(ValueError):
        protocol.parse_slot_info(bytes(10))

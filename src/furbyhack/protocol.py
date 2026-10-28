"""Furby Connect BLE protocol: UUIDs, command builders and response parsers.

Pure module, no I/O.
"""

import struct
import zlib
from dataclasses import dataclass
from enum import Enum, IntEnum

DEVICE_NAME = "Furby"

# Proprietary "Fluff" service
FLUFF_SERVICE = "dab91435-b5a1-e29c-b041-bcd562613bde"
GP_WRITE = "dab91383-b5a1-e29c-b041-bcd562613bde"  # GeneralPlus: movement, LCD, sound
GP_LISTEN = "dab91382-b5a1-e29c-b041-bcd562613bde"  # GeneralPlus notifications
NORDIC_WRITE = "dab90757-b5a1-e29c-b041-bcd562613bde"  # Nordic: BLE chip
NORDIC_LISTEN = "dab90756-b5a1-e29c-b041-bcd562613bde"  # Nordic notifications
FILE_WRITE = "dab90758-b5a1-e29c-b041-bcd562613bde"  # DLC upload data

# Without a keepalive at this interval Furby starts talking and moving on its own.
KEEPALIVE_INTERVAL_S = 3.0


def _check_byte(name: str, value: int) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be 0..255, got {value}")
    return value


# --- GeneralPlus commands (write to GP_WRITE) ---


def keepalive() -> bytes:
    return bytes([0x00])


def antenna(red: int, green: int, blue: int) -> bytes:
    return bytes(
        [0x14, _check_byte("red", red), _check_byte("green", green), _check_byte("blue", blue)]
    )


def action(input: int, index: int, subindex: int, specific: int) -> bytes:
    """Trigger a built-in action sequence."""
    return bytes(
        [
            0x13,
            0x00,
            _check_byte("input", input),
            _check_byte("index", index),
            _check_byte("subindex", subindex),
            _check_byte("specific", specific),
        ]
    )


def lcd(on: bool) -> bytes:
    """LCD (eyes) backlight."""
    return bytes([0xCD, 0x01 if on else 0x00])


# --- DLC slots and upload (GeneralPlus) ---

DLC_SLOT_COUNT = 14
DLC_FILENAME_LEN = 12  # e.g. "TU003410.DLC"
UPLOAD_CHUNK_SIZE = 20

# Response prefixes the Furby answers with
RESP_FILE_TRANSFER = 0x24
RESP_DLC_SLOTS = 0x72
RESP_DLC_SLOT_INFO = 0x73
RESP_DLC_DELETE = 0x74
RESP_DLC_LOAD_ACTIVATE = 0xDC  # answer to load / activate / deactivate


class FileTransferMode(IntEnum):
    """Second byte of a 0x24 notification during upload."""

    END_CURRENT_TRANSFER = 1
    READY_TO_RECEIVE = 2
    TIMEOUT = 3
    READY_TO_APPEND = 4
    RECEIVED_OK = 5
    RECEIVED_ERR = 6


class SlotState(Enum):
    EMPTY = "empty"
    FILLED = "filled"
    ACTIVE = "active"


@dataclass(frozen=True)
class SlotInfo:
    name: str
    length: int
    checksum: int


def dlc_upload_start(size: int, filename: str) -> bytes:
    """Announce an upload; Furby answers 0x24 READY_TO_RECEIVE."""
    if len(filename) != DLC_FILENAME_LEN or not filename.isascii():
        raise ValueError(f"filename must be {DLC_FILENAME_LEN} ASCII chars, got {filename!r}")
    if not 0 < size < 1 << 24:
        raise ValueError(f"size must fit in 3 bytes, got {size}")
    # 0x02 is taken as-is from both reference clients; its meaning is unknown.
    return bytes([0x50, 0x00]) + size.to_bytes(3, "big") + b"\x02" + filename.encode() + b"\0\0"


def dlc_slots_query() -> bytes:
    return bytes([0x72])


def dlc_slot_info_query(slot: int) -> bytes:
    return bytes([0x73, _check_slot(slot)])


def dlc_load(slot: int) -> bytes:
    return bytes([0x60, _check_slot(slot)])


def dlc_activate() -> bytes:
    """Activate the loaded DLC."""
    return bytes([0x61])


def dlc_deactivate(slot: int) -> bytes:
    return bytes([0x62, _check_slot(slot)])


def dlc_delete(slot: int) -> bytes:
    return bytes([0x74, _check_slot(slot)])


def _check_slot(slot: int) -> int:
    if not 0 <= slot < DLC_SLOT_COUNT:
        raise ValueError(f"slot must be 0..{DLC_SLOT_COUNT - 1}, got {slot}")
    return slot


def slot_checksum(raw: bytes) -> int:
    """Checksum Furby reports for a slot: Adler-32 of the whole file (verified on hardware).

    Only recomputed when the slot is loaded; before that it may be 0 or a stale value.
    """
    return zlib.adler32(raw)


def parse_slots(resp: bytes) -> list[SlotState]:
    """0x72 response: big-endian bitmasks of filled (bytes 3-4) and active (7-8) slots."""
    if len(resp) < 9:
        raise ValueError(f"slot list response too short: {resp.hex(' ')}")
    filled = int.from_bytes(resp[3:5], "big")
    active = int.from_bytes(resp[7:9], "big")
    states = []
    for i in range(DLC_SLOT_COUNT):
        if active & (1 << i):
            states.append(SlotState.ACTIVE)
        elif filled & (1 << i):
            states.append(SlotState.FILLED)
        else:
            states.append(SlotState.EMPTY)
    return states


def parse_slot_info(resp: bytes) -> SlotInfo | None:
    """0x73 response: name at 2..10, u24 length at 10..13, u32 checksum at 13..17. None if empty."""
    if len(resp) < 17:
        raise ValueError(f"slot info response too short: {resp.hex(' ')}")
    (length,) = struct.unpack_from(">I", resp, 9)
    length &= 0xFFFFFF
    if length == 0:
        return None
    name = resp[2:10].decode("ascii", errors="replace")
    (checksum,) = struct.unpack_from(">I", resp, 13)
    return SlotInfo(name, length, checksum)


# --- Nordic commands (write to NORDIC_WRITE) ---

NORDIC_PACKET_ACK = 0x09  # notification: byte 1 = packets buffered on Furby
NORDIC_PACKET_OVERLOAD = 0x0A  # notification: Furby dropped/overflowed


def nordic_packet_ack(enabled: bool) -> bytes:
    """Enable GotPacketAck notifications, used for upload flow control."""
    return bytes([0x09, 0x01 if enabled else 0x00, 0x00])


def nordic_disconnect() -> bytes:
    # TODO: verify on hardware whether this is needed for a clean exit
    # or whether a plain bleak disconnect is enough.
    return bytes([0x07])

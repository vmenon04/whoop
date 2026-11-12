"""The only commands this project may send to the band, and their reply layouts.

Safety rule: a command is sent only if it is in ALLOWED, and only with the
exact argument bytes listed there. There is no way to pass free arguments.
Adding a command here is a decision for the owner of the band, not a refactor.

Numbers and layouts: LeonardSEO/wearable5.0 (`protocol/whoop5_protocol.json`,
`re/probe_read_pointer.py`) and ryanbr/noop. Unverified on this band until the
spike has run; see the `whoop-ble` skill.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from whoop_lab.ble.framing import FrameError, build_frame, check_frame

PACKET_COMMAND = 35
PACKET_COMMAND_RESPONSE = 36
PACKET_REALTIME_DATA = 40
PACKET_REALTIME_RAW_DATA = 43
PACKET_HISTORICAL_DATA = 47
PACKET_EVENT = 48
PACKET_METADATA = 49
PACKET_CONSOLE_LOGS = 50

PACKET_NAMES = {
    35: "COMMAND",
    36: "COMMAND_RESPONSE",
    40: "REALTIME_DATA",
    43: "REALTIME_RAW_DATA",
    47: "HISTORICAL_DATA",
    48: "EVENT",
    49: "METADATA",
    50: "CONSOLE_LOGS",
    51: "REALTIME_IMU_DATA_STREAM",
    52: "HISTORICAL_IMU_DATA_STREAM",
}

META_HISTORY_START = 1
META_HISTORY_END = 2
META_HISTORY_COMPLETE = 3
META_NAMES = {1: "HISTORY_START", 2: "HISTORY_END", 3: "HISTORY_COMPLETE"}

RESULT_NAMES = {0: "FAILURE", 1: "SUCCESS", 2: "PENDING", 3: "UNSUPPORTED"}
RESULT_SUCCESS = 1
RESULT_PENDING = 2


class Command(IntEnum):
    ABORT_HISTORICAL_TRANSMITS = 20
    SEND_HISTORICAL_DATA = 22
    GET_BATTERY_LEVEL = 26
    GET_DATA_RANGE = 34
    GET_HELLO = 145


# Command -> the one argument string it may carry. All of these only read.
# SEND_HISTORICAL_DATA starts a transfer but consumes nothing: the band's trim
# position moves only on HISTORICAL_DATA_RESULT (23), which is deliberately absent.
ALLOWED: dict[Command, bytes] = {
    Command.GET_HELLO: b"\x01",
    Command.GET_BATTERY_LEVEL: b"\x00",
    Command.GET_DATA_RANGE: b"\x00",
    Command.SEND_HISTORICAL_DATA: b"\x00",
    Command.ABORT_HISTORICAL_TRANSMITS: b"\x00",
}

# Never sent by this project, whatever else changes: these can wipe, brick, lock or
# reconfigure a band. Listed so that a test can prove none of them is ever allowed.
NEVER: dict[int, str] = {
    25: "FORCE_TRIM",
    29: "REBOOT_STRAP",
    32: "POWER_CYCLE_STRAP",
    33: "SET_READ_POINTER",
    36: "START_FIRMWARE_LOAD",
    37: "LOAD_FIRMWARE_DATA",
    38: "PROCESS_FIRMWARE_IMAGE",
    39: "SET_LED_DRIVE",
    41: "SET_TIA_GAIN",
    43: "SET_BIAS_OFFSET",
    45: "ENTER_BLE_DFU",
    52: "SET_DP_TYPE",
    53: "FORCE_DP_TYPE",
    61: "SET_AFE_PARAMETERS",
    66: "SET_ALARM_TIME",
    77: "SET_ADVERTISING_NAME_HARVARD",
    83: "VERIFY_FIRMWARE_IMAGE",
    99: "RESET_FUEL_GAUGE",
    100: "CALIBRATE_CAPSENSE",
    119: "SET_DEVICE_CONFIG_VALUE",
    120: "SET_FF_VALUE",
    131: "SET_RESEARCH_PACKET",
    140: "SET_ADVERTISING_NAME",
    142: "START_FIRMWARE_LOAD_NEW",
    143: "LOAD_FIRMWARE_DATA_NEW",
    144: "PROCESS_FIRMWARE_IMAGE_NEW",
    146: "SET_CLOCK",
    155: "START_CERTIFICATE_TRANSFER",
    156: "LOAD_CERTIFICATE",
    157: "VERIFY_CERTIFICATE",
    158: "PROCESS_CERTIFICATE",
    159: "LOCK_DEVICE",
}

# Reading commands that raise battery drain or shrink the history buffer. Not allowed
# yet; each needs an explicit opt-in before it is added to ALLOWED.
OPT_IN_LATER: dict[int, str] = {
    81: "START_RAW_DATA",
    96: "ENTER_HIGH_FREQ_SYNC",
    105: "TOGGLE_IMU_MODE_HISTORICAL",
    106: "TOGGLE_IMU_MODE",
    107: "ENABLE_OPTICAL_DATA",
    108: "TOGGLE_OPTICAL_MODE",
    153: "TOGGLE_PERSISTENT_R20",
    154: "TOGGLE_PERSISTENT_R21",
}


class CommandNotAllowedError(Exception):
    pass


def build_allowed(command: int, sequence: int) -> bytes:
    """The frame for an allowed command. Raises for anything not in ALLOWED."""
    try:
        allowed = Command(command)
        args = ALLOWED[allowed]
    except (ValueError, KeyError):
        name = NEVER.get(command) or OPT_IN_LATER.get(command) or "unknown"
        raise CommandNotAllowedError(
            f"command {command} ({name}) is not on the allowlist"
        ) from None
    return build_frame(bytes([PACKET_COMMAND, sequence & 0xFF, allowed]) + args)


HISTORICAL_DATA_RESULT = 23
_END_TOKEN = slice(21, 29)


def build_history_ack(history_end_frame: bytes, sequence: int) -> bytes:
    """Acknowledge one history chunk by echoing the token of its own HISTORY_END frame.

    This MOVES THE BAND'S TRIM POSITION: the acknowledged records are gone for every
    other client, the WHOOP app included. Send it only after the chunk is safely on disk.

    The token can come from nowhere else. Command 23 with any other position rewinds
    or skips the band's history, which is why it is not in ALLOWED and why this
    function takes the band's frame and not a page number.
    """
    try:
        check_frame(history_end_frame)
    except FrameError as exc:
        raise CommandNotAllowedError(f"not a valid frame: {exc}") from None
    if metadata_type(history_end_frame) != META_HISTORY_END or len(history_end_frame) < 33:
        raise CommandNotAllowedError("an acknowledgement needs the band's HISTORY_END frame")
    token = history_end_frame[_END_TOKEN]
    return build_frame(
        bytes([PACKET_COMMAND, sequence & 0xFF, HISTORICAL_DATA_RESULT, 0x01]) + token
    )


# --- replies ------------------------------------------------------------------------------

_RESPONSE_BODY = 13  # frame offset of a command response's body


@dataclass(frozen=True)
class CommandResponse:
    command: int
    origin_sequence: int
    result: int
    body: bytes

    @property
    def result_name(self) -> str:
        return RESULT_NAMES.get(self.result, f"result {self.result}")


def parse_response(frame: bytes) -> CommandResponse | None:
    """`frame` as a command response, or None if it is another packet type."""
    if len(frame) < _RESPONSE_BODY + 4 or frame[8] != PACKET_COMMAND_RESPONSE:
        return None
    return CommandResponse(frame[10], frame[11], frame[12], frame[_RESPONSE_BODY:-4])


@dataclass(frozen=True)
class DataRange:
    """Where the band's history ring stands. Pages are 4 KB of flash."""

    read_page: int
    write_page: int
    trim_page: int
    write_wrap: int
    total_pages: int
    trim_clock: int
    newest_clock: int

    @property
    def pages_behind(self) -> int | None:
        """Pages written but not yet acknowledged by any client."""
        if not self.total_pages:
            return None
        return (self.write_page - self.trim_page) % self.total_pages


def parse_data_range(body: bytes) -> DataRange | None:
    """The final (SUCCESS) GET_DATA_RANGE body. None if it is the short PENDING reply."""
    if len(body) < 65:
        return None
    read_page, write_page, trim_page, write_wrap, total_pages = struct.unpack_from("<5I", body, 5)
    (trim_clock,) = struct.unpack_from("<I", body, 41)
    (newest_clock,) = struct.unpack_from("<I", body, 57)
    return DataRange(
        read_page, write_page, trim_page, write_wrap, total_pages, trim_clock, newest_clock
    )


@dataclass(frozen=True)
class Hello:
    battery_percent: float
    unix_s: int
    firmware: str | None


def parse_hello(body: bytes) -> Hello | None:
    if len(body) < 10:
        return None
    (battery_tenths,) = struct.unpack_from("<I", body, 1)
    (unix_s,) = struct.unpack_from("<I", body, 6)
    firmware = None
    if len(body) >= 98:
        major, minor, patch = body[91], body[92], body[93]
        (build,) = struct.unpack_from("<I", body, 94)
        firmware = f"{major}.{minor}.{patch}.{build}"
    return Hello(battery_tenths / 10.0, unix_s, firmware)


@dataclass(frozen=True)
class RecordHeader:
    """The header shared by historical (47) and realtime raw (43) data records."""

    packet_type: int
    layout: int
    record_index: int
    unix_s: int


def parse_record_header(frame: bytes) -> RecordHeader | None:
    if len(frame) < 25 or frame[8] not in (PACKET_HISTORICAL_DATA, PACKET_REALTIME_RAW_DATA):
        return None
    record_index, unix_s = struct.unpack_from("<II", frame, 11)
    return RecordHeader(frame[8], frame[9], record_index, unix_s)


def metadata_type(frame: bytes) -> int | None:
    """HISTORY_START / END / COMPLETE for a metadata frame, None for anything else."""
    if len(frame) < 15 or frame[8] != PACKET_METADATA:
        return None
    return frame[10]

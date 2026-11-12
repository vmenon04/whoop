"""BLE framing, the command allowlist and capture summaries. No device, no real frames:
every frame here is built by the code under test or typed out from public documentation."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from whoop_lab.ble import commands
from whoop_lab.ble.capture import (
    HEART_RATE_MEASUREMENT,
    CaptureWriter,
    parse_heart_rate_measurement,
    render,
    summarize,
)
from whoop_lab.ble.commands import (
    ALLOWED,
    NEVER,
    OPT_IN_LATER,
    Command,
    CommandNotAllowedError,
    DataRange,
    build_allowed,
    build_history_ack,
    build_trim_restore,
    metadata_type,
    parse_data_range,
    parse_hello,
    parse_record_header,
    parse_response,
)
from whoop_lab.ble.framing import (
    FrameError,
    Reassembler,
    build_frame,
    check_frame,
    crc16_modbus,
)

# Published by LeonardSEO/wearable5.0 and ryanbr/noop as the fixed first write of a session.
CLIENT_HELLO = bytes.fromhex("aa0108000001e67123019101363e5c8d")
DATA_CHARACTERISTIC = "fd4b0005-cce1-4033-93ce-002d5875f58a"


def record_frame(layout: int, index: int, unix_s: int, payload: int = 100) -> bytes:
    """A synthetic historical data record with the documented header and a filler payload."""
    body = bytes([47, layout, 0]) + struct.pack("<IIH", index, unix_s, 0) + bytes(payload)
    return build_frame(body, destination=1, source=0)


def metadata_frame(meta_type: int, token: bytes = bytes(8)) -> bytes:
    body = bytes([49, 0, meta_type]) + struct.pack("<IH", 1_790_000_000, 0) + bytes(4) + token
    return build_frame(body, destination=1, source=0)


def response_frame(command: int, result: int, body: bytes, origin_sequence: int = 1) -> bytes:
    return build_frame(
        bytes([36, 9, command, origin_sequence, result]) + body, destination=1, source=0
    )


# --- framing ------------------------------------------------------------------------------


def test_crc16_modbus_matches_the_standard_check_value() -> None:
    assert crc16_modbus(b"123456789") == 0x4B37


def test_builder_reproduces_the_published_client_hello_byte_for_byte() -> None:
    assert build_allowed(Command.GET_HELLO, sequence=1) == CLIENT_HELLO
    check_frame(CLIENT_HELLO)


def test_built_frames_validate_and_pad_to_four_bytes() -> None:
    for size in range(1, 12):
        frame = build_frame(bytes(range(size)))
        check_frame(frame)
        assert len(frame) % 4 == 0
        assert frame[8 : 8 + size] == bytes(range(size))


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda f: f[:10], "too short"),
        (lambda f: b"\xab" + f[1:], "does not start"),
        (lambda f: f + bytes(4), "declared length"),
        (lambda f: f[:4] + bytes([f[4] ^ 1]) + f[5:], "header checksum"),
        (lambda f: f[:9] + bytes([f[9] ^ 1]) + f[10:], "body checksum"),
    ],
)
def test_damaged_frames_are_rejected(damage: object, message: str) -> None:
    frame = record_frame(18, 1, 1_790_000_000)
    with pytest.raises(FrameError, match=message):
        check_frame(damage(frame))  # type: ignore[operator]


def test_reassembler_joins_fragments_of_any_size() -> None:
    frames = [record_frame(18, i, 1_790_000_000 + i, payload=97 + i) for i in range(6)]
    stream = b"".join(frames)
    for size in (1, 3, 20, 185, len(stream)):
        reassembler = Reassembler()
        out: list[bytes] = []
        for start in range(0, len(stream), size):
            out += reassembler.feed(stream[start : start + size])
        assert out == frames
        assert reassembler.discarded == 0
        assert reassembler.pending == 0


def test_reassembler_skips_garbage_and_counts_it() -> None:
    good = record_frame(18, 1, 1_790_000_000)
    damaged = bytearray(record_frame(18, 2, 1_790_000_001))
    damaged[30] ^= 0xFF
    reassembler = Reassembler()
    out = reassembler.feed(b"\x00\x11\xaa" + good + bytes(damaged) + good)
    assert out == [good, good]
    assert reassembler.discarded == 3 + len(damaged)


def test_reassembler_keeps_a_marker_split_across_notifications() -> None:
    good = record_frame(18, 1, 1_790_000_000)
    reassembler = Reassembler()
    assert reassembler.feed(good[:1]) == []
    assert reassembler.feed(good[1:]) == [good]


# --- the command allowlist ----------------------------------------------------------------


def test_only_the_five_reading_commands_are_allowed() -> None:
    assert {int(command) for command in ALLOWED} == {20, 22, 26, 34, 145}


@pytest.mark.parametrize("command", sorted(NEVER))
def test_destructive_and_configuration_commands_are_refused(command: int) -> None:
    with pytest.raises(CommandNotAllowedError, match=NEVER[command]):
        build_allowed(command, sequence=1)


@pytest.mark.parametrize("command", sorted(OPT_IN_LATER))
def test_battery_draining_streams_are_refused_until_opted_in(command: int) -> None:
    with pytest.raises(CommandNotAllowedError):
        build_allowed(command, sequence=1)


def test_every_command_number_outside_the_allowlist_is_refused() -> None:
    allowed = {int(command) for command in ALLOWED}
    for command in range(256):
        if command in allowed:
            check_frame(build_allowed(command, sequence=7))
        else:
            with pytest.raises(CommandNotAllowedError):
                build_allowed(command, sequence=7)


def test_the_history_acknowledgement_cannot_be_built_from_a_number() -> None:
    """Command 23 moves the band's trim position. It exists only as build_history_ack,
    which echoes the band's own token and cannot name an arbitrary position."""
    with pytest.raises(CommandNotAllowedError):
        build_allowed(23, sequence=1)


def test_acknowledgement_echoes_the_token_of_a_real_history_end_frame() -> None:
    token = bytes.fromhex("41b6010010000000")
    ack = build_history_ack(metadata_frame(2, token), sequence=9)
    check_frame(ack)
    assert ack[8:12] == bytes([35, 9, 23, 1])
    assert ack[12:20] == token
    assert len(ack) == 24


@pytest.mark.parametrize(
    "frame",
    [
        metadata_frame(1),  # HISTORY_START
        metadata_frame(3),  # HISTORY_COMPLETE
        record_frame(18, 1, 1_790_000_000),
        metadata_frame(2)[:-1] + b"\x00",  # damaged
        b"",
    ],
)
def test_acknowledgement_is_refused_without_a_valid_history_end_frame(frame: bytes) -> None:
    with pytest.raises(CommandNotAllowedError):
        build_history_ack(frame, sequence=1)


def test_restore_names_the_trim_page_and_wrap_the_band_reported() -> None:
    original = DataRange(5145, 5229, 5143, 0, 131072, 1_790_000_000, 1_790_001_000)
    frame = build_trim_restore(original, sequence=4)
    check_frame(frame)
    assert frame[8:12] == bytes([35, 4, 23, 1])
    assert struct.unpack_from("<II", frame, 12) == (5143, 0)
    # After the ring has wrapped, a trim page above the write head is one pass older.
    wrapped = DataRange(0, 100, 130000, 7, 131072, 0, 0)
    assert struct.unpack_from("<II", build_trim_restore(wrapped, 1), 12) == (130000, 6)
    behind = DataRange(0, 9000, 8000, 7, 131072, 0, 0)
    assert struct.unpack_from("<II", build_trim_restore(behind, 1), 12) == (8000, 7)


@pytest.mark.parametrize(
    "unusable",
    [
        DataRange(0, 100, 50, 0, 0, 0, 0),  # no ring size
        DataRange(0, 100, 200000, 3, 131072, 0, 0),  # page outside the ring
        DataRange(0, 100, 500, 0, 131072, 0, 0),  # above the write head on the first pass
    ],
)
def test_restore_refuses_a_range_it_cannot_trust(unusable: DataRange) -> None:
    with pytest.raises(CommandNotAllowedError):
        build_trim_restore(unusable, sequence=1)


def test_no_list_overlaps_another() -> None:
    allowed = {int(command) for command in ALLOWED}
    assert not allowed & set(NEVER)
    assert not allowed & set(OPT_IN_LATER)
    assert not set(NEVER) & set(OPT_IN_LATER)


def test_allowed_commands_carry_only_their_fixed_argument() -> None:
    frame = build_allowed(Command.SEND_HISTORICAL_DATA, sequence=5)
    assert frame[8:12] == bytes([35, 5, 22, 0])
    assert len(frame) == 16
    assert not hasattr(commands, "build_command")  # no path that takes free arguments


# --- replies ------------------------------------------------------------------------------


def test_data_range_reply_is_parsed_and_the_pending_reply_is_not() -> None:
    body = bytearray(65)
    body[0] = 1
    struct.pack_into("<5I", body, 5, 2654, 3064, 2654 - 10, 7, 131072)
    struct.pack_into("<I", body, 41, 1_790_000_000)
    struct.pack_into("<I", body, 57, 1_790_005_000)
    response = parse_response(response_frame(34, 1, bytes(body)))
    assert response is not None
    assert (response.command, response.result_name) == (34, "SUCCESS")
    data_range = parse_data_range(response.body)
    assert data_range is not None
    assert (data_range.write_page, data_range.trim_page) == (3064, 2644)
    assert data_range.total_pages == 131072
    assert data_range.pages_behind == 420
    assert data_range.newest_clock == 1_790_005_000

    pending = parse_response(response_frame(34, 2, b""))
    assert pending is not None
    assert pending.result_name == "PENDING"
    assert parse_data_range(pending.body) is None


def test_pages_behind_wraps_around_the_ring() -> None:
    body = bytearray(65)
    struct.pack_into("<5I", body, 5, 0, 5, 131070, 8, 131072)
    data_range = parse_data_range(bytes(body))
    assert data_range is not None
    assert data_range.pages_behind == 7


def test_hello_reply_gives_battery_clock_and_firmware() -> None:
    body = bytearray(110)
    struct.pack_into("<I", body, 1, 873)
    struct.pack_into("<I", body, 6, 1_790_000_000)
    body[91:94] = bytes([50, 43, 2])
    hello = parse_hello(bytes(body))
    assert hello is not None
    assert hello.battery_percent == pytest.approx(87.3)
    assert hello.unix_s == 1_790_000_000
    assert hello.firmware == "50.43.2.0"
    short = parse_hello(bytes(body[:20]))
    assert short is not None
    assert short.firmware is None


def test_record_and_metadata_headers() -> None:
    header = parse_record_header(record_frame(18, 42, 1_790_000_123))
    assert header is not None
    assert (header.packet_type, header.layout, header.record_index, header.unix_s) == (
        47,
        18,
        42,
        1_790_000_123,
    )
    assert parse_record_header(metadata_frame(2)) is None
    assert metadata_type(metadata_frame(2)) == 2
    assert metadata_type(record_frame(18, 1, 1_790_000_000)) is None
    assert parse_response(record_frame(18, 1, 1_790_000_000)) is None


# --- captures -----------------------------------------------------------------------------


def test_standard_heart_rate_measurement_with_rr_intervals() -> None:
    measurement = parse_heart_rate_measurement(bytes([0x10, 58]) + struct.pack("<HH", 1034, 1021))
    assert measurement is not None
    assert measurement.heart_rate == 58
    assert measurement.rr_raw == (1034, 1021)
    wide = parse_heart_rate_measurement(bytes([0x01]) + struct.pack("<H", 301))
    assert wide is not None
    assert wide.heart_rate == 301
    assert parse_heart_rate_measurement(b"\x00") is None


def test_capture_round_trip_and_summary(tmp_path: Path) -> None:
    writer = CaptureWriter(tmp_path / "run")
    writer.write("tx", "fd4b0002", build_allowed(Command.SEND_HISTORICAL_DATA, 2))
    frames = [record_frame(18, i, 1_790_000_000 + i) for i in range(61)]
    frames += [record_frame(21, 100, 1_790_000_030, payload=1220), metadata_frame(2)]
    stream = b"".join(frames)
    for start in range(0, len(stream), 180):  # split as notifications would be
        writer.write("rx", DATA_CHARACTERISTIC, stream[start : start + 180])
    writer.write("rx", HEART_RATE_MEASUREMENT, bytes([0x10, 61]) + struct.pack("<H", 980))
    writer.write("rx", DATA_CHARACTERISTIC, b"\x01\x02\x03")  # noise
    writer.close()

    lines = [json.loads(line) for line in writer.path.read_text().splitlines()]
    assert {line["dir"] for line in lines} == {"rx", "tx"}
    assert (
        bytes.fromhex("".join(x["hex"] for x in lines if x["char"] == DATA_CHARACTERISTIC))[
            : len(stream)
        ]
        == stream
    )  # the raw bytes survive exactly

    summary = summarize(writer.path)
    assert summary.sent == 1
    assert summary.frames == 63
    assert summary.by_packet_type == {47: 62, 49: 1}
    assert summary.metadata == {2: 1}
    assert summary.discarded_bytes == 3
    biometric = summary.records[(47, 18)]
    assert biometric.count == 61
    assert biometric.span_seconds == 60
    assert biometric.records_per_second == pytest.approx(1.0)
    assert summary.records[(47, 21)].records_per_second is None  # one record says nothing
    assert (summary.heart_rate_min, summary.heart_rate_max, summary.rr_intervals) == (61, 61, 1)

    text = render(summary)
    assert "type 47 layout 18: 61 records" in text
    assert "1.00 records/s" in text
    assert "metadata HISTORY_END: 1" in text
    assert "bytes that fit no valid frame: 3" in text

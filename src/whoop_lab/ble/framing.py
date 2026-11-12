"""The WHOOP 5.0 "format 1" frame: checksums, building, validation and reassembly.

    [0]    0xAA         start of frame
    [1]    0x01         format
    [2:4]  u16 LE       declared length = whole frame length - 8
    [4]    u8           destination (0 in requests to the band)
    [5]    u8           source      (1 in requests to the band)
    [6:8]  u16 LE       CRC16-Modbus over bytes [0, 6)
    [8:-4]              body, zero-padded to a multiple of 4
    [-4:]  u32 LE       CRC32 (zlib) over the body

The layout is as documented by LeonardSEO/wearable5.0 and ryanbr/noop. This is
an independent implementation; `tests/test_ble_framing.py` checks it against
the client-hello bytes those projects publish.

Stdlib only, and nothing here talks to a device.
"""

from __future__ import annotations

import struct
import zlib

START_OF_FRAME = 0xAA
FORMAT_1 = 0x01
HEADER_LENGTH = 8
TRAILER_LENGTH = 4
MIN_FRAME_LENGTH = 16
# The largest record documented is 2,140 bytes. Anything claiming far more is not a frame.
MAX_FRAME_LENGTH = 4096

_MARKER = bytes([START_OF_FRAME, FORMAT_1])


class FrameError(ValueError):
    """The bytes are not one complete, checksum-valid frame."""


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_frame(body: bytes, *, destination: int = 0x00, source: int = 0x01) -> bytes:
    """Wrap `body` in a frame, padding it with zeros to a multiple of 4."""
    padded = body + bytes(-len(body) % 4)
    header = _MARKER + struct.pack("<HBB", len(padded) + TRAILER_LENGTH, destination, source)
    header += struct.pack("<H", crc16_modbus(header))
    return header + padded + struct.pack("<I", zlib.crc32(padded))


def check_frame(raw: bytes) -> None:
    """Raise FrameError unless `raw` is exactly one valid frame."""
    if len(raw) < MIN_FRAME_LENGTH:
        raise FrameError("too short")
    if raw[:2] != _MARKER:
        raise FrameError("does not start with AA 01")
    (declared,) = struct.unpack_from("<H", raw, 2)
    if declared + HEADER_LENGTH != len(raw):
        raise FrameError(f"declared length {declared} does not match {len(raw)} bytes")
    if (declared - TRAILER_LENGTH) % 4:
        raise FrameError("body is not a multiple of 4 bytes")
    if crc16_modbus(raw[:6]) != struct.unpack_from("<H", raw, 6)[0]:
        raise FrameError("header checksum mismatch")
    if (
        zlib.crc32(raw[HEADER_LENGTH:-TRAILER_LENGTH])
        != struct.unpack_from("<I", raw, len(raw) - 4)[0]
    ):
        raise FrameError("body checksum mismatch")


class Reassembler:
    """Joins the notifications of ONE characteristic into complete frames.

    A large record spans many notifications and only its first fragment starts
    with AA 01. Bytes that cannot begin a valid frame are skipped one at a time
    and counted in `discarded`, so damage costs as little as possible and never
    goes unnoticed.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.discarded = 0

    @property
    def pending(self) -> int:
        """Bytes held back waiting for the rest of a frame."""
        return len(self._buffer)

    def _drop(self, count: int) -> None:
        del self._buffer[:count]
        self.discarded += count

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(_MARKER)
            if start < 0:
                # Keep a final AA: the 01 may be the first byte of the next notification.
                keep = 1 if self._buffer[-1:] == _MARKER[:1] else 0
                self._drop(len(self._buffer) - keep)
                return frames
            self._drop(start)
            if len(self._buffer) < 4:
                return frames
            total = struct.unpack_from("<H", self._buffer, 2)[0] + HEADER_LENGTH
            if not MIN_FRAME_LENGTH <= total <= MAX_FRAME_LENGTH:
                self._drop(1)
                continue
            if len(self._buffer) < total:
                return frames
            candidate = bytes(self._buffer[:total])
            try:
                check_frame(candidate)
            except FrameError:
                self._drop(1)
                continue
            frames.append(candidate)
            del self._buffer[:total]

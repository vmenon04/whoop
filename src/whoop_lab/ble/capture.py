"""Raw capture files and a summary of what they hold.

A capture is a JSON-lines file with one line per Bluetooth notification received
and per command sent, written before any reassembly or decoding:

    {"t": <unix seconds>, "dir": "rx" | "tx", "char": "<uuid>", "hex": "<bytes>"}

Captures contain biometric data and device identifiers. They live under
`captures/`, which is git-ignored, and are never committed.
"""

from __future__ import annotations

import json
import struct
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from whoop_lab.ble.commands import (
    META_NAMES,
    PACKET_NAMES,
    metadata_type,
    parse_record_header,
)
from whoop_lab.ble.framing import Reassembler

HEART_RATE_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
NOTIFICATIONS_FILE = "notifications.jsonl"

# A record timestamp outside this range is a parsing mistake, not a time.
_PLAUSIBLE_UNIX = (1_600_000_000, 2_200_000_000)


class CaptureWriter:
    """Appends notifications to disk, flushing each line so a crash loses nothing."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / NOTIFICATIONS_FILE
        self._file: IO[str] = self.path.open("a", encoding="utf-8")
        self.received = 0
        self.received_bytes = 0

    def write(self, direction: str, characteristic: str, data: bytes) -> None:
        line = {"t": time.time(), "dir": direction, "char": characteristic, "hex": data.hex()}
        self._file.write(json.dumps(line) + "\n")
        self._file.flush()
        if direction == "rx":
            self.received += 1
            self.received_bytes += len(data)

    def close(self) -> None:
        self._file.close()


def read_capture(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


@dataclass(frozen=True)
class HeartRateMeasurement:
    heart_rate: int
    rr_raw: tuple[int, ...]  # as sent; reported to be milliseconds on this band, not 1/1024 s


def parse_heart_rate_measurement(data: bytes) -> HeartRateMeasurement | None:
    """The standard Bluetooth Heart Rate Measurement (0x2A37) layout."""
    if len(data) < 2:
        return None
    flags, offset = data[0], 1
    if flags & 0x01:
        if len(data) < 3:
            return None
        (heart_rate,) = struct.unpack_from("<H", data, offset)
        offset += 2
    else:
        heart_rate = data[offset]
        offset += 1
    if flags & 0x08:  # energy expended
        offset += 2
    intervals: list[int] = []
    if flags & 0x10:
        while offset + 2 <= len(data):
            intervals.append(struct.unpack_from("<H", data, offset)[0])
            offset += 2
    return HeartRateMeasurement(heart_rate, tuple(intervals))


@dataclass
class RecordStats:
    """One kind of data record: a packet type and a layout number."""

    count: int = 0
    total_bytes: int = 0
    lengths: Counter[int] = field(default_factory=Counter)
    first_unix: int | None = None
    last_unix: int | None = None
    first_index: int | None = None
    last_index: int | None = None

    def add(self, length: int, unix_s: int, record_index: int) -> None:
        self.count += 1
        self.total_bytes += length
        self.lengths[length] += 1
        if _PLAUSIBLE_UNIX[0] <= unix_s <= _PLAUSIBLE_UNIX[1]:
            self.first_unix = unix_s if self.first_unix is None else min(self.first_unix, unix_s)
            self.last_unix = unix_s if self.last_unix is None else max(self.last_unix, unix_s)
        self.first_index = (
            record_index if self.first_index is None else min(self.first_index, record_index)
        )
        self.last_index = (
            record_index if self.last_index is None else max(self.last_index, record_index)
        )

    @property
    def span_seconds(self) -> int | None:
        if self.first_unix is None or self.last_unix is None:
            return None
        return self.last_unix - self.first_unix

    @property
    def records_per_second(self) -> float | None:
        """Records per second of band time. None when the span is too short to say."""
        span = self.span_seconds
        if span is None or span < 10 or self.count < 2:
            return None
        return (self.count - 1) / span


@dataclass
class Summary:
    notifications: int = 0
    notification_bytes: int = 0
    sent: int = 0
    first_received: float | None = None
    last_received: float | None = None
    by_characteristic: Counter[str] = field(default_factory=Counter)
    frames: int = 0
    frame_bytes: int = 0
    by_packet_type: Counter[int] = field(default_factory=Counter)
    records: dict[tuple[int, int], RecordStats] = field(default_factory=dict)
    metadata: Counter[int] = field(default_factory=Counter)
    discarded_bytes: int = 0
    pending_bytes: int = 0
    heart_rate_notifications: int = 0
    heart_rate_min: int | None = None
    heart_rate_max: int | None = None
    rr_intervals: int = 0

    @property
    def wall_seconds(self) -> float | None:
        if self.first_received is None or self.last_received is None:
            return None
        return self.last_received - self.first_received


def summarize(path: Path) -> Summary:
    """Reassemble a capture and count what came back. Decodes headers only."""
    summary = Summary()
    streams: dict[str, Reassembler] = {}
    for line in read_capture(path):
        data = bytes.fromhex(line["hex"])
        if line["dir"] != "rx":
            summary.sent += 1
            continue
        characteristic = str(line["char"]).lower()
        summary.notifications += 1
        summary.notification_bytes += len(data)
        summary.by_characteristic[characteristic] += 1
        received = float(line["t"])
        if summary.first_received is None:
            summary.first_received = received
        summary.last_received = received

        if characteristic == HEART_RATE_MEASUREMENT:
            measurement = parse_heart_rate_measurement(data)
            if measurement is not None:
                summary.heart_rate_notifications += 1
                summary.rr_intervals += len(measurement.rr_raw)
                rate = measurement.heart_rate
                low, high = summary.heart_rate_min, summary.heart_rate_max
                summary.heart_rate_min = rate if low is None else min(low, rate)
                summary.heart_rate_max = rate if high is None else max(high, rate)
            continue

        for frame in streams.setdefault(characteristic, Reassembler()).feed(data):
            summary.frames += 1
            summary.frame_bytes += len(frame)
            summary.by_packet_type[frame[8]] += 1
            header = parse_record_header(frame)
            if header is not None:
                stats = summary.records.setdefault(
                    (header.packet_type, header.layout), RecordStats()
                )
                stats.add(len(frame), header.unix_s, header.record_index)
            meta = metadata_type(frame)
            if meta is not None:
                summary.metadata[meta] += 1
    summary.discarded_bytes = sum(stream.discarded for stream in streams.values())
    summary.pending_bytes = sum(stream.pending for stream in streams.values())
    return summary


def render(summary: Summary) -> str:
    """The summary as plain text. Counts and sizes only: no biometric values except the
    heart-rate range, which is there to show the stream is real."""
    lines = [
        f"notifications received: {summary.notifications} ({summary.notification_bytes} bytes)",
        f"commands sent: {summary.sent}",
    ]
    if summary.wall_seconds is not None:
        lines.append(f"capture lasted: {summary.wall_seconds:.1f} s")
    for characteristic, count in sorted(summary.by_characteristic.items()):
        lines.append(f"  {characteristic}: {count}")
    lines.append(f"complete frames: {summary.frames} ({summary.frame_bytes} bytes)")
    lines.append(
        f"bytes that fit no valid frame: {summary.discarded_bytes}; "
        f"left waiting for more: {summary.pending_bytes}"
    )
    for packet_type, count in sorted(summary.by_packet_type.items()):
        name = PACKET_NAMES.get(packet_type, "unknown")
        lines.append(f"  packet type {packet_type} {name}: {count}")
    for meta, count in sorted(summary.metadata.items()):
        lines.append(f"  metadata {META_NAMES.get(meta, meta)}: {count}")
    if summary.records:
        lines.append("data records by packet type and layout:")
    for (packet_type, layout), stats in sorted(summary.records.items()):
        sizes = ", ".join(f"{length} B x{n}" for length, n in sorted(stats.lengths.items()))
        text = f"  type {packet_type} layout {layout}: {stats.count} records, {sizes}"
        if stats.span_seconds is not None:
            text += f"; band time span {stats.span_seconds} s"
        rate = stats.records_per_second
        if rate is not None:
            text += f"; {rate:.2f} records/s"
        lines.append(text)
    if summary.heart_rate_notifications:
        lines.append(
            f"standard heart rate: {summary.heart_rate_notifications} notifications, "
            f"{summary.heart_rate_min}-{summary.heart_rate_max} bpm, "
            f"{summary.rr_intervals} R-R intervals"
        )
    return "\n".join(lines)

"""Decoding of band frames, kept separate from capture so it can be re-run.

Raw frames are the record of truth. Anything decoded from them carries
DECODER_VERSION, so a better decoder can reprocess old frames and the two sets
of results can be told apart.

The decoder itself is vendored (see `vendor/whoop5/VENDORED.md`). How far each
field can be trusted is in the `whoop-ble` skill.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whoop_lab.ble.capture import read_capture
from whoop_lab.ble.commands import PACKET_HISTORICAL_DATA
from whoop_lab.ble.framing import Reassembler
from whoop_lab.ble.vendor.whoop5 import FrameError, decode_frame

# Upstream commit of the vendored decoder. Change it whenever the vendored files change.
DECODER_VERSION = "whoop5@4361d069"

LAYOUT_BIOMETRIC = 18


def decode(frame: bytes) -> dict[str, Any] | None:
    """Every field the decoder knows for one complete frame, or None if it is not valid."""
    try:
        decoded: dict[str, Any] = decode_frame(frame)
    except FrameError:
        return None
    return decoded


@dataclass(frozen=True)
class Biometric:
    """One per-second history record (layout 18), reduced to what the metrics use."""

    unix_s: int
    record_index: int
    heart_rate: int  # 0 when the band has no reading
    rr_ms: tuple[int, ...]  # milliseconds, as sent
    band_state: str | None  # WAKE, STILL, SLEEP or UP
    skin_temp_c: float | None
    spo2_percent: int | None  # byte 82 when it holds 70..100; see the skill before trusting it


def biometric(decoded: dict[str, Any]) -> Biometric | None:
    """A decoded frame as a Biometric, or None if it is not a layout-18 record."""
    if decoded.get("layout") != LAYOUT_BIOMETRIC or decoded.get("unix_s") is None:
        return None
    skin = decoded.get("skin_temp_c")
    spo2 = decoded.get("spo2_percent")
    return Biometric(
        unix_s=int(decoded["unix_s"]),
        record_index=int(decoded["record_index"]),
        heart_rate=int(decoded.get("heart_rate") or 0),
        rr_ms=tuple(int(v) for v in decoded.get("rr_ms") or ()),
        band_state=decoded.get("band_state"),
        skin_temp_c=float(skin) if skin is not None else None,
        spo2_percent=int(spo2) if spo2 is not None else None,
    )


def load_biometrics(capture_files: Iterable[Path]) -> list[Biometric]:
    """Every layout-18 record in the given captures, oldest first, each one once.

    The same history can be drained more than once (a re-read, or an unacknowledged
    chunk sent again), so records are keyed on the band's own time and record index.
    """
    records: dict[tuple[int, int], Biometric] = {}
    for path in capture_files:
        streams: dict[str, Reassembler] = {}
        for line in read_capture(path):
            if line["dir"] != "rx":
                continue
            stream = streams.setdefault(str(line["char"]), Reassembler())
            for frame in stream.feed(bytes.fromhex(line["hex"])):
                if frame[8] != PACKET_HISTORICAL_DATA or frame[9] != LAYOUT_BIOMETRIC:
                    continue
                decoded = decode(frame)
                record = biometric(decoded) if decoded is not None else None
                if record is not None:
                    records[(record.unix_s, record.record_index)] = record
    return sorted(records.values(), key=lambda r: (r.unix_s, r.record_index))

"""Decoding of band frames, kept separate from capture so it can be re-run.

Raw frames are the record of truth. Anything decoded from them carries
DECODER_VERSION, so a better decoder can reprocess old frames and the two sets
of results can be told apart.

The decoder itself is vendored (see `vendor/whoop5/VENDORED.md`). How far each
field can be trusted is in the `whoop-ble` skill.
"""

from __future__ import annotations

from typing import Any

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

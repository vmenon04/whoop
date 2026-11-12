"""Reference decoder for the WHOOP 5.0 / MG Bluetooth LE protocol (format-1 envelope).

Vendored from LeonardSEO/wearable5.0 (MIT). See VENDORED.md next to this file.
"""
from .decode import decode_frame, pages_behind, rr_ticks_to_ms, schema
from .framing import Frame, FrameError, Reassembler, crc16_modbus, crc32, validate

__version__ = "0.1.0"
__all__ = ["decode_frame", "pages_behind", "rr_ticks_to_ms", "schema", "Frame", "FrameError",
           "Reassembler", "crc16_modbus", "crc32", "validate"]

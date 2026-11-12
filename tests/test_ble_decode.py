"""The vendored decoder, exercised only on synthetic frames."""

from __future__ import annotations

from whoop_lab.ble import decode as ble_decode
from whoop_lab.ble.commands import parse_record_header
from whoop_lab.ble.framing import check_frame
from whoop_lab.ble.vendor import whoop5
from whoop_lab.ble.vendor.whoop5 import decode as vendored_decode
from whoop_lab.ble.vendor.whoop5 import framing as vendored_framing
from whoop_lab.ble.vendor.whoop5 import synth


def test_biometric_record_decodes_to_named_fields() -> None:
    frame = synth.r18(hr=58, rr_ticks=(1024, 980), skin_raw=3412)
    check_frame(frame)  # our framing and the vendored one agree on what a valid frame is
    decoded = ble_decode.decode(frame)
    assert decoded is not None
    assert decoded["layout"] == ble_decode.LAYOUT_BIOMETRIC
    assert decoded["heart_rate"] == 58
    assert decoded["rr_ms"] == [1024, 980]  # milliseconds, as sent
    assert decoded["skin_temp_c"] == 34.12
    header = parse_record_header(frame)
    assert header is not None
    assert (header.record_index, header.unix_s) == (decoded["record_index"], decoded["unix_s"])


def test_damaged_frame_decodes_to_nothing() -> None:
    frame = bytearray(synth.r18())
    frame[40] ^= 0xFF
    assert ble_decode.decode(bytes(frame)) is None


def test_decoder_version_names_the_vendored_commit() -> None:
    assert ble_decode.DECODER_VERSION.startswith("whoop5@")


def test_vendored_code_cannot_build_commands() -> None:
    """Only whoop_lab.ble.commands may build a frame to send. The upstream builders
    were removed when the decoder was vendored; an update must remove them again."""
    for module in (whoop5, vendored_framing, vendored_decode):
        assert not hasattr(module, "build_command")
        assert not hasattr(module, "history_ack")

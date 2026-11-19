"""Phase 1 spike: talk to a WHOOP 5.0 over Bluetooth LE and save every byte it sends.

Run from the repository root. bleak is in the optional `ble` dependency group, so the
server image does not carry it:

    uv run --group ble python scripts/ble_spike.py scan
    uv run --group ble python scripts/ble_spike.py hr --seconds 120
    uv run --group ble python scripts/ble_spike.py status
    uv run --group ble python scripts/ble_spike.py peek
    uv run --group ble python scripts/ble_spike.py drain
    uv run --group ble python scripts/ble_spike.py report captures/<run>

Stages, in order of how much they touch the band:

    scan    listen for the band's advertisement. Sends nothing.
    hr      connect WITHOUT pairing, list the band's services, record the standard
            heart-rate stream. Sends nothing.
    status  pair, say hello, read battery and the history range twice.
    peek    request stored history and record the first burst. It is NOT acknowledged,
            so the band's sync position does not move and the WHOOP app loses nothing.
            The range is read before and after to prove that.

Every notification is written to captures/<run>/notifications.jsonl before anything
else happens to it. That directory is git-ignored: it holds biometric data.

    drain   read ALL stored history, acknowledging each chunk once it is on disk.
            This CONSUMES it: the band's sync position moves, and the WHOOP app will
            never receive or score those hours. It asks before starting.
            With --restore the position is put back afterwards and checked, so the
            WHOOP app uploads the same history when it next syncs.
            With --reread-hours N it first moves the position back about N hours, to
            read history the app has already synced, and always restores afterwards.
    restore put the position back for an earlier drain that could not (lost connection).

Only the commands in whoop_lab.ble.commands.ALLOWED can be sent, plus the history
acknowledgement, which can only echo the token the band itself just sent.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from whoop_lab.ble.capture import (
    HEART_RATE_MEASUREMENT,
    NOTIFICATIONS_FILE,
    CaptureWriter,
    parse_heart_rate_measurement,
    render,
    summarize,
)
from whoop_lab.ble.commands import (
    META_HISTORY_COMPLETE,
    META_HISTORY_END,
    META_NAMES,
    RESULT_PENDING,
    Command,
    CommandNotAllowedError,
    CommandResponse,
    DataRange,
    build_allowed,
    build_history_ack,
    build_trim_restore,
    build_trim_rewind,
    metadata_type,
    parse_data_range,
    parse_hello,
    parse_response,
    rewind_page,
)
from whoop_lab.ble.framing import Reassembler

SERVICE = "fd4b0001-cce1-4033-93ce-002d5875f58a"
COMMAND_CHARACTERISTIC = "fd4b0002-cce1-4033-93ce-002d5875f58a"
NOTIFY_CHARACTERISTICS = (
    "fd4b0003-cce1-4033-93ce-002d5875f58a",  # command responses
    "fd4b0004-cce1-4033-93ce-002d5875f58a",  # events
    "fd4b0005-cce1-4033-93ce-002d5875f58a",  # data
)
# Device Information Service strings worth knowing. The serial number is read but only
# its prefix is printed.
DEVICE_INFORMATION = {
    "manufacturer": "00002a29-0000-1000-8000-00805f9b34fb",
    "model": "00002a24-0000-1000-8000-00805f9b34fb",
    "serial": "00002a25-0000-1000-8000-00805f9b34fb",
    "hardware": "00002a27-0000-1000-8000-00805f9b34fb",
    "firmware": "00002a26-0000-1000-8000-00805f9b34fb",
    "software": "00002a28-0000-1000-8000-00805f9b34fb",
}

NOT_FOUND = """\
No WHOOP band found. It only advertises when no other device is connected to it.
  1. On the phone: force-quit the WHOOP app and turn Bluetooth off.
  2. Tap the band firmly several times until its light flashes blue.
  3. Run this again straight away."""

PERMISSION_HELP = """\
Bluetooth did not answer. macOS is probably waiting for permission, or refused it.
  Open System Settings, Privacy & Security, Bluetooth, and switch on the terminal
  app you are running this from. Then quit and reopen that terminal and try again."""

PAIRING_HELP = """\
The band did not let this Mac read its data channels. It holds one bond at a time.
  1. On the phone: force-quit the WHOOP app and turn Bluetooth off.
  2. On the Mac: System Settings, Bluetooth, and forget any WHOOP entry.
  3. Tap the band until it flashes blue, run this again, and accept the pairing prompt."""


def say(text: str = "") -> None:
    print(text, flush=True)


def stamp(unix_s: int) -> str:
    if not 1_600_000_000 <= unix_s <= 2_200_000_000:
        return f"no usable time ({unix_s})"
    return datetime.fromtimestamp(unix_s, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def new_capture_directory(stage: str) -> Path:
    name = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S") + f"-{stage}"
    return Path("captures") / name


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_whoop(device: Any, advertisement: Any) -> bool:
    uuids = [str(u).lower() for u in (advertisement.service_uuids or [])]
    name = (advertisement.local_name or device.name or "").upper()
    return SERVICE in uuids or name.startswith("WHOOP")


async def find_band(scan_seconds: float) -> Any:
    from bleak import BleakScanner

    say(f"Scanning for up to {scan_seconds:.0f} s ...")
    device = await BleakScanner.find_device_by_filter(is_whoop, timeout=scan_seconds)
    if device is None:
        say(NOT_FOUND)
        return None
    say(f"Found {device.name or 'unnamed band'}")
    return device


class Session:
    """One connection: records everything received and sends only allowed commands."""

    def __init__(self, client: Any, writer: CaptureWriter) -> None:
        self.client = client
        self.writer = writer
        self.sequence = 0
        self.streams = {uuid: Reassembler() for uuid in NOTIFY_CHARACTERISTICS}
        self.responses: list[CommandResponse] = []
        self.metadata: list[int] = []
        self.end_frames: list[bytes] = []
        self.chunk_ended = asyncio.Event()
        self.frames = 0

    def on_notification(self, characteristic: Any, data: bytearray) -> None:
        uuid = str(characteristic.uuid).lower()
        raw = bytes(data)
        self.writer.write("rx", uuid, raw)  # to disk first, whatever happens next
        stream = self.streams.get(uuid)
        if stream is None:
            return
        for frame in stream.feed(raw):
            self.frames += 1
            response = parse_response(frame)
            if response is not None:
                self.responses.append(response)
            meta = metadata_type(frame)
            if meta is not None:
                self.metadata.append(meta)
                if meta == META_HISTORY_END:
                    self.end_frames.append(frame)
                    self.chunk_ended.set()

    async def subscribe(self) -> None:
        for uuid in NOTIFY_CHARACTERISTICS:
            await self.client.start_notify(uuid, self.on_notification)

    async def send(self, command: Command) -> int:
        self.sequence = (self.sequence + 1) & 0xFF
        frame = build_allowed(command, self.sequence)  # raises unless on the allowlist
        self.writer.write("tx", COMMAND_CHARACTERISTIC, frame)
        await self.client.write_gatt_char(COMMAND_CHARACTERISTIC, frame, response=True)
        return self.sequence

    async def acknowledge(self, history_end_frame: bytes) -> None:
        """Tell the band a chunk is safely stored. The band then forgets it."""
        self.sequence = (self.sequence + 1) & 0xFF
        await self.write(build_history_ack(history_end_frame, self.sequence))

    async def write(self, frame: bytes) -> None:
        self.writer.write("tx", COMMAND_CHARACTERISTIC, frame)
        await self.client.write_gatt_char(COMMAND_CHARACTERISTIC, frame, response=True)

    async def start_transfer(self) -> bool:
        """Start a history transfer and wait for its first HISTORY_END. A trim move is
        only accepted at that point. Returns False if the band never serves a chunk."""
        for attempt in range(1, 5):
            self.chunk_ended.clear()
            await self.send(Command.SEND_HISTORICAL_DATA)
            try:
                await asyncio.wait_for(self.chunk_ended.wait(), timeout=20)
            except TimeoutError:
                # Nothing unsynced to serve yet. The band writes a page every 15 s or so.
                await self.send(Command.ABORT_HISTORICAL_TRANSMITS)
                say(f"  attempt {attempt}: the band has no chunk to serve yet, waiting 20 s")
                await asyncio.sleep(20)
                continue
            return True
        return False

    async def rewind(self, current: DataRange, pages_back: int) -> bool:
        """Move trim back so already-synced history is served again."""
        if not await self.start_transfer():
            return False
        self.sequence = (self.sequence + 1) & 0xFF
        await self.write(build_trim_rewind(current, pages_back, self.sequence))
        return True

    async def restore_trim(self, original: DataRange) -> bool:
        """Put trim back to `original` and check it. The band only accepts this during
        a transfer, after a HISTORY_END, so one is started first."""
        for attempt in range(1, 4):
            if not await self.start_transfer():
                return False
            self.sequence = (self.sequence + 1) & 0xFF
            await self.write(build_trim_restore(original, self.sequence))
            await asyncio.sleep(1)
            await self.send(Command.ABORT_HISTORICAL_TRANSMITS)
            await asyncio.sleep(1.5)
            after = await self.data_range("after restore")
            if after is not None and after.trim_page == original.trim_page:
                return True
            say(f"  restore attempt {attempt}: trim is not back yet")
        return False

    async def ask(
        self, command: Command, *, wait_seconds: float = 8.0, min_body: int = 0
    ) -> CommandResponse | None:
        """Send and wait for the reply. `min_body` skips a short PENDING reply."""
        seen = len(self.responses)
        await self.send(command)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            for response in self.responses[seen:]:
                # Several commands answer twice: PENDING first, then the real reply.
                final = response.result != RESULT_PENDING
                if response.command == command and final and len(response.body) >= min_body:
                    return response
            await asyncio.sleep(0.05)
        return None

    async def hello(self) -> None:
        response = await self.ask(Command.GET_HELLO)
        if response is None:
            say("hello: no reply")
            return
        hello = parse_hello(response.body)
        say(f"hello: {response.result_name}, reply body {len(response.body)} bytes")
        if hello is not None:
            say(f"  battery {hello.battery_percent:.1f}%, band clock {stamp(hello.unix_s)}")
            say(f"  firmware {hello.firmware or 'not in this reply'}")

    async def data_range(self, label: str) -> DataRange | None:
        response = await self.ask(Command.GET_DATA_RANGE, min_body=65)
        if response is None:
            say(f"history range ({label}): no complete reply")
            return None
        data_range = parse_data_range(response.body)
        if data_range is None:
            return None
        say(
            f"history range ({label}): write page {data_range.write_page}, "
            f"trim page {data_range.trim_page}, wrap {data_range.write_wrap}, "
            f"ring {data_range.total_pages} pages"
        )
        say(
            f"  unsynced backlog {data_range.pages_behind} pages; "
            f"newest page written {stamp(data_range.newest_clock)}; "
            f"trim at {stamp(data_range.trim_clock)}"
        )
        return data_range


STALE_BOND_HELP = """\
This Mac still holds a bond the band has since deleted: re-pairing the WHOOP app replaces
  it, since the band keeps only one. macOS will not pair again on its own, and nothing here
  can remove a bond for it.
  1. On the Mac: System Settings, Bluetooth, WHOOP, Forget This Device.
  2. Run this again (with the band flashing blue if it asks)."""


class StaleBondError(Exception):
    """macOS refused to connect because the band discarded this Mac's bond."""


async def connect(device: Any, connect_seconds: float) -> Any:
    from bleak import BleakClient

    client = BleakClient(device, timeout=connect_seconds)
    try:
        await client.connect()
    except Exception as exc:
        # CBErrorDomain code 14, CBErrorPeerRemovedPairingInformation. Retrying can't help.
        if "Code=14" in str(exc) or "Peer removed pairing information" in str(exc):
            raise StaleBondError from exc
        say(f"Could not connect: {type(exc).__name__}: {exc}")
        return None
    say("Connected")
    return client


def describe_services(client: Any) -> list[dict[str, Any]]:
    table = []
    for service in client.services:
        for characteristic in service.characteristics:
            table.append(
                {
                    "service": str(service.uuid),
                    "characteristic": str(characteristic.uuid),
                    "properties": list(characteristic.properties),
                }
            )
    return table


async def read_device_information(client: Any) -> dict[str, str]:
    info: dict[str, str] = {}
    for name, uuid in DEVICE_INFORMATION.items():
        try:
            value = await asyncio.wait_for(client.read_gatt_char(uuid), timeout=10)
        except Exception as exc:
            info[name] = f"unreadable ({type(exc).__name__})"
            continue
        info[name] = bytes(value).decode("utf-8", errors="replace").strip("\x00 ")
    return info


# --- stages -------------------------------------------------------------------------------


async def stage_scan(args: argparse.Namespace) -> int:
    from bleak import BleakScanner

    say(f"Scanning for {args.timeout:.0f} s ...")
    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)
    bands = [(d, adv) for d, adv in found.values() if is_whoop(d, adv)]
    if not bands:
        say(NOT_FOUND)
        return 2
    for device, advertisement in bands:
        offers = SERVICE in [str(u).lower() for u in (advertisement.service_uuids or [])]
        say(
            f"{advertisement.local_name or device.name}: signal {advertisement.rssi} dBm, "
            f"advertises the WHOOP 5 service: {'yes' if offers else 'no'}"
        )
    return 0


async def stage_hr(args: argparse.Namespace) -> int:
    device = await find_band(args.timeout)
    if device is None:
        return 2
    try:
        client = await connect(device, args.timeout)
    except StaleBondError:
        say(STALE_BOND_HELP)
        return 3
    if client is None:
        return 3
    directory = new_capture_directory("hr")
    writer = CaptureWriter(directory)
    try:
        write_json(directory / "gatt.json", describe_services(client))
        info = await read_device_information(client)
        write_json(directory / "device.json", info)
        for name, value in info.items():
            shown = value[:3] + "..." if name == "serial" and len(value) > 3 else value
            say(f"  {name}: {shown}")

        latest: dict[str, Any] = {}

        def on_heart_rate(characteristic: Any, data: bytearray) -> None:
            raw = bytes(data)
            writer.write("rx", str(characteristic.uuid).lower(), raw)
            measurement = parse_heart_rate_measurement(raw)
            if measurement is not None:
                latest["hr"] = measurement.heart_rate
                latest["rr"] = latest.get("rr", 0) + len(measurement.rr_raw)

        await client.start_notify(HEART_RATE_MEASUREMENT, on_heart_rate)
        say(f"Recording the standard heart-rate stream for {args.seconds} s (no pairing) ...")
        for elapsed in range(0, args.seconds, 10):
            await asyncio.sleep(min(10, args.seconds - elapsed))
            say(
                f"  {min(elapsed + 10, args.seconds):>4} s: {writer.received} notifications, "
                f"latest {latest.get('hr', '?')} bpm, {latest.get('rr', 0)} R-R intervals so far"
            )
        await client.stop_notify(HEART_RATE_MEASUREMENT)
    finally:
        await client.disconnect()
        writer.close()
    return finish(directory)


PAIRING_MODE = """\
The data channels need a bond, and the band only accepts one in pairing mode.
  1. Phone: WHOOP app force-quit, Bluetooth off.
  2. Tap the band firmly and repeatedly until its light flashes blue.
Press Enter as soon as it is flashing ..."""


async def open_session(args: argparse.Namespace, stage: str) -> tuple[Any, Session, Path] | None:
    """Scan, connect and subscribe, trying a few times: the band drops a connection
    that asks for its data channels unless it is in pairing mode."""
    if not args.no_prompt:
        await asyncio.to_thread(input, PAIRING_MODE)
    for attempt in range(1, args.attempts + 1):
        say(f"Attempt {attempt} of {args.attempts}")
        device = await find_band(args.timeout)
        if device is None:
            return None
        try:
            client = await connect(device, args.timeout)
        except StaleBondError:
            say(STALE_BOND_HELP)
            return None
        if client is None:
            await asyncio.sleep(2)
            continue
        directory = new_capture_directory(stage)
        writer = CaptureWriter(directory)
        session = Session(client, writer)
        say("Subscribing to the data channels. Accept the pairing prompt if macOS shows one ...")
        try:
            await asyncio.wait_for(session.subscribe(), timeout=args.pair_timeout)
        except Exception as exc:
            say(f"Could not subscribe: {type(exc).__name__}: {exc}")
            writer.close()
            writer.path.unlink(missing_ok=True)
            directory.rmdir()
            try:
                await client.disconnect()
            except Exception as disconnect_error:
                say(f"  (already disconnected: {type(disconnect_error).__name__})")
            await asyncio.sleep(2)
            continue
        write_json(directory / "gatt.json", describe_services(client))
        return client, session, directory
    say(PAIRING_HELP)
    return None


async def stage_status(args: argparse.Namespace) -> int:
    opened = await open_session(args, "status")
    if opened is None:
        return 3
    client, session, directory = opened
    try:
        await session.hello()
        battery = await session.ask(Command.GET_BATTERY_LEVEL)
        if battery is None:
            say("battery: no reply")
        else:
            say(f"battery: {battery.result_name}, reply body {battery.body.hex()}")
        first = await session.data_range("first")
        started = time.monotonic()
        say(f"Waiting {args.gap} s to measure how fast the band writes history ...")
        await asyncio.sleep(args.gap)
        second = await session.data_range("second")
        elapsed = time.monotonic() - started
        if first is not None and second is not None and first.total_pages:
            pages = (second.write_page - first.write_page) % first.total_pages
            per_hour = pages / elapsed * 3600
            say(f"  {pages} pages written in {elapsed:.0f} s, about {per_hour:.0f} pages an hour")
            if per_hour > 0:
                megabytes_per_day = per_hour * 24 * 4096 / 1e6
                days = first.total_pages / (per_hour * 24)
                say(
                    f"  at 4 KB a page: about {megabytes_per_day:.0f} MB a day, "
                    f"ring holds about {days:.1f} days (rough: one short sample)"
                )
    finally:
        await client.disconnect()
        session.writer.close()
    return finish(directory)


async def stage_peek(args: argparse.Namespace) -> int:
    opened = await open_session(args, "peek")
    if opened is None:
        return 3
    client, session, directory = opened
    try:
        await session.hello()
        before = await session.data_range("before")
        if before is None:
            say("No history range, so not requesting history.")
            return 4
        say("Requesting history. The first burst is recorded and NOT acknowledged ...")
        seen = len(session.metadata)
        await session.send(Command.SEND_HISTORICAL_DATA)
        deadline = time.monotonic() + args.burst_timeout
        ended = None
        while time.monotonic() < deadline and ended is None:
            await asyncio.sleep(0.1)
            for meta in session.metadata[seen:]:
                if meta in (META_HISTORY_END, META_HISTORY_COMPLETE):
                    ended = meta
        say(
            f"Burst ended with {META_NAMES.get(ended, 'a timeout') if ended else 'a timeout'}; "
            f"{session.frames} frames so far"
        )
        await session.send(Command.ABORT_HISTORICAL_TRANSMITS)
        await asyncio.sleep(1.5)
        after = await session.data_range("after")
        if after is not None:
            if after.trim_page == before.trim_page:
                say("Trim page unchanged: nothing was consumed, the WHOOP app loses nothing.")
            else:
                say(
                    f"WARNING: trim page moved from {before.trim_page} to {after.trim_page}. "
                    "Stop here and report this."
                )
    finally:
        await client.disconnect()
        session.writer.close()
    return finish(directory)


async def run_stage(stage: Any, args: argparse.Namespace) -> int:
    """Run a stage, but first make sure Bluetooth answers at all: without permission
    macOS leaves the first scan hanging for ever with no message."""
    from bleak import BleakScanner

    try:
        await asyncio.wait_for(BleakScanner.discover(timeout=1.0), timeout=20)
    except TimeoutError:
        say(PERMISSION_HELP)
        return 5
    except Exception as exc:
        say(f"Bluetooth is not available: {type(exc).__name__}: {exc}")
        say(PERMISSION_HELP)
        return 5
    result: int = await stage(args)
    return result


RESTORE_OK = """\
Trim is back at page {page}. Now turn the phone's Bluetooth on, re-pair the band in
the WHOOP app, and let it sync: it will upload this same history to WHOOP."""

RESTORE_FAILED = """\
WARNING: the band's sync position could NOT be put back to page {page}.
The capture is safe on disk, but WHOOP will not receive this history unless a later
restore works. Do not open the WHOOP app yet. Try:
    uv run --group ble python scripts/ble_spike.py restore {directory}
and tell me what it prints."""

RANGE_FILE = "range_before.json"
# Deliberately above the measured 200 to 250, so a re-read starts a little early.
REREAD_PAGES_PER_HOUR = 260


async def finish_restore(session: Session, original: DataRange, directory: Path) -> None:
    say("Putting the band's sync position back ...")
    if await session.restore_trim(original):
        say(RESTORE_OK.format(page=original.trim_page))
    else:
        say(RESTORE_FAILED.format(page=original.trim_page, directory=directory))


async def stage_restore(args: argparse.Namespace) -> int:
    source = Path(args.directory) / RANGE_FILE
    original = DataRange(**json.loads(source.read_text(encoding="utf-8")))
    opened = await open_session(args, "restore")
    if opened is None:
        return 3
    client, session, directory = opened
    try:
        await session.hello()
        now = await session.data_range("now")
        if now is not None and now.trim_page == original.trim_page:
            say("Trim is already at the recorded page. Nothing to do.")
        else:
            await finish_restore(session, original, Path(args.directory))
    finally:
        if client.is_connected:
            await client.disconnect()
        session.writer.close()
    return finish(directory)


REREAD_WARNING = """\
This moves the band's sync position BACK about {hours:g} hours, reads forward from
there to now, then puts the position back exactly where it started and checks it.
It re-reads history the WHOOP app already has; the app loses nothing if the final
restore succeeds, and receives some history twice if it does not.
The method is documented by one project, on this band's firmware (50.43.2.0).
Type 'reread' to go ahead: """

DRAIN_WARNING = """\
This reads all history the band has not yet synced and acknowledges it.
Acknowledged history is removed from the band's queue: the WHOOP app will never
receive it, so WHOOP will show no data, scores or API records for those hours.
Each chunk is written to disk before it is acknowledged.
With --restore the position is put back afterwards, so the app still gets it all.
Type 'drain' to go ahead: """


async def stage_drain(args: argparse.Namespace) -> int:
    if args.reread_hours:
        # Moving the band's position backwards is never skipped with --yes.
        answer = await asyncio.to_thread(input, REREAD_WARNING.format(hours=args.reread_hours))
        if answer.strip().lower() != "reread":
            say("Not re-reading.")
            return 0
    elif not args.yes:
        answer = await asyncio.to_thread(input, DRAIN_WARNING)
        if answer.strip().lower() != "drain":
            say("Not draining.")
            return 0
    opened = await open_session(args, "drain")
    if opened is None:
        return 3
    client, session, directory = opened
    writer = session.writer
    try:
        await session.hello()
        before = await session.data_range("before")
        if before is None:
            say("No history range, so not draining.")
            return 4
        write_json(directory / RANGE_FILE, dataclasses.asdict(before))
        started = time.monotonic()
        chunks = handled = 0
        if args.reread_hours:
            pages_back = int(args.reread_hours * REREAD_PAGES_PER_HOUR)
            try:
                page, wrap = rewind_page(before, pages_back)
            except CommandNotAllowedError as exc:
                say(f"Cannot re-read: {exc}")
                return 4
            say(f"Rewinding the sync position to page {page} (wrap {wrap}) ...")
            if not await session.rewind(before, pages_back):
                say("The band served no chunk, so the position was not moved. Nothing changed.")
                return 4
            handled = len(session.end_frames)  # the chunk we rode in on is not acknowledged
        else:
            await session.send(Command.SEND_HISTORICAL_DATA)
        meta_seen = len(session.metadata)
        seen_received, last_activity = writer.received, time.monotonic()
        outcome = "the band reporting HISTORY_COMPLETE"
        while META_HISTORY_COMPLETE not in session.metadata[meta_seen:]:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            if writer.received != seen_received:
                seen_received, last_activity = writer.received, now
            if len(session.end_frames) > handled:
                end_frame = session.end_frames[handled]
                handled += 1
                writer.sync()  # on disk first: after the acknowledgement the band forgets it
                await session.acknowledge(end_frame)
                chunks += 1
                if chunks % 100 == 0:
                    megabytes = writer.received_bytes / 1e6
                    say(f"  {chunks} chunks, {megabytes:.1f} MB, {now - started:.0f} s")
                if args.max_chunks and chunks >= args.max_chunks:
                    outcome = f"the --max-chunks limit of {args.max_chunks}"
                    await session.send(Command.ABORT_HISTORICAL_TRANSMITS)
                    break
            elif not client.is_connected:
                outcome = "the band disconnecting (unacknowledged data will be sent again)"
                break
            elif now - last_activity > args.idle_timeout:
                outcome = f"{args.idle_timeout:.0f} s of silence"
                await session.send(Command.ABORT_HISTORICAL_TRANSMITS)
                break
        writer.sync()
        elapsed = time.monotonic() - started
        say(f"Drain ended with {outcome}: {chunks} chunks acknowledged in {elapsed:.0f} s")
        if client.is_connected:
            await asyncio.sleep(1.5)
            after = await session.data_range("after")
            if after is not None and before.total_pages:
                moved = (after.trim_page - before.trim_page) % before.total_pages
                say(
                    f"Trim moved forward {moved} pages, {moved * 4096 / 1e6:.1f} MB of band storage"
                )
            if args.restore or args.reread_hours:
                await finish_restore(session, before, directory)
            else:
                say("No --restore: WHOOP will not receive this history.")
        elif args.restore or args.reread_hours:
            say(RESTORE_FAILED.format(page=before.trim_page, directory=directory))
    finally:
        if client.is_connected:
            await client.disconnect()
        writer.close()
    return finish(directory)


def finish(directory: Path) -> int:
    say()
    say(f"Saved to {directory}/")
    say(render(summarize(directory / NOTIFICATIONS_FILE)))
    return 0


def stage_report(args: argparse.Namespace) -> int:
    path = Path(args.directory)
    if path.is_dir():
        path = path / NOTIFICATIONS_FILE
    say(render(summarize(path)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    stages = parser.add_subparsers(dest="stage", required=True)

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        stage = stages.add_parser(name, help=summary)
        stage.add_argument("--timeout", type=float, default=30.0, help="scan and connect timeout")
        return stage

    add("scan", "look for the band; sends nothing")
    hr = add("hr", "unpaired heart-rate stream; sends nothing")
    hr.add_argument("--seconds", type=int, default=120)
    status = add("status", "pair, hello, battery, history range twice")
    status.add_argument("--gap", type=int, default=180, help="seconds between range readings")
    peek = add("peek", "record the first history burst without acknowledging it")
    peek.add_argument("--burst-timeout", type=float, default=45.0)
    drain = add("drain", "read and CONSUME all unsynced history")
    drain.add_argument("--max-chunks", type=int, default=0, help="stop after this many (0: all)")
    drain.add_argument("--idle-timeout", type=float, default=30.0)
    drain.add_argument("--yes", action="store_true", help="skip the confirmation")
    drain.add_argument(
        "--restore",
        action="store_true",
        help="afterwards put the sync position back so the WHOOP app still gets everything",
    )
    drain.add_argument(
        "--reread-hours",
        type=float,
        default=0.0,
        help="first move the sync position back about this many hours, to read history the "
        "WHOOP app has already synced; the position is always restored afterwards",
    )
    restore = add("restore", "put the sync position back for an earlier drain")
    restore.add_argument("directory", help="the drain's capture directory")
    for stage in (status, peek, drain, restore):
        stage.add_argument("--pair-timeout", type=float, default=90.0)
        stage.add_argument("--attempts", type=int, default=3)
        stage.add_argument("--no-prompt", action="store_true", help="do not wait for Enter")
    report = stages.add_parser("report", help="summarise a saved capture")
    report.add_argument("directory")

    args = parser.parse_args()
    if args.stage == "report":
        return stage_report(args)
    runner = {
        "scan": stage_scan,
        "hr": stage_hr,
        "status": stage_status,
        "peek": stage_peek,
        "drain": stage_drain,
        "restore": stage_restore,
    }
    try:
        import bleak  # noqa: F401
    except ImportError:
        say("bleak is not installed. Run: uv run --group ble python scripts/ble_spike.py ...")
        return 1
    try:
        return asyncio.run(run_stage(runner[args.stage], args))
    except KeyboardInterrupt:
        say("Stopped. Whatever was received is already on disk.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

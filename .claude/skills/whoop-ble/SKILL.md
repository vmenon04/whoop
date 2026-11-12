---
name: whoop-ble
description: Facts and hard safety rules for talking to the WHOOP 5.0 band directly over Bluetooth LE. Use when writing or changing anything under src/whoop_lab/ble, scripts/ble_spike.py, the collector, BLE storage or decoders, or when deciding which commands may be sent to the band.
---

# WHOOP 5.0 over Bluetooth LE

The protocol is reverse-engineered by third parties. Nothing here comes from WHOOP. Every fact carries one of these labels:

- **BAND**: confirmed on the owner's own band by this project. Nothing has this label yet.
- **SOURCES AGREE**: at least two independent projects say the same thing.
- **ONE SOURCE**: a single project, often a single band and firmware.
- **DISPUTED**: the projects disagree. Never pick a side by guessing; show the owner.

When the band contradicts this file, the band wins: update the file and say so.

## Safety rules, never relaxed

1. Send only commands in `whoop_lab.ble.commands.ALLOWED`, with exactly the argument bytes listed there. `build_allowed()` is the only way to build a command frame. Do not add a function that takes free arguments.
2. Adding a command to `ALLOWED` is the band owner's decision. Ask first, every time, and say what the command changes on the band.
3. Never send anything in `NEVER`: firmware, reboot, power cycle, trim, read pointer, fuel gauge, calibration, certificates, lock, clock, alarms, advertising name, LED or analog front-end settings, feature flags, device configuration.
4. If a command's effect is not documented by a source, it is unknown. Do not send it. Ask.
5. Streams that raise battery drain or shrink the history buffer are in `OPT_IN_LATER` and stay off until the owner opts in.
6. Captured frames are biometric data. They live in `captures/` (git-ignored) and are never committed. Tests use synthetic frames built by the code under test.
7. `tests/test_ble_framing.py` proves every command number outside the allowlist is refused. Keep that test.

## One bond, one consumer (SOURCES AGREE)

- The band holds one encrypted bond and serves one central at a time. It advertises only when nothing is connected.
- Each stored record goes to whichever client acknowledges it first. `HISTORICAL_DATA_RESULT` (23) moves the band's **trim** position, and the WHOOP app syncs from trim.
- So a normal drain by this project means WHOOP's cloud never receives those hours, and the API side of this project gets gaps.
- Without an acknowledgement the band stops after the first burst and trim does not move. That is what `ble_spike.py peek` relies on, and it reads the range before and after to check.
- ONE SOURCE (wearable5.0, one band, firmware 50.43.2): an acknowledgement carrying an older position, sent during a transfer, moves trim back, so a drain can be undone and the app then uploads everything. Not allowed here until the owner decides.
- DISPUTED: whether pairing a new central removes the phone's bond. wearable5.0 and noop say the WHOOP app must be re-paired; whoop-vault says not.

## GATT (SOURCES AGREE)

| Role | UUID |
|---|---|
| Service | `fd4b0001-cce1-4033-93ce-002d5875f58a` |
| Command write | `fd4b0002-…` |
| Command responses | `fd4b0003-…` |
| Events | `fd4b0004-…` |
| Data | `fd4b0005-…` |
| Memfault diagnostics | `fd4b0007-…` (not subscribed) |
| Standard heart rate | `0x2A37`, readable without a bond |

A neighbouring service `11500001-6215-11ee-8c99-0242ac120002` belongs to the battery pack and uses different framing.

## Frame (SOURCES AGREE)

`AA 01`, u16 length (frame length minus 8), destination, source, CRC16-Modbus of the first 6 bytes, body padded to a multiple of 4, CRC32 of the body. Implemented in `ble/framing.py`. The first write of a session is the fixed hello `aa0108000001e67123019101363e5c8d`, and `build_allowed(GET_HELLO, 1)` must reproduce it exactly.

- Body starts `[packet type][sequence][command or layout]`.
- Command response (type 36): command at frame offset 10, origin sequence 11, result 12 (0 failure, 1 success, 2 pending, 3 unsupported), body from 13.
- Data record (types 43 and 47): layout at 9, record index u32 at 11, band time u32 at 15. Layout plus record index plus band time is the natural idempotency key.
- Metadata (type 49): type at 10 (1 start, 2 end, 3 complete); `HISTORY_END` carries an 8-byte token at 21.
- A record can span many notifications; only the first fragment starts with `AA 01`. Reassemble per characteristic.

## Commands in use

| Number | Name | Argument | Effect |
|---|---|---|---|
| 145 | `GET_HELLO` | `01` | Opens the session; reply has battery, clock, firmware |
| 26 | `GET_BATTERY_LEVEL` | `00` | Reads battery |
| 34 | `GET_DATA_RANGE` | `00` | Reads ring pointers. Two replies: a short PENDING, then 65 bytes |
| 22 | `SEND_HISTORICAL_DATA` | `00` | Starts a history transfer from trim. Consumes nothing by itself |
| 20 | `ABORT_HISTORICAL_TRANSMITS` | `00` | Stops the transfer |

whoop-vault documents 22 with no argument and wearable5.0 with `00`; after zero-padding both are the same bytes.

## What is on the wire (labels as noted)

- **History ring** (SOURCES AGREE on structure, ONE SOURCE on numbers): 131,072 pages of 4 KB; about 245 pages an hour in normal use, so roughly 24 MB a day and about three weeks of buffer.
- **R18** (layout 18, one per second): heart rate, R-R intervals, gravity, steps, skin temperature, band state (wake, still, sleep, up). SOURCES AGREE.
- **R-R unit is milliseconds**, in R18 and in `0x2A37`, not the 1/1024 s the Bluetooth standard specifies. SOURCES AGREE (early noop builds had it wrong).
- **SpO2**: R18 byte 82 during 30-second windows every 20 minutes of sleep. DISPUTED: wearable5.0 calls it proven on three bands; noop keeps it uninterpreted because of two contrary nights.
- **R20** optical blocks, **R21** 100 Hz six-axis IMU, **R26** compact optical bursts (about 40 s every 19 minutes at 25 Hz). Structure decoded; LED wavelengths, gyro scale and units open.
- **Raw capture costs buffer** (ONE SOURCE): persistent raw records write about 3,600 pages an hour, cutting the ring to about 36 hours, and turning them on needs configuration writes that rule 3 forbids.
- **No scores on the wire** (SOURCES AGREE): recovery, strain and sleep stages are computed on WHOOP's servers. Respiratory rate is not sent either; it has to be derived from R-R.

## Reference projects

| Project | License | Use |
|---|---|---|
| LeonardSEO/wearable5.0 | MIT | Schema and decoders with evidence per fact. Vendor `python/whoop5` at a pinned commit, with its LICENSE, when decoding starts |
| Sophonbot0/whoop-vault | MIT | Linux and BlueZ pairing sequence only. It ships a command fuzzer and alarm writes; do not copy its command code |
| ryanbr/noop, vishk23/noop | PolyForm Noncommercial | Protocol facts only (its license says the facts are free to reuse). Copy no code |

## Platform notes

- macOS: bleak cannot pair explicitly. The system pairs when an encrypted characteristic is first used, and may show a prompt. The terminal needs Bluetooth permission, and without it the first scan hangs silently; `ble_spike.py` checks for that. Device addresses are per-Mac UUIDs.
- Linux: needs BlueZ and an adapter (the server has neither). whoop-vault's working order is: agent `NoInputNoOutput`, keep `scan on` running, then `trust`, `connect`, `pair`. DISPUTED minimum BlueZ version: 5.66 or 5.85.

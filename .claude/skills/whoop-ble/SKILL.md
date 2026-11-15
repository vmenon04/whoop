---
name: whoop-ble
description: Facts and hard safety rules for talking to the WHOOP 5.0 band directly over Bluetooth LE. Use when writing or changing anything under src/whoop_lab/ble, scripts/ble_spike.py, the collector, BLE storage or decoders, or when deciding which commands may be sent to the band.
---

# WHOOP 5.0 over Bluetooth LE

The protocol is reverse-engineered by third parties. Nothing here comes from WHOOP. Every fact carries one of these labels:

- **BAND**: confirmed on the owner's own band by this project (model 5.0, hardware `WG50_r45`, firmware 50.43.2.0), with the date.
- **SOURCES AGREE**: at least two independent projects say the same thing.
- **ONE SOURCE**: a single project, often a single band and firmware.
- **DISPUTED**: the projects disagree. Never pick a side by guessing; show the owner.

When the band contradicts this file, the band wins: update the file and say so.

## Safety rules, never relaxed

1. Send only commands in `whoop_lab.ble.commands.ALLOWED`, with exactly the argument bytes listed there. `build_allowed()` is the only way to build a command frame. Do not add a function that takes free arguments.
1a. The one exception is `build_history_ack()`, which builds `HISTORICAL_DATA_RESULT` (23) from the band's own `HISTORY_END` frame and can only echo that frame's token. It consumes history (see below), so write and fsync the chunk before sending it. Never build command 23 from a page number: the only other form is `build_trim_restore()`, described under "One bond, one consumer".
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
- ONE SOURCE (wearable5.0, one band, firmware 50.43.2): an acknowledgement carrying an older position, sent during a transfer after a `HISTORY_END`, moves trim back, so a drain can be undone and the app then uploads everything. The owner approved this on 2026-11-12 for one purpose: `build_trim_restore()` puts trim back to the position the band reported at the start of the same drain. It takes that `DataRange`, never a page number. Not yet run on the band.
- Re-reading older history uses the same frame with an older page. `build_trim_rewind()` implements it (a bounded number of pages back from the write head, clamped to history the band still holds), and `ble_spike.py drain --reread-hours` always follows it with a restore. The owner ran it once on 2026-11-12 by typing the confirmation. That is consent for that run, not standing approval: do not build anything that rewinds unattended until the owner says so. No other use of command 23 exists.
- BAND (2026-11-12): pairing the Mac removed the phone's bond; the WHOOP app had to be re-paired afterwards. Swapping between the app and this project costs a re-pair each way.

## GATT (BAND, 2026-11-12)

| Role | UUID |
|---|---|
| Service | `fd4b0001-cce1-4033-93ce-002d5875f58a` |
| Command write | `fd4b0002-…` |
| Command responses | `fd4b0003-…` |
| Events | `fd4b0004-…` |
| Data | `fd4b0005-…` |
| Memfault diagnostics | `fd4b0007-…` (not subscribed) |
| Standard heart rate | `0x2A37`, notifies without a bond |
| Device information | `0x2A29`, `2A24`, `2A25`, `2A26`, `2A27` readable without a bond; no `2A28` |
| Battery | `0x2A19`, read and notify |

A neighbouring service `11500001-6215-11ee-8c99-0242ac120002` belongs to the battery pack and uses different framing.

## Observed on the band (BAND, 2026-11-12)

- With the WHOOP app connected the band still advertises its name, but without the `fd4b0001` service. With the phone's Bluetooth off it advertises the service.
- An unpaired connection works: device information reads, and `0x2A37` sends one notification a second.
- R-R intervals on `0x2A37` are patchy. In a 120-second capture while awake, 27 of 120 notifications carried one (31 intervals, where about 140 beats occurred), in two runs. The unpaired stream alone is not enough for HRV; whether it is complete during sleep is untested.
- Subscribing to the `fd4b` channels without a bond makes the band drop the connection (bleak reports `disconnected`), and the next connection attempt can fail during service discovery. The band has to be in pairing mode: tap it until it flashes blue.

- Pairing works from macOS once the band is in pairing mode, and the bond persists: a second connection minutes later needed no pairing mode.
- `GET_HELLO` and `GET_DATA_RANGE` both answer twice: a PENDING reply (for hello, 107 zero bytes), then SUCCESS. Take the reply whose result is not PENDING. The hello body gives battery in tenths at 1, clock at 6, the serial as ASCII at 14 and firmware at 91 to 97, matching Device Information.
- `GET_DATA_RANGE` body is 67 bytes (65 plus padding); pages and the 131,072-page ring parse as documented. `GET_BATTERY_LEVEL` body starts with the percentage as one byte.
- A history request without acknowledgement returned `HISTORY_START`, 30 layout-18 records of 124 bytes (one per second, contiguous record indexes, starting exactly at the trim clock), events, 55 console frames, then `HISTORY_END`, all in about one second. Trim stayed where it was; only the read page advanced, by two pages. So one chunk is about two pages, or 30 seconds.
- Layout 18 decodes sensibly at the documented offsets: heart rate at 22, skin temperature at 73 (hundredths of a degree), gravity vector of length 1.0 at 45.
- Those 30 daytime records carried no R-R intervals at all (`rr_count` 0). Sources report 92% of beats overnight. Daytime R-R may simply be absent; unknown until a night is drained.
- Write rate: 200 pages an hour measured over 3 minutes, 250 an hour from trim clock to newest clock. That is 20 to 25 MB a day and 22 to 27 days of ring.
- The console log on the band says "No active optical data collection sources" and "No active IMU data collection sources", and shows the WHOOP app writing feature flags on each connect. Raw optical and IMU records are not being stored by default.

- **Re-read and restore work on this band** (BAND, 2026-11-12, run by the owner): trim was moved back about 4,100 pages, 1,095 chunks covering 16.4 hours (8.1 MB over the air) arrived in 264 seconds with no bytes lost and no gaps in the record index, and the restore put trim back on its original page, confirmed by a range read. About 220 times real time.
- **Overnight R-R is nearly complete; daytime R-R is not.** While the band reports SLEEP, over 90% of expected beats carry an R-R interval. While awake about a fifth do, and almost none in active hours. HRV from this band is a sleep-time measure.
- The vendored experimental helpers (sleep period from band state, RMSSD, respiratory rate from R-R, SpO2 windows from byte 82, skin temperature) all produced plausible values for one night, close to WHOOP's API values except HRV, where WHOOP's figure is not a whole-night RMSSD and its definition has to be matched, not assumed. One night proves nothing about agreement. The numbers themselves are biometric data and are not recorded in this repository.
- **Four nights against WHOOP's API** (BAND, 2026-11-15, ble-nightly-1 and -2): resting heart rate (median over the sleep window) within 1 bpm of WHOOP's every night; byte-82 blood oxygen within about 1 point, following WHOOP's night-to-night changes; skin temperature within about 0.25 C; respiratory rate from R-R 0.2 to 1.5 breaths a minute above WHOOP's (wearable5.0's method notes an upward bias from missed beats). RMSSD followed WHOOP's direction; on the three high-HRV nights it read 10 to 19% lower under ble-nightly-2, and lower still under the 20% rule and wearable5.0's 200 ms step filter, which discard real large beat-to-beat changes. Four nights prove nothing; this supports, but does not settle, the SpO2 field. Values are not recorded here.
- The share of beat-to-beat jumps rises with real HRV. It is not by itself evidence of noise: on 2026-11-15 a rise taken for noise was confirmed real by WHOOP's own HRV.
- **Daytime fields in R18** (BAND, 2026-11-15, 3.5 days): heart rate, band state, activity class (still, walk, run), movement (`dynamic_accel_g`), gravity, skin temperature and two step counters arrive every second all day. R-R does not (see above), and SpO2 only during sleep.
- **Step counters reset.** `step_counter` (noop and johga agree on it) only rose, except for drops to 0 that coincided with every session this project opened (12 Nov 12:16, 12:44, 13:46; 13 Nov 10:38; 15 Nov 10:23); not seen at the WHOOP app's syncs. Whether our hello or the bond causes it is unknown. `hw_step_counter` (undocumented) also drops to 0 mid-wear and counted 20 to 75% more. Count increases only (`ble/minutes.py`). Which counter is steps is open until compared with WHOOP's per-cycle `step_count` (Data health dashboard).
- **Recording pauses around our sessions**: no records for 26 minutes after the 12 Nov drain and 10 minutes after the 13 Nov one.
- Records seen in 16 hours: layout 18 (one per second, 124 bytes), layout 26 compact optical (88 bytes, in bursts roughly every 20 minutes), events, console logs. No layout 20 or 21.

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

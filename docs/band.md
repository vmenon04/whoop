# Reading the band over Bluetooth (experimental)

WHOOP's API serves scores computed on WHOOP's servers, and only while you have a membership. The band itself stores a record every second: heart rate, R-R intervals, movement, steps, skin temperature, blood oxygen during sleep, and whether you are awake or asleep. This part of the project copies those records over Bluetooth and computes its own nightly and per-minute metrics, using published methods rather than WHOOP's.

It works with a **WHOOP 5.0** band, from a Mac. The protocol was reverse-engineered by other projects, not documented by WHOOP; the decoder comes from [LeonardSEO/wearable5.0](https://github.com/LeonardSEO/wearable5.0). Expect rough edges. It has been tested on one band and one firmware (50.43.2.0).

## Safety

The band only receives a short, fixed list of read and transfer commands (`src/whoop_lab/ble/commands.py`). Firmware, configuration, alarms, clock writes and anything undocumented are refused in code, and a test checks that.

## How syncing works, and why it matters

- **Only one device can be connected.** The band keeps a single Bluetooth bond. Pairing the Mac removes the phone's bond, so you re-pair in the WHOOP app afterwards.
- **A read can hide data from WHOOP.** The band hands each stored record to whichever device acknowledges it first. A plain drain therefore takes those hours away from the WHOOP app, unless you put the band's sync position back afterwards. That is what `--restore` does, and you should use it while you still use the WHOOP app.

## Daily use

```sh
uv run --group ble python scripts/ble_spike.py drain --restore     # band nearby, phone Bluetooth off
```

The capture is saved under `captures/`, which git ignores. It is biometric data; don't commit it. If you skipped days, add `--reread-hours N` to read back further; the sync position is always restored afterwards. `scripts/ble_spike.py --help` lists the other commands. `status`, `scan` and `hr` read nothing from the history.

The worker imports new captures every five minutes from `CAPTURES_DIR` (default `./captures`). If the stack runs on another machine, copy the captures there:

```sh
WHOOP_HOST=myserver WHOOP_CAPTURES=/path/to/captures ./scripts/push_ble.sh
```

Then reconnect the band in the WHOOP app.

## What it computes

| Table | Contents |
| --- | --- |
| `ble_nights` | Per night: sleep window from the band's own state, RMSSD (HRV) with ectopic-beat correction (Kubios-style 200 ms threshold against a local median), the share of corrected beats (above 5% is flagged), resting heart rate, respiratory rate from R-R intervals, blood oxygen, skin temperature. |
| `ble_minutes` | Per minute, all day: heart rate, state, activity class, steps, movement, skin temperature. |

On the Health dashboard they appear next to WHOOP's values, and the Data health dashboard compares the two.

On one band over a few nights, resting heart rate agreed with WHOOP's within 1 bpm, blood oxygen within about 1 point and skin temperature within about 0.25 °C. HRV followed WHOOP's direction but read lower on high-HRV nights. Respiratory rate read slightly higher, a known bias of deriving it from R-R intervals. A few nights prove nothing, so treat these figures as a sanity check, not a validation.

To recompute everything after a change to the metrics, run `docker compose exec worker whoop-lab ble-import --all`.

The detailed protocol notes, with how well each fact is supported, are in `.claude/skills/whoop-ble/SKILL.md`.

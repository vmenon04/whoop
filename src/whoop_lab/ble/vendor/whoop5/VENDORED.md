# Vendored: whoop5

- Source: https://github.com/LeonardSEO/wearable5.0, directory `python/whoop5`
- Commit: `4361d069784b28bfcfd6ae278868e6d0fdb74820` (2026-11-06)
- License: MIT, Copyright (c) 2026 LeonardSEO. The full text is in `LICENSE` in this directory.

## Changes from upstream

Two functions that build frames to send to a band were removed, because this project
allows commands to be built only by `whoop_lab.ble.commands`:

- `framing.build_command`
- `decode.history_ack`

`__init__.py` no longer exports them. Nothing else was changed. Do not edit these
files by hand; to update, copy the new upstream files, reapply the two removals and
change the commit above.

This directory is excluded from ruff and mypy.

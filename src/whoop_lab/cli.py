"""`whoop-lab` command line."""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="WHOOP archive and experiments.")


@app.callback()
def _root() -> None:
    """WHOOP archive and n-of-1 experiments."""


def main() -> None:
    app()

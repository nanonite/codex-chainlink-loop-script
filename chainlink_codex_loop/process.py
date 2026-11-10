"""Subprocess helper shared by the command modules."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def run(
    argv: Sequence[str] | str,
    *,
    cwd: str | Path,
    timeout: float | None = None,
    verify: bool = False,
) -> tuple[int, str, str]:
    """Run a command and return its exit code, stdout, and stderr.

    A string command is accepted only for the user's shell-based verify command.
    All other commands must be provided as an argv sequence.
    """
    if isinstance(argv, str):
        if not verify:
            raise TypeError("string commands are allowed only for --verify")
        result = subprocess.run(
            argv,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    else:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return result.returncode, result.stdout, result.stderr

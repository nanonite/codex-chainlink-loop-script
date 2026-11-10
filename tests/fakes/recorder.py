#!/usr/bin/env python3
"""Shared recording and fixture replay logic for fake command binaries."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(name: str) -> int:
    record_dir = os.environ.get("CHAINLINK_LOOP_FAKE_DIR")
    entry = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "stdin": sys.stdin.read(),
    }
    if record_dir:
        path = Path(record_dir)
        path.mkdir(parents=True, exist_ok=True)
        records = path / f"{name}.jsonl"
        with records.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry) + "\n")

    fixture_path = os.environ.get("CHAINLINK_LOOP_FAKE_FIXTURE")
    fixture = {}
    if fixture_path:
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        if name in fixture and isinstance(fixture[name], dict):
            fixture = fixture[name]
    stdout = os.environ.get("CHAINLINK_LOOP_FAKE_STDOUT", fixture.get("stdout", ""))
    stderr = os.environ.get("CHAINLINK_LOOP_FAKE_STDERR", fixture.get("stderr", ""))
    exit_code = int(os.environ.get("CHAINLINK_LOOP_FAKE_EXIT_CODE", fixture.get("exit_code", 0)))
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code


if __name__ == "__main__":
    executable = Path(sys.argv[0]).name
    raise SystemExit(main(executable))

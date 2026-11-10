"""Command-line interface for chainlink-codex-loop."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chainlink-codex-loop")
    parser.add_argument("--repo", default=os.getcwd(), metavar="DIR")
    parser.add_argument("--backlog", action="store_true")
    parser.add_argument("--label", metavar="L")
    parser.add_argument("--priority", metavar="P")
    parser.add_argument("--max-issues", type=int, metavar="N")
    parser.add_argument("--attempts", type=int, default=3, metavar="N")
    parser.add_argument("--verify", metavar="CMD")
    parser.add_argument("--base", metavar="BRANCH")
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument("--model", metavar="M")
    parser.add_argument("--reviewer-model", metavar="M")
    parser.add_argument("--timeout", type=float, metavar="SECONDS")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("ids", nargs="*", metavar="ID")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.backlog and args.ids:
        parser.error("explicit IDs cannot be combined with --backlog")
    if not args.backlog and not args.ids:
        parser.error("provide one or more IDs or use --backlog")
    args.repo = Path(args.repo).expanduser().resolve()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line entry point for chainlink-codex-loop."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from . import backlog, codex
from .backlog import SelectionConfig
from .chainlink import ChainlinkError
from dataclasses import replace

from .gitops import DirtyWorktreeError, GitError, base_branch, ensure_clean, workspace_problem
from .loop import LoopConfig, Outcome, run_and_record
from .state import (
    InterruptController,
    RunState,
    WorkspaceLock,
    WorkspaceLockError,
    print_summary,
    process_exit_code,
)


DEFAULT_TIMEOUT_SECONDS = 3600.0

EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _progress(message: str) -> None:
    print(message, file=sys.stdout, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chainlink-codex-loop")
    parser.add_argument("--repo", default=os.getcwd(), metavar="DIR")
    parser.add_argument("--backlog", action="store_true")
    parser.add_argument("--label", metavar="L")
    parser.add_argument("--priority", metavar="P")
    parser.add_argument("--max-issues", type=int, metavar="N")
    parser.add_argument("--attempts", type=int, default=3, metavar="N")
    parser.add_argument("--verify", metavar="CMD")
    parser.add_argument(
        "--base",
        required=True,
        metavar="BRANCH",
        help="local branch that issue branches start from and approved work merges into (required)",
    )
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument("--model", metavar="M")
    parser.add_argument("--reviewer-model", metavar="M")
    parser.add_argument(
        "--effort", metavar="E", choices=EFFORTS,
        help="worker reasoning effort: " + ", ".join(EFFORTS),
    )
    parser.add_argument(
        "--reviewer-effort", metavar="E", choices=EFFORTS,
        help="reviewer reasoning effort: " + ", ".join(EFFORTS),
    )
    parser.add_argument(
        "--reviewer-sandbox",
        choices=codex.REVIEWER_SANDBOXES,
        default=codex.DEFAULT_REVIEWER_SANDBOX,
        help=(
            "Codex sandbox for the reviewer (default workspace-write, so it can run tests; "
            "its changes to the work tree are discarded)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "limit for each Codex run and each verify run "
            f"(default {DEFAULT_TIMEOUT_SECONDS:g}; 0 disables the limit)"
        ),
    )
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


def _preflight(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Check prerequisites without changing the target repository."""
    errors: list[str] = []
    warnings: list[str] = []
    for executable in ("codex", "chainlink"):
        if shutil.which(executable) is None:
            errors.append(f"{executable} is not on PATH")

    repo: Path = args.repo
    if not repo.is_dir():
        errors.append(f"target repository does not exist or is not a directory: {repo}")
    else:
        db_path = repo / ".chainlink" / "issues.db"
        if not db_path.is_file():
            errors.append(f"Chainlink database not found: {db_path}")

        valid_worktree = False
        try:
            ensure_clean(repo)
            valid_worktree = True
        except DirtyWorktreeError as error:
            valid_worktree = True
            if args.dry_run:
                warnings.append(f"dry-run target has a dirty work tree: {error}")
            else:
                errors.append(str(error))
        except (GitError, OSError) as error:
            errors.append(f"target must be a Git work tree: {error}")
        if valid_worktree:
            try:
                base_branch(repo, args.base)
            except (GitError, OSError) as error:
                errors.append(f"invalid base branch: {error}")

        try:
            WorkspaceLock.check_available(repo)
        except WorkspaceLockError as error:
            errors.append(str(error))

    if args.attempts < 1:
        errors.append("--attempts must be at least 1")
    if args.max_issues is not None and args.max_issues < 1:
        errors.append("--max-issues must be at least 1")
    if args.timeout is not None and args.timeout < 0:
        errors.append("--timeout must be 0 (no limit) or greater")
    if args.priority is not None and args.priority not in backlog.PRIORITY_ORDER:
        errors.append(f"unknown priority {args.priority!r}; use critical, high, medium, or low")
    return errors, warnings


def _selection_config(args: argparse.Namespace, progress=None, *, check_locks: bool = True) -> SelectionConfig:
    return SelectionConfig(
        repo=args.repo,
        explicit_ids=args.ids,
        backlog=args.backlog,
        label=args.label,
        priority=args.priority,
        max_issues=args.max_issues,
        progress=progress or _progress,
        check_locks=check_locks,
    )


def _preview_paths(repo: Path, issue_id: int | str) -> tuple[Path, Path, Path]:
    log_dir = repo / ".chainlink-loop" / "logs" / str(issue_id)
    worker_log = log_dir / "worker-1.jsonl"
    review_log = log_dir / "review-1.jsonl"
    verdict_path = log_dir / "verdict-1.json"
    return worker_log, review_log, verdict_path


def _print_preview(args: argparse.Namespace, issues: list[dict[str, object]]) -> None:
    repo: Path = args.repo
    schema = Path(__file__).parent / "schemas" / "verdict.json"
    for index, issue in enumerate(issues):
        if index:
            print()
        issue_id = issue.get("id", "unknown")
        worker_log, review_log, verdict_path = _preview_paths(repo, issue_id)
        worker_argv = codex.worker_argv(worker_log, args.model, args.effort)
        reviewer_argv = codex.reviewer_argv(
            schema, verdict_path, args.reviewer_model, args.reviewer_effort,
            args.reviewer_sandbox,
        )
        print(f"Issue #{issue_id}: {issue.get('title', '')}")
        print(f"  priority: {issue.get('priority', 'unknown')}")
        print(f"  branch: chainlink/{issue_id}")
        print(f"  worker argv: {json.dumps(worker_argv, ensure_ascii=False)}")
        print(f"  reviewer argv: {json.dumps(reviewer_argv, ensure_ascii=False)}")
        print(f"  verify command: {args.verify if args.verify is not None else 'none'}")
    if not issues:
        print("No eligible issues selected.")


def _dry_run(args: argparse.Namespace) -> int:
    try:
        # Chainlink 0.2.0's locks list fetches its Git-backed remote cache, so
        # a nominally read-only query can write inside the target repository.
        # Keep previews side-effect free and report that locks are not checked.
        issues = list(backlog.select_issues(_selection_config(args, check_locks=False)))
    except (ChainlinkError, ValueError, OSError) as error:
        print(f"error: could not select issues: {error}", file=sys.stderr)
        return 2
    try:
        print(f"Base branch: {base_branch(args.repo, args.base)}")
    except (GitError, OSError) as error:
        print(f"error: invalid base branch: {error}", file=sys.stderr)
        return 2
    print("Remote Chainlink issue locks: not checked during dry-run.")
    _print_preview(args, issues)
    return 0


def _run(args: argparse.Namespace) -> int:
    # Resolve the base once. Re-deriving it from HEAD for every issue would let a
    # run that got stranded on an issue branch stack later issues on top of it.
    try:
        base = base_branch(args.repo, args.base)
    except (GitError, OSError) as error:
        print(f"error: invalid base branch: {error}", file=sys.stderr)
        return 2
    _progress(f"base branch: {base}")
    config = LoopConfig(
        repo=args.repo,
        attempts=args.attempts,
        verify=args.verify,
        base=base,
        no_merge=args.no_merge,
        model=args.model,
        reviewer_model=args.reviewer_model,
        effort=args.effort,
        reviewer_effort=args.reviewer_effort,
        reviewer_sandbox=args.reviewer_sandbox,
        timeout=args.timeout or None,
        progress=_progress,
    )
    state = RunState(args.repo)
    outcomes: list[Outcome] = []
    runtime_error = False

    try:
        workspace_lock = WorkspaceLock(args.repo)
        with workspace_lock:
            with InterruptController() as interrupt:
                state.start_run(config)

                def transition(
                    issue_id: int | str,
                    phase: str,
                    attempt: int | None,
                    thread_id: str | None,
                ) -> None:
                    state.transition(issue_id, phase, attempt, thread_id)
                    attempt_text = "-" if attempt is None else str(attempt)
                    _progress(
                        f"issue #{issue_id}: phase={phase} attempt={attempt_text} outcome=running"
                    )

                config.transition = transition
                selection = iter(backlog.select_issues(_selection_config(args)))
                try:
                    while not interrupt.interrupted:
                        try:
                            issue = next(selection)
                        except StopIteration:
                            break
                        issue_id = issue.get("id")
                        if issue_id is None:
                            print("warning: selected Chainlink issue has no id; skipped", file=sys.stderr)
                            continue
                        outcome = run_and_record(issue_id, config)
                        if outcome is None:
                            continue
                        outcome = replace(outcome, issue_id=issue_id)
                        outcomes.append(outcome)
                        state.finish_issue(issue_id, outcome)
                        _progress(
                            f"issue #{issue_id}: phase=finished attempt={outcome.attempts_used} "
                            f"outcome={outcome.outcome}"
                        )
                        problem = workspace_problem(args.repo, base)
                        if problem is not None:
                            # Every later issue would fail the clean-tree check and
                            # collect a meaningless blocker comment, so stop here.
                            print(
                                f"error: stopping after issue #{issue_id}: {problem}. "
                                "Repair the repository, then rerun the remaining issues.",
                                file=sys.stderr,
                            )
                            runtime_error = True
                            break
                except (ChainlinkError, ValueError, OSError) as error:
                    print(f"error: could not continue issue selection: {error}", file=sys.stderr)
                    runtime_error = True
                finally:
                    state.finish_run()
                    print_summary(outcomes, stream=sys.stdout)

                if runtime_error:
                    return 2
                return process_exit_code(
                    outcomes,
                    interrupted=interrupt.interrupted,
                )
    except (WorkspaceLockError, GitError, OSError) as error:
        print(f"error: preflight or workspace setup failed: {error}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    errors, warnings = _preflight(args)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        return _dry_run(args)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())

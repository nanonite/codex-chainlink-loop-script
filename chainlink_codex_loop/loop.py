"""Single-issue worker, verification, and review state machine."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import chainlink, codex
from .gitops import GitError, GitOps
from .process import run
from .prompts import findings_prompt, reviewer_prompt, verify_failed_prompt, worker_prompt


@dataclass
class LoopConfig:
    repo: str | Path
    attempts: int = 3
    verify: str | None = None
    base: str | None = None
    model: str | None = None
    reviewer_model: str | None = None
    timeout: float | None = None
    schema_path: str | Path | None = None
    progress: Callable[[str], None] | None = None


@dataclass(frozen=True)
class Outcome:
    outcome: str
    attempts_used: int
    thread_id: str | None
    last_verdict: dict[str, Any] | None
    reason: str | None
    branch_name: str | None


def _notify(config: LoopConfig, message: str) -> None:
    if config.progress is not None:
        config.progress(message)


def _log_path(repo: Path, issue_id: int | str, action: str, attempt: int | None = None) -> Path:
    suffix = f"-{attempt}" if attempt is not None else ""
    return repo / ".chainlink-loop" / "logs" / f"issue-{issue_id}-{action}{suffix}.log"


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _verify(command: str, repo: Path, timeout: float | None) -> tuple[int, str, str, bool]:
    try:
        exit_code, stdout, stderr = run(command, cwd=repo, timeout=timeout, verify=True)
        return exit_code, stdout, stderr, False
    except subprocess.TimeoutExpired as error:
        return 124, _text(error.stdout), _text(error.stderr), True


def run_issue(issue_id: int | str, config: LoopConfig) -> Outcome:
    """Run one worker/verify/review cycle without writing to Chainlink or merging."""
    repo = Path(config.repo).expanduser().resolve()
    attempts_limit = int(config.attempts)
    if attempts_limit < 1:
        return Outcome(
            "escalated", 0, None, None, "attempts must be at least 1", None
        )

    attempts_used = 0
    thread_id: str | None = None
    verdict: dict[str, Any] | None = None
    branch_name: str | None = None
    try:
        issue = chainlink.show(issue_id, repo=repo)
        git = GitOps(repo, base=config.base)
        branch_name = git.start_issue(issue_id, title=str(issue.get("title", "")))
        _notify(config, f"issue #{issue_id}: starting worker on {branch_name}")
        worker = codex.run_worker(
            worker_prompt(issue),
            _log_path(repo, issue_id, "worker"),
            model=config.model,
            timeout=config.timeout,
            repo=repo,
        )
        thread_id = worker.thread_id

        schema_path = config.schema_path or Path(__file__).parent / "schemas" / "verdict.json"
        for attempt in range(1, attempts_limit + 1):
            attempts_used = attempt
            _notify(config, f"issue #{issue_id}: attempt {attempt}/{attempts_limit}")

            if config.verify:
                exit_code, stdout, stderr, timed_out = _verify(config.verify, repo, config.timeout)
                if exit_code:
                    output = stdout
                    if stderr:
                        output = f"{output}\n{stderr}" if output else stderr
                    timeout_detail = " (timed out)" if timed_out else ""
                    reason = f"Verification command failed with exit code {exit_code}{timeout_detail}"
                    if attempt == attempts_limit:
                        return Outcome(
                            "verify-failed", attempts_used, thread_id, verdict,
                            f"{reason}: {config.verify}", branch_name,
                        )
                    _notify(config, f"issue #{issue_id}: verification failed; asking worker to fix it")
                    resumed = codex.resume_worker(
                        thread_id,
                        verify_failed_prompt(config.verify, exit_code, output, issue),
                        _log_path(repo, issue_id, "resume-verify", attempt),
                        model=config.model,
                        timeout=config.timeout,
                        repo=repo,
                    )
                    thread_id = resumed.thread_id
                    continue

            _notify(config, f"issue #{issue_id}: starting fresh reviewer")
            verdict = codex.run_reviewer(
                reviewer_prompt(issue, git.base),
                schema_path,
                _log_path(repo, issue_id, "review", attempt),
                model=config.reviewer_model,
                timeout=config.timeout,
                repo=repo,
            )
            if verdict.get("verdict") == "approved":
                outcome = "done" if git.has_changes() else "approved-no-changes"
                return Outcome(outcome, attempts_used, thread_id, verdict, None, branch_name)

            if verdict.get("verdict") != "changes-requested":
                return Outcome(
                    "escalated", attempts_used, thread_id, verdict,
                    f"Reviewer returned unsupported verdict {verdict.get('verdict')!r}",
                    branch_name,
                )

            if attempt == attempts_limit:
                return Outcome(
                    "escalated", attempts_used, thread_id, verdict,
                    f"Reviewer still requested changes: {verdict.get('summary', '')}",
                    branch_name,
                )
            _notify(config, f"issue #{issue_id}: reviewer requested changes; resuming worker")
            resumed = codex.resume_worker(
                thread_id,
                findings_prompt(verdict, issue),
                _log_path(repo, issue_id, "resume-review", attempt),
                model=config.model,
                timeout=config.timeout,
                repo=repo,
            )
            thread_id = resumed.thread_id

        return Outcome(
            "escalated", attempts_used, thread_id, verdict,
            "Issue loop ended without an outcome", branch_name,
        )
    except (codex.CodexError, chainlink.ChainlinkError, GitError) as error:
        _notify(config, f"issue #{issue_id}: escalating after error: {error}")
        reason = str(error)
        stderr = getattr(error, "stderr", "").strip()
        if stderr and stderr not in reason:
            reason = f"{reason}: {stderr}"
        return Outcome("escalated", attempts_used, thread_id, verdict, reason, branch_name)

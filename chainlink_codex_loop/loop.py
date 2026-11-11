"""Single-issue worker, verification, and review state machine."""

from __future__ import annotations

import subprocess
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import chainlink, codex
from .gitops import GitError, GitOps
from .process import run
from .prompts import findings_prompt, reviewer_prompt, verify_failed_prompt, worker_prompt
from .state import runtime_log_path

LOGGER = logging.getLogger(__name__)


@dataclass
class LoopConfig:
    repo: str | Path
    attempts: int = 3
    verify: str | None = None
    base: str | None = None
    model: str | None = None
    reviewer_model: str | None = None
    effort: str | None = None
    reviewer_effort: str | None = None
    reviewer_sandbox: str = codex.DEFAULT_REVIEWER_SANDBOX
    timeout: float | None = None
    no_merge: bool = False
    schema_path: str | Path | None = None
    progress: Callable[[str], None] | None = None
    transition: Callable[[int | str, str, int | None, str | None], None] | None = None


@dataclass(frozen=True)
class Outcome:
    outcome: str
    attempts_used: int
    thread_id: str | None
    last_verdict: dict[str, Any] | None
    reason: str | None
    branch_name: str | None
    verify_output_tail: str | None = None
    issue_id: int | str | None = None


def _notify(config: LoopConfig, message: str) -> None:
    if config.progress is not None:
        config.progress(message)


def _log_path(repo: Path, issue_id: int | str, action: str, attempt: int) -> Path:
    extension = ".jsonl" if action in {"worker", "review"} else ".log"
    return runtime_log_path(repo, issue_id, f"{action}-{attempt}{extension}")


def _transition(
    config: LoopConfig,
    issue_id: int | str,
    phase: str,
    attempt: int | None,
    thread_id: str | None,
) -> None:
    if config.transition is not None:
        config.transition(issue_id, phase, attempt, thread_id)


def _write_verify_log(
    repo: Path,
    issue_id: int | str,
    attempt: int,
    stdout: str,
    stderr: str,
) -> None:
    path = runtime_log_path(repo, issue_id, f"verify-{attempt}.log")
    content = stdout
    if stderr:
        content = f"{content}\n{stderr}" if content else stderr
    path.write_text(content, encoding="utf-8")


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


def run_issue(
    issue_id: int | str,
    config: LoopConfig,
    *,
    on_issue_started: Callable[[dict[str, Any], GitOps, str], None] | None = None,
    on_thread_started: Callable[[str], None] | None = None,
) -> Outcome:
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
        if on_issue_started is not None:
            on_issue_started(issue, git, branch_name)
        _notify(config, f"issue #{issue_id}: starting worker on {branch_name}")
        _transition(config, issue_id, "worker", 1, None)
        worker = codex.run_worker(
            worker_prompt(issue),
            _log_path(repo, issue_id, "worker", 1),
            model=config.model,
            effort=config.effort,
            timeout=config.timeout,
            repo=repo,
        )
        thread_id = worker.thread_id
        if on_thread_started is not None:
            on_thread_started(thread_id)

        schema_path = config.schema_path or Path(__file__).parent / "schemas" / "verdict.json"
        for attempt in range(1, attempts_limit + 1):
            attempts_used = attempt
            _notify(config, f"issue #{issue_id}: attempt {attempt}/{attempts_limit}")

            verify_evidence: dict[str, Any] | None = None
            if config.verify:
                _transition(config, issue_id, "verify", attempt, thread_id)
                exit_code, stdout, stderr, timed_out = _verify(config.verify, repo, config.timeout)
                _write_verify_log(repo, issue_id, attempt, stdout, stderr)
                if not exit_code:
                    verify_evidence = {
                        "command": config.verify,
                        "exit_code": exit_code,
                        "output": f"{stdout}\n{stderr}" if stdout and stderr else stdout or stderr,
                    }
                if exit_code:
                    output = stdout
                    if stderr:
                        output = f"{output}\n{stderr}" if output else stderr
                    timeout_detail = " (timed out)" if timed_out else ""
                    reason = f"Verification command failed with exit code {exit_code}{timeout_detail}"
                    if attempt == attempts_limit:
                        output_lines = output.splitlines()
                        output_tail = "\n".join(output_lines[-200:]) if output_lines else ""
                        return Outcome(
                            "verify-failed", attempts_used, thread_id, verdict,
                            f"{reason}: {config.verify}", branch_name, output_tail,
                        )
                    _notify(config, f"issue #{issue_id}: verification failed; asking worker to fix it")
                    _transition(config, issue_id, "worker", attempt + 1, thread_id)
                    resumed = codex.resume_worker(
                        thread_id,
                        verify_failed_prompt(config.verify, exit_code, output, issue),
                        _log_path(repo, issue_id, "worker", attempt + 1),
                        model=config.model,
                        effort=config.effort,
                        timeout=config.timeout,
                        repo=repo,
                    )
                    thread_id = resumed.thread_id
                    continue

            _notify(config, f"issue #{issue_id}: starting fresh reviewer")
            _transition(config, issue_id, "review", attempt, thread_id)
            reviewer_can_write = config.reviewer_sandbox != "read-only"
            # A reviewer that can run tests can also write files. Whatever it
            # changes must not reach the commit or the worker's next turn.
            reviewed_tree = git.snapshot_tree() if reviewer_can_write else None
            try:
                verdict = codex.run_reviewer(
                    reviewer_prompt(
                        issue,
                        git.base,
                        can_run_commands=reviewer_can_write,
                        verify=verify_evidence,
                    ),
                    schema_path,
                    _log_path(repo, issue_id, "review", attempt),
                    model=config.reviewer_model,
                    effort=config.reviewer_effort,
                    timeout=config.timeout,
                    repo=repo,
                    verdict_path=runtime_log_path(repo, issue_id, f"verdict-{attempt}.json"),
                    sandbox=config.reviewer_sandbox,
                )
            finally:
                if reviewed_tree is not None:
                    discarded = git.restore_tree(reviewed_tree)
                    if discarded:
                        _notify(
                            config,
                            f"issue #{issue_id}: discarded reviewer changes to "
                            + ", ".join(discarded[:10])
                            + (" and others" if len(discarded) > 10 else ""),
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
            _transition(config, issue_id, "worker", attempt + 1, thread_id)
            resumed = codex.resume_worker(
                thread_id,
                findings_prompt(verdict, issue),
                _log_path(repo, issue_id, "worker", attempt + 1),
                model=config.model,
                effort=config.effort,
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
        thread_id = getattr(error, "thread_id", None) or thread_id
        reason = "interrupted" if isinstance(error, codex.CodexInterruptedError) else str(error)
        stderr = getattr(error, "stderr", "").strip()
        if stderr and stderr not in reason:
            reason = f"{reason}: {stderr}"
        return Outcome("escalated", attempts_used, thread_id, verdict, reason, branch_name)


def _safe_comment(
    issue_id: int | str,
    text: str,
    kind: str,
    repo: Path,
) -> bool:
    try:
        chainlink.comment(issue_id, text, kind=kind, repo=repo)
        return True
    except Exception as error:
        LOGGER.warning("Could not add %s comment to issue #%s: %s", kind, issue_id, error)
        return False


def _safe_close(issue_id: int | str, repo: Path) -> bool:
    try:
        chainlink.close(issue_id, repo=repo)
        return True
    except Exception as error:
        LOGGER.warning("Could not close issue #%s: %s", issue_id, error)
        return False


def _outcome_blocker_text(outcome: Outcome) -> str:
    details = [f"Outcome: {outcome.outcome}", f"Reason: {outcome.reason or 'No reason provided.'}"]
    if outcome.last_verdict:
        summary = outcome.last_verdict.get("summary")
        if summary:
            details.append(f"Reviewer summary: {summary}")
        findings = outcome.last_verdict.get("findings", [])
        if findings:
            details.append("Reviewer findings: " + json.dumps(findings, ensure_ascii=False))
    if outcome.verify_output_tail:
        lines = outcome.verify_output_tail.splitlines()
        tail = "\n".join(lines[-200:])
        details.append(f"Verifier output tail:\n{tail}")
    details.extend(
        [
            f"Branch: {outcome.branch_name or 'unavailable'}",
            f"Codex thread id: {outcome.thread_id or 'unavailable'}",
        ]
    )
    return "\n".join(details)


def run_and_record(issue_id: int | str, config: LoopConfig) -> Outcome | None:
    """Lock, run, record, and release one issue; return None when another agent owns it."""
    repo = Path(config.repo).expanduser().resolve()
    lock_claimed = False
    git_holder: dict[str, GitOps] = {}
    outcome: Outcome | None = None

    def record_plan(issue: dict[str, Any], git: GitOps, branch: str) -> None:
        git_holder["git"] = git
        _safe_comment(
            issue_id,
            f"The loop picked up issue #{issue_id} on branch {branch}. Worker thread id will be added when available.",
            "plan",
            repo,
        )

    def record_thread(thread_id: str) -> None:
        branch = git_holder["git"].current_issue_id
        branch_name = f"chainlink/{branch}" if branch is not None else "unavailable"
        _safe_comment(
            issue_id,
            f"The issue worker is running on branch {branch_name} with Codex thread id {thread_id}.",
            "plan",
            repo,
        )

    try:
        lock_claimed = chainlink.lock_claim(issue_id, branch=f"chainlink/{issue_id}", repo=repo)
        if not lock_claimed:
            try:
                if chainlink.lock_is_held_by_other(issue_id, repo=repo):
                    _notify(config, f"issue #{issue_id}: skipped because another agent holds its lock")
                    return None
            except Exception as error:
                LOGGER.warning("Could not determine lock ownership for issue #%s: %s", issue_id, error)

        chainlink.session_work(issue_id, repo=repo)
        outcome = run_issue(
            issue_id,
            config,
            on_issue_started=record_plan,
            on_thread_started=record_thread,
        )
        _transition(config, issue_id, "recording", outcome.attempts_used, outcome.thread_id)
        git = git_holder.get("git")

        if outcome.outcome == "done":
            if git is None:
                outcome = replace(
                    outcome,
                    outcome="escalated",
                    reason="Issue branch was not available for finalization",
                )
            else:
                try:
                    branch = git.finish_done(
                        issue_id,
                        merge=not config.no_merge,
                        title=str(git.current_issue_title or issue_id),
                    )
                    commit = git.branch_head(branch)
                    outcome = replace(outcome, branch_name=branch)
                    summary = (outcome.last_verdict or {}).get("summary", "Approved")
                    resolution = (
                        f"Reviewer approved: {summary}\nAttempts used: {outcome.attempts_used}\n"
                        f"Branch: {branch}\nCommit: {commit}"
                    )
                    _safe_comment(issue_id, resolution, "resolution", repo)
                    _safe_close(issue_id, repo)
                    return outcome
                except Exception as error:
                    detail = str(error)
                    stderr = getattr(error, "stderr", "").strip()
                    if stderr and stderr not in detail:
                        detail = f"{detail}: {stderr}"
                    outcome = replace(
                        outcome,
                        outcome="escalated",
                        reason=f"Could not finish approved issue: {detail}",
                    )
                    try:
                        git.finish_failed(issue_id, "escalated")
                    except Exception as finish_error:
                        LOGGER.warning("Could not return issue #%s to base: %s", issue_id, finish_error)

        elif outcome.outcome == "approved-no-changes":
            if git is not None:
                try:
                    git.finish_no_changes(issue_id)
                except Exception as error:
                    LOGGER.warning("Could not return unchanged issue #%s to base: %s", issue_id, error)
            observation = (
                "The reviewer found the issue already satisfied and there was no diff. "
                "A human should decide whether to close it."
            )
            _safe_comment(issue_id, observation, "observation", repo)
            return outcome

        if outcome.outcome in {"verify-failed", "escalated"}:
            if git is not None:
                try:
                    git.finish_failed(issue_id, outcome.outcome)
                except Exception as error:
                    LOGGER.warning("Could not save failed work for issue #%s: %s", issue_id, error)
            _safe_comment(issue_id, _outcome_blocker_text(outcome), "blocker", repo)
        return outcome
    except Exception as error:
        LOGGER.warning("Issue #%s processing failed: %s", issue_id, error)
        if outcome is None:
            detail = str(error)
            stderr = getattr(error, "stderr", "").strip()
            if stderr and stderr not in detail:
                detail = f"{detail}: {stderr}"
            outcome = Outcome(
                "escalated",
                0,
                getattr(error, "thread_id", None),
                None,
                detail,
                None,
            )
            _safe_comment(issue_id, _outcome_blocker_text(outcome), "blocker", repo)
        return outcome
    finally:
        if lock_claimed:
            try:
                chainlink.lock_release(issue_id, repo=repo)
            except Exception as error:
                LOGGER.warning("Could not release lock for issue #%s: %s", issue_id, error)

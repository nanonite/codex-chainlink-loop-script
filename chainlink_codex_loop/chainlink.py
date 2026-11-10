"""Chainlink issue tracker adapter.

All Chainlink subprocess calls live in this module. JSON is decoded from stdout
only because Chainlink may write diagnostics to stderr alongside valid JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .process import run

LOGGER = logging.getLogger(__name__)
DEFAULT_REPO = Path.cwd()


class ChainlinkError(RuntimeError):
    """A Chainlink command failed or returned data in an unexpected shape."""

    def __init__(
        self,
        message: str,
        *,
        argv: list[str],
        exit_code: int | None,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr


def _command(args: list[str], repo: str | Path) -> tuple[int, str, str]:
    argv = ["chainlink", *args]
    return run(argv, cwd=repo)


def _checked(args: list[str], repo: str | Path) -> tuple[str, str]:
    argv = ["chainlink", *args]
    code, stdout, stderr = run(argv, cwd=repo)
    if code:
        raise ChainlinkError(
            f"Chainlink command failed with exit code {code}: {' '.join(argv)}",
            argv=argv,
            exit_code=code,
            stderr=stderr,
        )
    return stdout, stderr


def _json(args: list[str], repo: str | Path) -> Any:
    argv = ["chainlink", *args]
    stdout, stderr = _checked(args, repo)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ChainlinkError(
            f"Chainlink returned invalid JSON for {' '.join(argv)}: {error}",
            argv=argv,
            exit_code=0,
            stderr=stderr,
        ) from error


def show(issue_id: int | str, repo: str | Path = DEFAULT_REPO) -> dict[str, Any]:
    """Return the full issue object, including labels and prior comments."""
    argv = ["issue", "show", str(issue_id), "--json"]
    issue = _json(argv, repo)
    if not isinstance(issue, dict):
        raise ChainlinkError(
            "Chainlink issue show returned a non-object JSON value",
            argv=["chainlink", *argv],
            exit_code=0,
            stderr="",
        )
    issue.setdefault("labels", [])
    issue.setdefault("comments", [])
    return issue


def ready(repo: str | Path = DEFAULT_REPO) -> list[dict[str, Any]]:
    """Return issues Chainlink currently reports as ready."""
    argv = ["issue", "ready", "--json"]
    issues = _json(argv, repo)
    if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
        raise ChainlinkError(
            "Chainlink issue ready returned an invalid JSON list",
            argv=["chainlink", *argv],
            exit_code=0,
            stderr="",
        )
    return issues


def comment(
    issue_id: int | str,
    text: str,
    kind: str = "note",
    repo: str | Path = DEFAULT_REPO,
) -> None:
    _checked(["issue", "comment", str(issue_id), text, "--kind", kind], repo)


def close(issue_id: int | str, repo: str | Path = DEFAULT_REPO) -> None:
    # Closing without this flag creates a CHANGELOG.md in the target source tree.
    _checked(["issue", "close", str(issue_id), "--no-changelog"], repo)


def session_work(issue_id: int | str, repo: str | Path = DEFAULT_REPO) -> None:
    args = ["session", "work", str(issue_id)]
    code, stdout, stderr = _command(args, repo)
    if code == 0:
        return
    if "no active session" not in stderr.lower():
        argv = ["chainlink", *args]
        raise ChainlinkError(
            f"Chainlink command failed with exit code {code}: {' '.join(argv)}",
            argv=argv,
            exit_code=code,
            stderr=stderr,
        )
    _checked(["session", "start"], repo)
    _checked(args, repo)


def _lock_unavailable(stderr: str) -> bool:
    message = stderr.lower()
    return any(
        phrase in message
        for phrase in (
            "no agent configured",
            "lock subsystem unavailable",
            "lock database unavailable",
            "lock backend unavailable",
            "locks unavailable",
            "could not open lock",
            "no tracker_remote configured",
            "no git remote configured",
        )
    )


def _lock_warning(operation: str, stderr: str) -> None:
    detail = stderr.strip() or "Chainlink lock support is unavailable"
    LOGGER.warning("Cannot %s Chainlink lock; continuing without a lock: %s", operation, detail)


def lock_claim(
    issue_id: int | str,
    branch: str | None = None,
    repo: str | Path = DEFAULT_REPO,
) -> bool:
    """Claim an issue lock, returning False when lock support is unavailable."""
    args = ["locks", "claim", str(issue_id)]
    if branch:
        args.extend(["--branch", branch])
    args.append("--json")
    code, _stdout, stderr = _command(args, repo)
    if code == 0:
        return True
    if _lock_unavailable(stderr):
        _lock_warning("claim", stderr)
        return False
    if "locked by '" in stderr.lower() or "not by you" in stderr.lower():
        LOGGER.warning("Issue #%s is locked by another Chainlink agent", issue_id)
        return False
    raise ChainlinkError(
        f"Chainlink lock claim failed with exit code {code}",
        argv=["chainlink", *args],
        exit_code=code,
        stderr=stderr,
    )


def lock_release(issue_id: int | str, repo: str | Path = DEFAULT_REPO) -> bool:
    """Release the current agent's lock; return False if locks are unavailable."""
    args = ["locks", "release", str(issue_id), "--json"]
    code, _stdout, stderr = _command(args, repo)
    if code == 0:
        return True
    if _lock_unavailable(stderr):
        _lock_warning("release", stderr)
        return False
    if "locked by '" in stderr.lower() or "not by you" in stderr.lower():
        LOGGER.warning("Issue #%s lock belongs to another Chainlink agent", issue_id)
        return False
    raise ChainlinkError(
        f"Chainlink lock release failed with exit code {code}",
        argv=["chainlink", *args],
        exit_code=code,
        stderr=stderr,
    )


def lock_is_held_by_other(
    issue_id: int | str,
    repo: str | Path = DEFAULT_REPO,
) -> bool:
    """Check lock ownership via the JSON lock list and local agent identity."""
    repo_path = Path(repo)
    agent_path = repo_path / ".chainlink" / "agent.json"
    try:
        agent = json.loads(agent_path.read_text(encoding="utf-8"))
        agent_id = agent.get("agent_id") if isinstance(agent, dict) else None
    except (OSError, json.JSONDecodeError):
        agent_id = None
    args = ["locks", "list", "--json"]
    code, stdout, stderr = _command(args, repo)
    if code:
        if _lock_unavailable(stderr):
            _lock_warning("check", stderr)
            return False
        raise ChainlinkError(
            f"Chainlink lock list failed with exit code {code}",
            argv=["chainlink", *args],
            exit_code=code,
            stderr=stderr,
        )
    try:
        result = json.loads(stdout)
        locks = result["locks"]
        lock = locks.get(str(issue_id))
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ChainlinkError(
            f"Chainlink locks list returned invalid JSON: {error}",
            argv=["chainlink", *args],
            exit_code=0,
            stderr=stderr,
        ) from error
    if not isinstance(lock, dict):
        if not agent_id:
            _lock_warning("check", "No agent configured in .chainlink/agent.json")
        return False
    # With no local identity, an existing lock cannot belong to this agent.
    return not agent_id or lock.get("agent_id") != agent_id

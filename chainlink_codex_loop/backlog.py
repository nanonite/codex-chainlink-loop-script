"""Lazy selection of explicit issues or the currently ready backlog."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, MutableSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import chainlink
from .chainlink import ChainlinkError

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class SelectionConfig:
    repo: str | Path
    explicit_ids: Iterable[int | str] = ()
    backlog: bool = False
    label: str | None = None
    priority: str | None = None
    max_issues: int | None = None
    progress: Callable[[str], None] | None = None


def _key(issue_id: int | str) -> str:
    return str(issue_id)


def _notify(config: SelectionConfig, message: str) -> None:
    if config.progress is not None:
        config.progress(message)


def _sort_id(issue_id: Any) -> tuple[int, int | str]:
    try:
        return 0, int(issue_id)
    except (TypeError, ValueError):
        return 1, str(issue_id)


def _priority_key(issue: dict[str, Any]) -> tuple[int, tuple[int, int | str]]:
    return (
        PRIORITY_ORDER.get(str(issue.get("priority", "medium")).lower(), len(PRIORITY_ORDER)),
        _sort_id(issue.get("id", "")),
    )


def _locked(issue_id: int | str, config: SelectionConfig) -> bool:
    try:
        return chainlink.lock_is_held_by_other(issue_id, repo=config.repo)
    except ChainlinkError as error:
        _notify(config, f"issue #{issue_id}: could not check lock; skipping this issue: {error}")
        return True


def _iter_explicit(
    config: SelectionConfig,
    attempted: MutableSet[str],
    limit: int | None,
) -> Iterator[dict[str, Any]]:
    yielded = 0
    for issue_id in config.explicit_ids:
        if limit is not None and yielded >= limit:
            return
        key = _key(issue_id)
        if key in attempted:
            _notify(config, f"issue #{issue_id}: already attempted in this run; skipped")
            continue
        try:
            issue = chainlink.show(issue_id, repo=config.repo)
        except ChainlinkError as error:
            _notify(config, f"issue #{issue_id}: could not be loaded; skipped: {error}")
            continue
        if issue.get("status") != "open":
            _notify(config, f"issue #{issue_id}: status is {issue.get('status')!r}; skipped")
            continue
        if _locked(issue_id, config):
            _notify(config, f"issue #{issue_id}: locked by another agent; skipped")
            continue
        attempted.add(key)
        yielded += 1
        yield issue


def _iter_backlog(
    config: SelectionConfig,
    attempted: MutableSet[str],
    limit: int | None,
) -> Iterator[dict[str, Any]]:
    if config.priority is not None and config.priority not in PRIORITY_ORDER:
        raise ValueError(f"unknown priority {config.priority!r}")
    yielded = 0
    label_ids: set[str] | None = None
    while limit is None or yielded < limit:
        ready = chainlink.ready(repo=config.repo)
        if config.label is not None:
            label_issues = chainlink.list_issues(label=config.label, status="open", repo=config.repo)
            label_ids = {_key(issue.get("id", "")) for issue in label_issues}
        else:
            label_ids = None

        candidates = [
            issue
            for issue in ready
            if _key(issue.get("id", "")) not in attempted
            and (config.priority is None or issue.get("priority") == config.priority)
            and (label_ids is None or _key(issue.get("id", "")) in label_ids)
        ]
        candidates.sort(key=_priority_key)

        selected: dict[str, Any] | None = None
        for issue in candidates:
            issue_id = issue.get("id")
            if _locked(issue_id, config):
                _notify(config, f"issue #{issue_id}: locked by another agent; skipped")
                continue
            selected = issue
            break
        if selected is None:
            return

        attempted.add(_key(selected.get("id", "")))
        yielded += 1
        yield selected


def select_issues(
    config: SelectionConfig,
    attempted: MutableSet[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield eligible issues lazily, refreshing ready issues after each yield."""
    already_attempted = attempted if attempted is not None else set()
    limit = config.max_issues
    if limit is not None and limit <= 0:
        return
    if config.backlog:
        yield from _iter_backlog(config, already_attempted, limit)
    else:
        yield from _iter_explicit(config, already_attempted, limit)

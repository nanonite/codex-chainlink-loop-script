"""Runtime state, per-repository lock, signal handling, and console summaries."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import codex
from .gitops import ensure_runtime_ignored


class WorkspaceLockError(RuntimeError):
    """Another chainlink-codex-loop process owns this repository."""


class WorkspaceLock:
    """Non-blocking advisory lock held for the lifetime of a loop run."""

    def __init__(self, repo: str | Path) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.runtime_dir = self.repo / ".chainlink-loop"
        self.path = self.runtime_dir / "lock"
        self._file: Any = None
        self._held = False

    def acquire(self) -> "WorkspaceLock":
        ensure_runtime_ignored(self.repo)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.seek(0)
            owner = lock_file.read().strip() or "unknown"
            lock_file.close()
            raise WorkspaceLockError(
                f"Another chainlink-codex-loop process holds the workspace lock (pid {owner})"
            ) from error
        except OSError as error:
            lock_file.close()
            raise WorkspaceLockError(f"Could not acquire workspace lock {self.path}: {error}") from error

        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(f"{os.getpid()}\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except OSError:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise
        self._file = lock_file
        self._held = True
        return self

    @staticmethod
    def check_available(repo: str | Path) -> None:
        """Check an existing lock without creating the runtime directory or lock file."""
        path = Path(repo).expanduser().resolve() / ".chainlink-loop" / "lock"
        if not path.exists():
            return
        try:
            lock_file = path.open("r", encoding="utf-8")
        except OSError as error:
            raise WorkspaceLockError(f"Could not inspect workspace lock {path}: {error}") from error
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                owner = lock_file.read().strip() or "unknown"
                raise WorkspaceLockError(
                    f"Another chainlink-codex-loop process holds the workspace lock (pid {owner})"
                ) from error
            except OSError as error:
                raise WorkspaceLockError(f"Could not inspect workspace lock {path}: {error}") from error
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            lock_file.close()

    def release(self) -> None:
        if not self._held:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._held = False

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def runtime_log_path(repo: str | Path, issue_id: int | str, filename: str) -> Path:
    directory = Path(repo).expanduser().resolve() / ".chainlink-loop" / "logs" / str(issue_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items() if not callable(item)}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if callable(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunState:
    """Atomically persist run configuration, active phase, and outcomes."""

    def __init__(self, repo: str | Path) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.runtime_dir = self.repo / ".chainlink-loop"
        self.path = self.runtime_dir / "state.json"
        self._data: dict[str, Any] = {}

    def start_run(self, config: Any) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._data = {
            "run_started_at": _now(),
            "updated_at": _now(),
            "config": _jsonable(config),
            "current_issue": None,
            "finished_issues": [],
        }
        self._write()

    def transition(
        self,
        issue_id: int | str,
        phase: str,
        attempt: int | None = None,
        thread_id: str | None = None,
    ) -> None:
        self._data["current_issue"] = {
            "id": issue_id,
            "phase": phase,
            "attempt": attempt,
            "thread_id": thread_id,
        }
        self._write()

    def finish_issue(self, issue_id: int | str, outcome: Any) -> None:
        finished = {
            "id": issue_id,
            "outcome": getattr(outcome, "outcome", None),
            "attempts_used": getattr(outcome, "attempts_used", None),
            "thread_id": getattr(outcome, "thread_id", None),
            "reason": getattr(outcome, "reason", None),
        }
        self._data.setdefault("finished_issues", []).append(_jsonable(finished))
        self._data["current_issue"] = None
        self._write()

    def finish_run(self) -> None:
        self._data["current_issue"] = None
        self._data["run_finished_at"] = _now()
        self._write()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def _write(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = _now()
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.runtime_dir,
                prefix=".state-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                json.dump(self._data, temporary, indent=2, ensure_ascii=False)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)


class InterruptController:
    """Kill the active Codex process group on the first signal; hard-exit on a second."""

    def __init__(self) -> None:
        self.interrupted = False
        self._previous: dict[int, Any] = {}

    def _handle(self, signum: int, frame: Any) -> None:
        if self.interrupted:
            os._exit(130)
        self.interrupted = True
        codex.request_interrupt()

    def __enter__(self) -> "InterruptController":
        codex.clear_interrupt()
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)


def print_transition(message: str, stream: TextIO = sys.stdout) -> None:
    print(message, file=stream, flush=True)


def format_summary(outcomes: Iterable[Any]) -> str:
    def concise_reason(outcome: Any) -> str:
        reason = str(getattr(outcome, "reason", "") or "")
        return reason if len(reason) <= 72 else reason[:69] + "..."

    def issue_label(outcome: Any) -> str:
        issue_id = getattr(outcome, "issue_id", None)
        return "" if issue_id is None else f"#{issue_id}"

    rows = [
        (
            issue_label(outcome),
            str(getattr(outcome, "outcome", "unknown")),
            str(getattr(outcome, "attempts_used", "")),
            str(getattr(outcome, "branch_name", "") or ""),
            concise_reason(outcome),
        )
        for outcome in outcomes
    ]
    headers = ("Issue", "Outcome", "Attempts", "Branch", "Reason")
    columns = range(len(headers))
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index]) for index in columns]
    separator = "  "
    lines = [separator.join(headers[i].ljust(widths[i]) for i in columns)]
    lines.append(separator.join("-" * width for width in widths))
    lines.extend(separator.join(row[i].ljust(widths[i]) for i in columns) for row in rows)
    return "\n".join(lines)


def print_summary(outcomes: Iterable[Any], stream: TextIO = sys.stdout) -> None:
    print(format_summary(outcomes), file=stream, flush=True)


def process_exit_code(outcomes: Iterable[Any], *, interrupted: bool = False, precondition_error: bool = False) -> int:
    if interrupted:
        return 130
    if precondition_error:
        return 2
    if any(getattr(outcome, "outcome", None) in {"verify-failed", "escalated"} for outcome in outcomes):
        return 1
    return 0

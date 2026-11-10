"""Codex CLI adapter for worker turns and fresh structured reviewers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CodexError(RuntimeError):
    """Base error raised for failed Codex CLI operations."""

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
        self.thread_id: str | None = None


class CodexExecutionError(CodexError):
    """Codex could not start, failed, or omitted its output file."""


class CodexTimeoutError(CodexError):
    """A Codex process exceeded its configured timeout."""


class CodexInterruptedError(CodexError):
    """A Codex process was stopped after SIGINT or SIGTERM."""


class CodexMissingThreadIdError(CodexError):
    """A worker JSONL stream did not contain a valid thread.started event."""


class CodexInvalidVerdictError(CodexError):
    """A reviewer response could not be parsed or did not match its schema."""


@dataclass(frozen=True)
class WorkerResult:
    thread_id: str
    exit_code: int
    last_message: str


_ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
_ACTIVE_LOCK = threading.Lock()
_INTERRUPT_REQUESTED = threading.Event()


def clear_interrupt() -> None:
    _INTERRUPT_REQUESTED.clear()


def request_interrupt() -> None:
    """Mark the current run interrupted and kill any active Codex process group."""
    _INTERRUPT_REQUESTED.set()
    with _ACTIVE_LOCK:
        process = _ACTIVE_PROCESS
    if process is not None:
        _stop_process_group(process)


def _absolute_log_path(log_path: str | Path) -> Path:
    path = Path(log_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _output_path(log_path: Path, suffix: str) -> Path:
    return log_path.with_name(f"{log_path.name}.{suffix}")


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _stream_command(
    argv: list[str],
    prompt: str,
    *,
    repo: str | Path,
    log_path: Path,
    timeout: float | None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run Codex with concurrent pipe readers and line-buffered stdout logging."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            argv,
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=(os.name == "posix"),
            creationflags=creationflags,
        )
    except OSError as error:
        raise CodexExecutionError(
            f"Could not start Codex: {error}",
            argv=argv,
            exit_code=None,
            stderr=str(error),
        ) from error

    global _ACTIVE_PROCESS
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESS = process
        already_interrupted = _INTERRUPT_REQUESTED.is_set()
    if already_interrupted:
        _stop_process_group(process)

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_parts: list[bytes] = []

    def write_prompt() -> None:
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def read_stdout() -> None:
        assert process.stdout is not None
        with log_path.open("wb") as log:
            for raw_line in iter(process.stdout.readline, b""):
                log.write(raw_line)
                log.flush()
                if on_stdout_line is not None:
                    on_stdout_line(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))

    def read_stderr() -> None:
        assert process.stderr is not None
        for raw_line in iter(process.stderr.readline, b""):
            stderr_parts.append(raw_line)

    stdout_thread = threading.Thread(target=read_stdout, name="codex-stdout", daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, name="codex-stderr", daemon=True)
    stdin_thread = threading.Thread(target=write_prompt, name="codex-stdin", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    stdin_thread.start()

    timed_out = False
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_group(process)
        return_code = process.wait()
    finally:
        with _ACTIVE_LOCK:
            if _ACTIVE_PROCESS is process:
                _ACTIVE_PROCESS = None

    stdin_thread.join()
    stdout_thread.join()
    stderr_thread.join()
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
    if _INTERRUPT_REQUESTED.is_set():
        raise CodexInterruptedError(
            "Codex command interrupted",
            argv=argv,
            exit_code=return_code,
            stderr=stderr,
        )
    if timed_out:
        raise CodexTimeoutError(
            f"Codex command exceeded timeout of {timeout} seconds",
            argv=argv,
            exit_code=return_code,
            stderr=stderr,
        )
    return return_code, stderr


def _thread_id_from_line(line: str) -> str | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") != "thread.started":
        return None
    thread_id = event.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _read_last_message(path: Path, argv: list[str], stderr: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CodexExecutionError(
            f"Codex did not create the last-message file {path}: {error}",
            argv=argv,
            exit_code=0,
            stderr=stderr,
        ) from error
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_output(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _raise_nonzero(
    argv: list[str],
    exit_code: int,
    stderr: str,
    thread_id: str | None = None,
) -> None:
    if exit_code:
        error = CodexExecutionError(
            f"Codex command failed with exit code {exit_code}",
            argv=argv,
            exit_code=exit_code,
            stderr=stderr,
        )
        error.thread_id = thread_id
        raise error


def run_worker(
    prompt: str,
    log_path: str | Path,
    model: str | None = None,
    timeout: float | None = None,
    repo: str | Path | None = None,
) -> WorkerResult:
    """Start a workspace-write Codex worker and return its thread id/message."""
    log = _absolute_log_path(log_path)
    last_path = _output_path(log, "last-message")
    argv = ["codex", "exec", "--json", "-s", "workspace-write"]
    if model:
        argv.extend(["-m", model])
    argv.extend(["-o", str(last_path), "-"])
    thread_ids: list[str] = []

    def parse_line(line: str) -> None:
        thread_id = _thread_id_from_line(line)
        if thread_id:
            thread_ids.append(thread_id)

    try:
        exit_code, stderr = _stream_command(
            argv,
            prompt,
            repo=repo or Path.cwd(),
            log_path=log,
            timeout=timeout,
            on_stdout_line=parse_line,
        )
    except (CodexTimeoutError, CodexInterruptedError) as error:
        error.thread_id = thread_ids[0] if thread_ids else None
        _remove_output(last_path)
        raise
    try:
        _raise_nonzero(argv, exit_code, stderr, thread_ids[0] if thread_ids else None)
    except CodexExecutionError:
        _remove_output(last_path)
        raise
    if not thread_ids:
        raise CodexMissingThreadIdError(
            "Codex worker output did not contain a thread.started event with thread_id",
            argv=argv,
            exit_code=exit_code,
            stderr=stderr,
        )
    return WorkerResult(thread_ids[0], exit_code, _read_last_message(last_path, argv, stderr))


def resume_worker(
    thread_id: str,
    prompt: str,
    log_path: str | Path,
    model: str | None = None,
    timeout: float | None = None,
    repo: str | Path | None = None,
) -> WorkerResult:
    """Resume the original workspace-write worker session."""
    log = _absolute_log_path(log_path)
    last_path = _output_path(log, "last-message")
    argv = ["codex", "exec", "resume", thread_id, "--json"]
    if model:
        argv.extend(["-m", model])
    argv.extend(["-o", str(last_path), "-"])
    resumed_ids: list[str] = []

    def parse_line(line: str) -> None:
        resumed_id = _thread_id_from_line(line)
        if resumed_id:
            resumed_ids.append(resumed_id)

    try:
        exit_code, stderr = _stream_command(
            argv,
            prompt,
            repo=repo or Path.cwd(),
            log_path=log,
            timeout=timeout,
            on_stdout_line=parse_line,
        )
    except (CodexTimeoutError, CodexInterruptedError) as error:
        error.thread_id = thread_id
        _remove_output(last_path)
        raise
    try:
        _raise_nonzero(argv, exit_code, stderr, thread_id)
    except CodexExecutionError:
        _remove_output(last_path)
        raise
    if not resumed_ids:
        raise CodexMissingThreadIdError(
            "Codex resume output did not contain a thread.started event with thread_id",
            argv=argv,
            exit_code=exit_code,
            stderr=stderr,
        )
    if resumed_ids[0] != thread_id:
        raise CodexExecutionError(
            f"Codex resumed thread {resumed_ids[0]!r}, expected {thread_id!r}",
            argv=argv,
            exit_code=exit_code,
            stderr=stderr,
        )
    return WorkerResult(thread_id, exit_code, _read_last_message(last_path, argv, stderr))


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema_value(value: Any, schema: Mapping[str, Any], location: str = "verdict") -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        raise ValueError(f"{location} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} must be one of {schema['enum']!r}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(f"{location} schema has invalid properties")
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{location} is missing required properties: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise ValueError(f"{location} has unexpected properties: {', '.join(sorted(unexpected))}")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                _validate_schema_value(child, child_schema, f"{location}.{key}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema_value(item, schema["items"], f"{location}[{index}]")


def run_reviewer(
    prompt: str,
    schema_path: str | Path,
    log_path: str | Path,
    model: str | None = None,
    timeout: float | None = None,
    repo: str | Path | None = None,
    verdict_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run a new read-only reviewer and validate its structured response."""
    log = _absolute_log_path(log_path)
    verdict_output = Path(verdict_path).expanduser().resolve() if verdict_path else _output_path(log, "verdict")
    verdict_output.parent.mkdir(parents=True, exist_ok=True)
    schema = Path(schema_path).expanduser().resolve()
    argv = [
        "codex", "exec", "-s", "read-only", "--output-schema", str(schema),
        "-o", str(verdict_output),
    ]
    if model:
        argv.extend(["-m", model])
    argv.append("-")
    exit_code, stderr = _stream_command(
        argv,
        prompt,
        repo=repo or Path.cwd(),
        log_path=log,
        timeout=timeout,
    )
    _raise_nonzero(argv, exit_code, stderr)
    try:
        verdict = json.loads(verdict_output.read_text(encoding="utf-8"))
        schema_value = json.loads(schema.read_text(encoding="utf-8"))
        if not isinstance(verdict, dict) or not isinstance(schema_value, dict):
            raise ValueError("verdict and schema must both be JSON objects")
        _validate_schema_value(verdict, schema_value)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise CodexInvalidVerdictError(
            f"Codex reviewer returned an invalid verdict: {error}",
            argv=argv,
            exit_code=exit_code,
            stderr=stderr,
        ) from error
    return verdict

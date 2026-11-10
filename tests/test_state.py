from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from chainlink_codex_loop.loop import LoopConfig, Outcome
from chainlink_codex_loop.state import (
    InterruptController,
    RunState,
    WorkspaceLock,
    WorkspaceLockError,
    format_summary,
    process_exit_code,
    runtime_log_path,
)


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
WORKER_EVENTS = (ROOT / "tests" / "fixtures" / "worker.jsonl").read_text(encoding="utf-8")


def make_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ("git", "init"),
        ("git", "config", "user.name", "State Test"),
        ("git", "config", "user.email", "state-test@example.invalid"),
    ):
        result = subprocess.run(args, cwd=repo, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    return branch


class RunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.base = make_git_repo(self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_workspace_lock_excludes_second_instance_and_ignores_stale_pid(self) -> None:
        first = WorkspaceLock(self.repo).acquire()
        try:
            self.assertEqual(first.path.read_text(encoding="utf-8").strip(), str(os.getpid()))
            with self.assertRaisesRegex(WorkspaceLockError, "holds the workspace lock"):
                WorkspaceLock(self.repo).acquire()
        finally:
            first.release()

        first.path.write_text("99999999\n", encoding="utf-8")
        with WorkspaceLock(self.repo) as recovered:
            self.assertEqual(recovered.path.read_text(encoding="utf-8").strip(), str(os.getpid()))
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertIn("/.chainlink-loop/", exclude.read_text(encoding="utf-8"))
        ignored = subprocess.run(
            ["git", "check-ignore", ".chainlink-loop/state.json"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_state_is_valid_json_after_each_transition_and_logs_are_per_issue(self) -> None:
        state = RunState(self.repo)
        state.start_run({"repo": self.repo, "attempts": 2})
        content = json.loads(state.path.read_text(encoding="utf-8"))
        self.assertIn("run_started_at", content)
        self.assertEqual(content["current_issue"], None)

        for phase, attempt, thread in (
            ("worker", 0, None),
            ("verify", 1, "thread-1"),
            ("review", 1, "thread-1"),
            ("recording", 1, "thread-1"),
        ):
            state.transition(22, phase, attempt, thread)
            content = json.loads(state.path.read_text(encoding="utf-8"))
            self.assertEqual(content["current_issue"]["phase"], phase)
            self.assertEqual(content["current_issue"]["attempt"], attempt)

        outcome = Outcome("escalated", 1, "thread-1", None, "review failed", "chainlink/22")
        state.finish_issue(22, outcome)
        content = json.loads(state.path.read_text(encoding="utf-8"))
        self.assertIsNone(content["current_issue"])
        self.assertEqual(content["finished_issues"][0]["outcome"], "escalated")
        self.assertEqual(content["finished_issues"][0]["reason"], "review failed")
        state.finish_run()
        self.assertIn("run_finished_at", json.loads(state.path.read_text(encoding="utf-8")))

        log = runtime_log_path(self.repo, 22, "worker-1.jsonl")
        self.assertEqual(log.parent, self.repo / ".chainlink-loop" / "logs" / "22")
        self.assertFalse(list((self.repo / ".chainlink-loop").glob(".state-*.tmp")))

    def test_summary_and_process_exit_codes(self) -> None:
        successes = [
            Outcome("done", 1, "t1", None, None, "chainlink/1"),
            Outcome("approved-no-changes", 1, "t2", None, None, "chainlink/2"),
        ]
        failed = [Outcome("verify-failed", 3, "t3", None, "verify", "chainlink/3")]
        summary = format_summary(successes)
        self.assertIn("Outcome", summary)
        self.assertIn("approved-no-changes", summary)
        self.assertEqual(process_exit_code(successes), 0)
        self.assertEqual(process_exit_code(failed), 1)
        self.assertEqual(process_exit_code(successes, precondition_error=True), 2)
        self.assertEqual(process_exit_code(failed, interrupted=True), 130)

        identified = format_summary(
            [
                Outcome("escalated", 0, None, None, "dirty", None, issue_id=7),
                Outcome("escalated", 0, None, None, "dirty", None, issue_id=8),
            ]
        )
        self.assertTrue(identified.splitlines()[0].startswith("Issue"))
        self.assertTrue(identified.splitlines()[2].startswith("#7"))
        self.assertTrue(identified.splitlines()[3].startswith("#8"))

        long_failure = Outcome("escalated", 2, "t4", None, "x" * 150, "chainlink/4")
        concise = format_summary([long_failure])
        self.assertIn("x" * 69 + "...", concise)
        self.assertLessEqual(max(map(len, concise.splitlines())), 120)


class InterruptIntegrationTests(unittest.TestCase):
    def test_sigint_kills_codex_group_records_interrupted_and_releases_both_locks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            base = make_git_repo(repo)
            issue_id = 211
            issue = {
                "id": issue_id,
                "title": "Interrupt test",
                "description": "## Acceptance criteria\nFinish safely.",
                "priority": "medium",
                "labels": [],
                "comments": [],
            }
            fixture_path = root / "fixture.json"
            fixture = {
                "chainlink": {
                    "responses": [
                        {"stdout": "claimed"},
                        {"stdout": "session work"},
                        {"stdout": json.dumps(issue)},
                        {"stdout": "plan comment"},
                        {"stdout": "blocker comment"},
                        {"stdout": "released"},
                    ]
                },
                "codex": {
                    "stdout": WORKER_EVENTS,
                    "last_message": "partial work",
                    "sleep_seconds": 60,
                    "spawn_child": True,
                    "write_files": {"partial.txt": "preserve on interruption\n"},
                },
            }
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            runner = root / "runner.py"
            runner.write_text(textwrap.dedent("""\
                import sys
                from pathlib import Path
                from chainlink_codex_loop.loop import LoopConfig, run_and_record
                from chainlink_codex_loop.state import (
                    InterruptController, RunState, WorkspaceLock,
                    print_summary, print_transition, process_exit_code,
                )

                repo = Path.cwd()
                config = LoopConfig(repo=repo, attempts=1, progress=print_transition)
                state = RunState(repo)
                state.start_run(config)
                config.transition = lambda issue, phase, attempt, thread: state.transition(issue, phase, attempt, thread)
                outcomes = []
                with WorkspaceLock(repo):
                    with InterruptController() as interrupt:
                        outcome = run_and_record(211, config)
                        if outcome is not None:
                            outcomes.append(outcome)
                            state.finish_issue(211, outcome)
                        state.finish_run()
                        print_summary(outcomes)
                        code = process_exit_code(outcomes, interrupted=interrupt.interrupted)
                raise SystemExit(code)
            """), encoding="utf-8")

            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["PATH"] = f"{FAKES}{os.pathsep}{env.get('PATH', '')}"
            records = root / "records"
            env["CHAINLINK_LOOP_FAKE_DIR"] = str(records)
            env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(fixture_path)
            process = subprocess.Popen(
                [sys.executable, str(runner)],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            codex_record = records / "codex.jsonl"
            child_file = records / "codex-child.pid"
            worker_log = repo / ".chainlink-loop" / "logs" / str(issue_id) / "worker-1.jsonl"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(f"runner exited before Codex started: {stdout}\n{stderr}")
                if codex_record.exists() and child_file.exists() and worker_log.exists():
                    if "thread.started" in worker_log.read_text(encoding="utf-8"):
                        break
                time.sleep(0.03)
            else:
                process.kill()
                stdout, stderr = process.communicate()
                self.fail(f"timed out waiting for fake Codex: {stdout}\n{stderr}")

            codex_pid = json.loads(codex_record.read_text(encoding="utf-8").splitlines()[0])["pid"]
            child_pid = int(child_file.read_text(encoding="utf-8"))
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 130, f"stdout={stdout}\nstderr={stderr}")
            self.assertIn("escalated", stdout)

            state = json.loads((repo / ".chainlink-loop" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["finished_issues"][0]["outcome"], "escalated")
            self.assertEqual(state["finished_issues"][0]["reason"], "interrupted")
            self.assertIsNone(state["current_issue"])
            current_branch = subprocess.run(
                ["git", "symbolic-ref", "--short", "HEAD"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(
                current_branch,
                base,
                f"stdout={stdout}\nstderr={stderr}\nstatus={subprocess.run(['git', 'status', '--porcelain'], cwd=repo, text=True, capture_output=True).stdout}",
            )
            self.assertIn("partial.txt", subprocess.run(["git", "ls-tree", "-r", "--name-only", "chainlink/211"], cwd=repo, text=True, capture_output=True, check=True).stdout)
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=True).stdout, "")

            calls = [json.loads(line)["argv"] for line in (records / "chainlink.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(calls[-1], ["locks", "release", str(issue_id), "--json"])
            blocker = next(call for call in calls if call[:2] == ["issue", "comment"] and "--kind" in call and call[call.index("--kind") + 1] == "blocker")
            self.assertIn("Codex thread id:", blocker[3])

            def running(pid: int) -> bool:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return False
                stat = Path(f"/proc/{pid}/stat")
                return not (stat.exists() and stat.read_text().split()[2] == "Z")

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (running(codex_pid) or running(child_pid)):
                time.sleep(0.03)
            self.assertFalse(running(codex_pid))
            self.assertFalse(running(child_pid))

            with WorkspaceLock(repo):
                pass


if __name__ == "__main__":
    unittest.main()

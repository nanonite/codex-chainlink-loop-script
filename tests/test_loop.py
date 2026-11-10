from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chainlink_codex_loop.loop import LoopConfig, run_issue


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
FIXTURES = ROOT / "tests" / "fixtures"
WORKER_EVENTS = (FIXTURES / "worker.jsonl").read_text(encoding="utf-8")
RESUME_EVENTS = (FIXTURES / "resume.jsonl").read_text(encoding="utf-8")


class IssueLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.records = self.root / "records"
        self.fixture = self.root / "fixture.json"
        self.env = os.environ.copy()
        self.env["PATH"] = f"{FAKES}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["CHAINLINK_LOOP_FAKE_DIR"] = str(self.records)
        for key in (
            "CHAINLINK_LOOP_FAKE_STDOUT",
            "CHAINLINK_LOOP_FAKE_STDERR",
            "CHAINLINK_LOOP_FAKE_EXIT_CODE",
        ):
            self.env.pop(key, None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_repo(self, name: str = "repo") -> Path:
        repo = self.root / name
        repo.mkdir(parents=True)
        self.git(repo, "init")
        self.git(repo, "config", "user.name", "Loop Test")
        self.git(repo, "config", "user.email", "loop-test@example.invalid")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-m", "base")
        return repo

    @staticmethod
    def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(f"git {args!r} failed: {result.stderr}")
        return result

    def configure(
        self,
        issue_id: int,
        responses: list[dict[str, object]],
        *,
        chainlink_exit_code: int = 0,
        chainlink_stderr: str = "",
    ) -> None:
        if self.records.exists():
            shutil.rmtree(self.records)
        issue = {
            "id": issue_id,
            "title": "Loop test issue",
            "description": "## Acceptance criteria\nComplete the requested work.",
            "priority": "high",
            "labels": ["test"],
            "comments": [],
        }
        fixture = {
            "chainlink": {
                "stdout": json.dumps(issue),
                "exit_code": chainlink_exit_code,
                "stderr": chainlink_stderr,
            },
            "codex": {"responses": responses},
        }
        self.fixture.write_text(json.dumps(fixture), encoding="utf-8")
        self.env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(self.fixture)

    def codex_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (self.records / "codex.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def worker_response(**overrides: object) -> dict[str, object]:
        return {
            "stdout": WORKER_EVENTS,
            "last_message": "worker completed",
            **overrides,
        }

    @staticmethod
    def resume_response(**overrides: object) -> dict[str, object]:
        return {
            "stdout": RESUME_EVENTS,
            "last_message": "worker resumed",
            **overrides,
        }

    @staticmethod
    def reviewer_response(verdict: dict[str, object], **overrides: object) -> dict[str, object]:
        return {
            "stdout": "review progress\n",
            "last_message": json.dumps(verdict),
            **overrides,
        }

    def run_config(self, repo: Path, **kwargs: object):
        return run_issue(101, LoopConfig(repo=repo, **kwargs))

    def test_approved_first_time_with_changes_is_done(self) -> None:
        repo = self.make_repo()
        approved = {"verdict": "approved", "summary": "All criteria met.", "findings": []}
        self.configure(
            101,
            [
                self.worker_response(write_files={"result.txt": "implemented\n"}),
                self.reviewer_response(approved),
            ],
        )
        progress: list[str] = []
        with patch.dict(os.environ, self.env, clear=True):
            result = self.run_config(repo, attempts=3, verify="exit 0", progress=progress.append)
        self.assertEqual(result.outcome, "done")
        self.assertEqual(result.attempts_used, 1)
        self.assertEqual(result.thread_id, json.loads(WORKER_EVENTS.splitlines()[0])["thread_id"])
        self.assertEqual(result.branch_name, "chainlink/101")
        self.assertIsNone(result.reason)
        self.assertEqual(self.git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip(), "chainlink/101")
        self.assertTrue(any("attempt 1/3" in message for message in progress))

    def test_approved_without_diff_is_approved_no_changes(self) -> None:
        repo = self.make_repo()
        approved = {"verdict": "approved", "summary": "No changes needed.", "findings": []}
        self.configure(101, [self.worker_response(), self.reviewer_response(approved)])
        with patch.dict(os.environ, self.env, clear=True):
            result = self.run_config(repo)
        self.assertEqual(result.outcome, "approved-no-changes")
        self.assertEqual(result.attempts_used, 1)

    def test_changes_requested_then_approved_resumes_same_thread(self) -> None:
        repo = self.make_repo()
        requested = {
            "verdict": "changes-requested",
            "summary": "Add the missing file.",
            "findings": [{"severity": "major", "file": "result.txt", "detail": "Create it."}],
        }
        approved = {"verdict": "approved", "summary": "Fixed.", "findings": []}
        self.configure(
            101,
            [
                self.worker_response(),
                self.reviewer_response(requested),
                self.resume_response(write_files={"result.txt": "fixed\n"}),
                self.reviewer_response(approved),
            ],
        )
        with patch.dict(os.environ, self.env, clear=True):
            result = self.run_config(repo, attempts=2)
        records = self.codex_records()
        thread_id = json.loads(WORKER_EVENTS.splitlines()[0])["thread_id"]
        self.assertEqual(result.outcome, "done")
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(records[2]["argv"][:4], ["exec", "resume", thread_id, "--json"])
        self.assertIn("Create it.", records[2]["stdin"])

    def test_verify_fails_until_attempts_exhausted(self) -> None:
        repo = self.make_repo()
        self.configure(101, [self.worker_response(), self.resume_response()])
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import sys; sys.exit(3)')}"
        with patch.dict(os.environ, self.env, clear=True):
            result = self.run_config(repo, attempts=2, verify=command)
        records = self.codex_records()
        self.assertEqual(result.outcome, "verify-failed")
        self.assertEqual(result.attempts_used, 2)
        self.assertIn("exit code 3", result.reason)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["argv"][:3], ["exec", "resume", result.thread_id])
        self.assertIn("Verifier command", records[1]["stdin"])

    def test_reviewer_never_approves_and_attempts_one_never_resumes(self) -> None:
        repo = self.make_repo()
        changes = {"verdict": "changes-requested", "summary": "Still incomplete.", "findings": []}
        self.configure(
            101,
            [
                self.worker_response(),
                self.reviewer_response(changes),
                self.resume_response(),
                self.reviewer_response(changes),
            ],
        )
        with patch.dict(os.environ, self.env, clear=True):
            result = self.run_config(repo, attempts=2)
        self.assertEqual(result.outcome, "escalated")
        self.assertEqual(result.attempts_used, 2)
        self.assertIn("still requested changes", result.reason)

        repo_one = self.make_repo("repo-one")
        self.configure(101, [self.worker_response(), self.reviewer_response(changes)])
        with patch.dict(os.environ, self.env, clear=True):
            one_attempt = self.run_config(repo_one, attempts=1)
        self.assertEqual(one_attempt.outcome, "escalated")
        self.assertEqual(one_attempt.attempts_used, 1)
        self.assertEqual(len(self.codex_records()), 2)
        self.assertFalse(any("resume" in record["argv"] for record in self.codex_records()))

    def test_worker_crash_timeout_and_malformed_verdict_escalate(self) -> None:
        crashed_repo = self.make_repo("crashed")
        self.configure(101, [{"stdout": "", "stderr": "worker crashed", "exit_code": 9}])
        with patch.dict(os.environ, self.env, clear=True):
            crashed = self.run_config(crashed_repo)
        self.assertEqual(crashed.outcome, "escalated")
        self.assertEqual(crashed.attempts_used, 0)

        timeout_repo = self.make_repo("timeout")
        self.configure(101, [self.worker_response(sleep_seconds=5)])
        with patch.dict(os.environ, self.env, clear=True):
            timed_out = self.run_config(timeout_repo, timeout=0.15)
        self.assertEqual(timed_out.outcome, "escalated")
        self.assertIn("timeout", timed_out.reason.lower())
        self.assertEqual(timed_out.thread_id, json.loads(WORKER_EVENTS.splitlines()[0])["thread_id"])

        malformed_repo = self.make_repo("malformed")
        self.configure(101, [self.worker_response(), self.reviewer_response({}, last_message="not JSON")])
        with patch.dict(os.environ, self.env, clear=True):
            malformed = self.run_config(malformed_repo)
        self.assertEqual(malformed.outcome, "escalated")
        self.assertEqual(malformed.attempts_used, 1)
        self.assertIn("invalid verdict", malformed.reason)

    def test_chainlink_failure_escalates_before_worker_start(self) -> None:
        repo = self.make_repo()
        self.configure(101, [], chainlink_exit_code=4, chainlink_stderr="tracker unavailable")
        with patch.dict(os.environ, self.env, clear=True):
            result = self.run_config(repo)
        self.assertEqual(result.outcome, "escalated")
        self.assertEqual(result.attempts_used, 0)
        self.assertIn("tracker unavailable", result.reason)
        self.assertFalse((self.records / "codex.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

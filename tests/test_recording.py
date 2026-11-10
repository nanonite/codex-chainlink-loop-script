from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chainlink_codex_loop import loop
from chainlink_codex_loop.loop import LoopConfig


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
FIXTURES = ROOT / "tests" / "fixtures"
WORKER_EVENTS = (FIXTURES / "worker.jsonl").read_text(encoding="utf-8")
RESUME_EVENTS = (FIXTURES / "resume.jsonl").read_text(encoding="utf-8")


class OutcomeRecordingTests(unittest.TestCase):
    issue_id = 201

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.name", "Recording Test")
        self.git("config", "user.email", "recording-test@example.invalid")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "base")
        self.base = self.git("symbolic-ref", "--short", "HEAD").stdout.strip()

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

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(f"git {args!r} failed: {result.stderr}")
        return result

    @staticmethod
    def worker(**overrides: object) -> dict[str, object]:
        return {"stdout": WORKER_EVENTS, "last_message": "worker done", **overrides}

    @staticmethod
    def reviewer(verdict: dict[str, object], **overrides: object) -> dict[str, object]:
        return {"stdout": "review log\n", "last_message": json.dumps(verdict), **overrides}

    def configure(
        self,
        codex_responses: list[dict[str, object]],
        *,
        chainlink_responses: list[dict[str, object]] | None = None,
    ) -> None:
        if self.records.exists():
            shutil.rmtree(self.records)
        issue = {
            "id": self.issue_id,
            "title": "Recording example",
            "description": "## Acceptance criteria\nComplete the work.",
            "labels": ["test"],
            "priority": "medium",
            "comments": [],
        }
        chainlink_calls = chainlink_responses or [
            {"stdout": "lock claimed"},
            {"stdout": "session work set"},
            {"stdout": json.dumps(issue)},
        ]
        self.fixture.write_text(
            json.dumps(
                {
                    "chainlink": {"responses": chainlink_calls},
                    "codex": {"responses": codex_responses},
                }
            ),
            encoding="utf-8",
        )
        self.env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(self.fixture)

    def chainlink_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (self.records / "chainlink.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def config(self, **kwargs: object) -> LoopConfig:
        return LoopConfig(repo=self.repo, **kwargs)

    def test_done_closes_after_resolution_and_releases_lock(self) -> None:
        approved = {"verdict": "approved", "summary": "All criteria met.", "findings": []}
        codex_responses = [self.worker(write_files={"result.txt": "done\n"}), self.reviewer(approved)]
        chainlink_responses = [
            {"stdout": "claimed"},
            {"stdout": "session"},
            {"stdout": json.dumps({
                "id": self.issue_id,
                "title": "Recording example",
                "description": "## Acceptance criteria\nComplete the work.",
                "labels": ["test"],
                "comments": [],
            })},
            {"stdout": "plan"},
            {"stdout": "thread plan"},
            {"stdout": "resolution"},
            {"stdout": "closed"},
            {"stdout": "released"},
        ]
        self.configure(codex_responses, chainlink_responses=chainlink_responses)
        with patch.dict(os.environ, self.env, clear=True):
            outcome = loop.run_and_record(self.issue_id, self.config())

        self.assertEqual(outcome.outcome, "done")
        records = self.chainlink_records()
        args = [record["argv"] for record in records]
        self.assertEqual(args[0], ["locks", "claim", str(self.issue_id), "--branch", f"chainlink/{self.issue_id}", "--json"])
        self.assertEqual(args[1], ["session", "work", str(self.issue_id)])
        self.assertEqual(args[-2], ["issue", "close", str(self.issue_id), "--no-changelog"])
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])
        comments = [arg for arg in args if arg[:2] == ["issue", "comment"]]
        kinds = [comment[comment.index("--kind") + 1] for comment in comments]
        self.assertEqual(kinds, ["plan", "plan", "resolution"])
        self.assertIn("Commit:", comments[-1][3])

    def test_approved_no_changes_stays_open_with_observation_and_release(self) -> None:
        approved = {"verdict": "approved", "summary": "Already satisfied.", "findings": []}
        self.configure([self.worker(), self.reviewer(approved)])
        with patch.dict(os.environ, self.env, clear=True):
            outcome = loop.run_and_record(self.issue_id, self.config())
        self.assertEqual(outcome.outcome, "approved-no-changes")
        args = [record["argv"] for record in self.chainlink_records()]
        self.assertFalse(any(argv[:2] == ["issue", "close"] for argv in args))
        comments = [argv for argv in args if argv[:2] == ["issue", "comment"]]
        self.assertEqual(comments[-1][comments[-1].index("--kind") + 1], "observation")
        self.assertIn("human should decide", comments[-1][3])
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").stdout.strip(), self.base)

    def test_verify_failure_stays_open_adds_blocker_and_releases_lock(self) -> None:
        self.configure([self.worker(write_files={"partial.txt": "preserved\n"})])
        with patch.dict(os.environ, self.env, clear=True):
            outcome = loop.run_and_record(
                self.issue_id,
                self.config(attempts=1, verify="exit 5"),
            )
        self.assertEqual(outcome.outcome, "verify-failed")
        args = [record["argv"] for record in self.chainlink_records()]
        self.assertFalse(any(argv[:2] == ["issue", "close"] for argv in args))
        comments = [argv for argv in args if argv[:2] == ["issue", "comment"]]
        self.assertEqual(comments[-1][comments[-1].index("--kind") + 1], "blocker")
        self.assertIn("Branch: chainlink/201", comments[-1][3])
        self.assertIn("Codex thread id:", comments[-1][3])
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])
        self.assertIn("partial.txt", self.git("ls-tree", "-r", "--name-only", "chainlink/201").stdout)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").stdout.strip(), self.base)

    def test_escalated_review_stays_open_with_findings_and_release(self) -> None:
        changes = {
            "verdict": "changes-requested",
            "summary": "Still broken.",
            "findings": [{"severity": "major", "file": "result.py", "detail": "Fix it."}],
        }
        self.configure([self.worker(write_files={"result.py": "work\n"}), self.reviewer(changes)])
        with patch.dict(os.environ, self.env, clear=True):
            outcome = loop.run_and_record(self.issue_id, self.config(attempts=1))
        self.assertEqual(outcome.outcome, "escalated")
        args = [record["argv"] for record in self.chainlink_records()]
        self.assertFalse(any(argv[:2] == ["issue", "close"] for argv in args))
        comments = [argv for argv in args if argv[:2] == ["issue", "comment"]]
        self.assertEqual(comments[-1][comments[-1].index("--kind") + 1], "blocker")
        self.assertIn("Fix it.", comments[-1][3])
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])

    def test_injected_exception_still_releases_lock_and_records_blocker(self) -> None:
        self.configure([])
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertLogs(loop.LOGGER, level="WARNING"):
                with patch.object(loop, "run_issue", side_effect=RuntimeError("injected failure")):
                    outcome = loop.run_and_record(self.issue_id, self.config())
        self.assertEqual(outcome.outcome, "escalated")
        args = [record["argv"] for record in self.chainlink_records()]
        self.assertFalse(any(argv[:2] == ["issue", "close"] for argv in args))
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])

    def test_merge_conflict_downgrades_to_blocker_and_never_closes(self) -> None:
        approved = {"verdict": "approved", "summary": "Approved.", "findings": []}
        self.configure(
            [self.worker(write_files={"README.md": "issue branch\n"}), self.reviewer(approved)]
        )
        changed_base = False
        progress_messages: list[str] = []

        def progress(message: str) -> None:
            nonlocal changed_base
            progress_messages.append(message)
            if not changed_base and "starting worker on" in message:
                changed_base = True
                self.git("checkout", self.base)
                (self.repo / "README.md").write_text("concurrent base edit\n", encoding="utf-8")
                self.git("commit", "-am", "concurrent base edit")
                self.git("checkout", f"chainlink/{self.issue_id}")

        with patch.dict(os.environ, self.env, clear=True):
            outcome = loop.run_and_record(self.issue_id, self.config(progress=progress))
        self.assertTrue(changed_base, f"outcome={outcome!r}; progress={progress_messages!r}")
        self.assertEqual(outcome.outcome, "escalated")
        self.assertIn("merge", outcome.reason.lower())
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").stdout.strip(), self.base)
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "concurrent base edit\n")
        args = [record["argv"] for record in self.chainlink_records()]
        self.assertFalse(any(argv[:2] == ["issue", "close"] for argv in args))
        comments = [argv for argv in args if argv[:2] == ["issue", "comment"]]
        self.assertEqual(comments[-1][comments[-1].index("--kind") + 1], "blocker")
        self.assertIn("Branch: chainlink/201", comments[-1][3])
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])

    def test_comment_recording_failure_is_logged_and_does_not_abort_done(self) -> None:
        approved = {"verdict": "approved", "summary": "Approved.", "findings": []}
        chainlink_responses = [
            {"stdout": "claimed"},
            {"stdout": "session"},
            {"stdout": json.dumps({
                "id": self.issue_id,
                "title": "Recording example",
                "description": "## Acceptance criteria\nComplete the work.",
                "labels": ["test"],
                "comments": [],
            })},
            {"stderr": "comment unavailable", "exit_code": 2},
            {"stdout": "thread comment"},
            {"stdout": "resolution"},
            {"stdout": "closed"},
            {"stdout": "released"},
        ]
        self.configure(
            [self.worker(write_files={"result.txt": "done\n"}), self.reviewer(approved)],
            chainlink_responses=chainlink_responses,
        )
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertLogs(loop.LOGGER, level="WARNING"):
                outcome = loop.run_and_record(self.issue_id, self.config())
        self.assertEqual(outcome.outcome, "done")
        args = [record["argv"] for record in self.chainlink_records()]
        self.assertEqual(args[-2], ["issue", "close", str(self.issue_id), "--no-changelog"])
        self.assertEqual(args[-1], ["locks", "release", str(self.issue_id), "--json"])


if __name__ == "__main__":
    unittest.main()

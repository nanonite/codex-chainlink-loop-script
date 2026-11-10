from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chainlink_codex_loop import codex


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
FIXTURES = ROOT / "tests" / "fixtures"
SCHEMA = ROOT / "chainlink_codex_loop" / "schemas" / "verdict.json"


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.records = self.repo / "records"
        self.fixture = self.repo / "fixture.json"
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

    def configure_fake(self, **fixture: object) -> None:
        self.fixture.write_text(json.dumps({"codex": fixture}), encoding="utf-8")
        self.env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(self.fixture)

    def records_for_codex(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (self.records / "codex.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def test_worker_and_resume_parse_real_jsonl_fixtures_and_keep_stdin_off_argv(self) -> None:
        worker_events = (FIXTURES / "worker.jsonl").read_text(encoding="utf-8")
        resume_events = (FIXTURES / "resume.jsonl").read_text(encoding="utf-8")
        expected_thread = next(
            json.loads(line)["thread_id"]
            for line in worker_events.splitlines()
            if json.loads(line).get("type") == "thread.started"
        )
        worker_output = "Codex startup notice\n" + worker_events

        self.configure_fake(stdout=worker_output, last_message="worker fixture captured.")
        with patch.dict(os.environ, self.env, clear=True):
            result = codex.run_worker(
                "- prompt begins with a dash",
                self.repo / "worker.log",
                model="test-model",
                timeout=2,
                repo=self.repo,
            )
        self.assertEqual(result.thread_id, expected_thread)
        self.assertEqual(result.last_message, "worker fixture captured.")
        self.assertEqual((self.repo / "worker.log").read_text(encoding="utf-8"), worker_output)
        worker_record = self.records_for_codex()[0]
        self.assertEqual(worker_record["stdin"], "- prompt begins with a dash")
        self.assertNotIn("- prompt begins with a dash", worker_record["argv"])
        self.assertEqual(worker_record["cwd"], str(self.repo))

        self.configure_fake(stdout=resume_events, last_message="resume fixture captured.")
        with patch.dict(os.environ, self.env, clear=True):
            resumed = codex.resume_worker(
                expected_thread,
                "review findings",
                self.repo / "resume.log",
                repo=self.repo,
            )
        self.assertEqual(resumed.thread_id, expected_thread)
        self.assertEqual(resumed.last_message, "resume fixture captured.")
        resume_record = self.records_for_codex()[1]
        self.assertEqual(resume_record["argv"][:5], ["exec", "resume", expected_thread, "--json", "-o"])
        self.assertNotIn("-s", resume_record["argv"])
        self.assertNotIn("-C", resume_record["argv"])

    def test_reviewer_uses_fresh_read_only_schema_command_and_validates_verdict(self) -> None:
        verdict = {"verdict": "approved", "summary": "Looks good.", "findings": []}
        self.configure_fake(stdout="review progress\n", last_message=json.dumps(verdict))
        with patch.dict(os.environ, self.env, clear=True):
            result = codex.run_reviewer(
                "review prompt",
                SCHEMA,
                self.repo / "review.log",
                model="review-model",
                repo=self.repo,
            )
        self.assertEqual(result, verdict)
        record = self.records_for_codex()[0]
        self.assertEqual(record["argv"][:3], ["exec", "-s", "read-only"])
        self.assertIn("--output-schema", record["argv"])
        self.assertIn(str(SCHEMA), record["argv"])
        self.assertNotIn("resume", record["argv"])
        self.assertEqual(record["stdin"], "review prompt")
        self.assertEqual((self.repo / "review.log").read_text(encoding="utf-8"), "review progress\n")

    def test_timeout_kills_worker_and_preserves_streamed_log(self) -> None:
        events = (FIXTURES / "worker.jsonl").read_text(encoding="utf-8")
        self.configure_fake(stdout=events, last_message="partial", sleep_seconds=5)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(codex.CodexTimeoutError) as raised:
                codex.run_worker(
                    "wait",
                    self.repo / "timeout.log",
                    timeout=0.2,
                    repo=self.repo,
                )
        expected_thread = json.loads(events.splitlines()[0])["thread_id"]
        self.assertEqual(raised.exception.thread_id, expected_thread)
        self.assertEqual((self.repo / "timeout.log").read_text(encoding="utf-8"), events)

    def test_nonzero_exit_and_missing_thread_id_have_distinct_errors(self) -> None:
        self.configure_fake(
            stdout=(FIXTURES / "worker.jsonl").read_text(encoding="utf-8"),
            stderr="codex failed",
            exit_code=7,
            last_message="unused",
        )
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(codex.CodexExecutionError) as raised:
                codex.run_worker("prompt", self.repo / "failed.log", repo=self.repo)
        self.assertEqual(raised.exception.exit_code, 7)
        self.assertEqual(raised.exception.stderr, "codex failed")

        self.configure_fake(stdout='{"type":"turn.started"}\n', last_message="unused")
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(codex.CodexMissingThreadIdError):
                codex.run_worker("prompt", self.repo / "missing.log", repo=self.repo)

    def test_malformed_reviewer_verdict_has_distinct_error(self) -> None:
        self.configure_fake(last_message="not JSON")
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(codex.CodexInvalidVerdictError):
                codex.run_reviewer("review", SCHEMA, self.repo / "bad-review.log", repo=self.repo)


if __name__ == "__main__":
    unittest.main()

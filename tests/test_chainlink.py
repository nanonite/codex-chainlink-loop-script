from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chainlink_codex_loop import chainlink


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"


class ChainlinkAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.records = self.repo / "records"
        self.fixture = self.repo / "fixture.json"
        self.env = os.environ.copy()
        self.env["PATH"] = f"{FAKES}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["CHAINLINK_LOOP_FAKE_DIR"] = str(self.records)
        self.env.pop("CHAINLINK_LOOP_FAKE_STDOUT", None)
        self.env.pop("CHAINLINK_LOOP_FAKE_STDERR", None)
        self.env.pop("CHAINLINK_LOOP_FAKE_EXIT_CODE", None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure_fake(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.fixture.write_text(
            json.dumps({
                "chainlink": {
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                }
            }),
            encoding="utf-8",
        )
        self.env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(self.fixture)

    def test_show_parses_stdout_and_ignores_warning_stderr(self) -> None:
        issue = {
            "id": 12,
            "title": "Example",
            "description": "Body",
            "priority": "high",
            "labels": ["adapter"],
            "comments": [{"kind": "plan", "text": "prior"}],
        }
        self.configure_fake(stdout=json.dumps(issue), stderr="WARN no tracker_remote configured\n")
        with patch.dict(os.environ, self.env, clear=True):
            result = chainlink.show(12, repo=self.repo)
        self.assertEqual(result["comments"][0]["text"], "prior")
        record = json.loads((self.records / "chainlink.jsonl").read_text().splitlines()[0])
        self.assertEqual(record["argv"], ["issue", "show", "12", "--json"])
        self.assertEqual(record["cwd"], str(self.repo))

    def test_nonzero_exit_raises_typed_error_with_stderr(self) -> None:
        self.configure_fake(stderr="database is unavailable", exit_code=7)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(chainlink.ChainlinkError) as raised:
                chainlink.ready(repo=self.repo)
        self.assertEqual(raised.exception.exit_code, 7)
        self.assertEqual(raised.exception.stderr, "database is unavailable")
        self.assertEqual(raised.exception.argv, ["chainlink", "issue", "ready", "--json"])

    def test_lock_held_by_another_agent_is_reported(self) -> None:
        config = self.repo / ".chainlink"
        config.mkdir()
        (config / "agent.json").write_text(json.dumps({"agent_id": "local-agent"}), encoding="utf-8")
        locks = {"version": 1, "locks": {"5": {"agent_id": "remote-agent", "branch": "chainlink/5"}}}
        self.configure_fake(stdout=json.dumps(locks))
        with patch.dict(os.environ, self.env, clear=True):
            held = chainlink.lock_is_held_by_other(5, repo=self.repo)
        self.assertTrue(held)
        record = json.loads((self.records / "chainlink.jsonl").read_text().splitlines()[0])
        self.assertEqual(record["argv"], ["locks", "list", "--json"])

    def test_existing_lock_is_reported_when_local_identity_is_missing(self) -> None:
        locks = {"version": 1, "locks": {"5": {"agent_id": "remote-agent"}}}
        self.configure_fake(stdout=json.dumps(locks))
        with patch.dict(os.environ, self.env, clear=True):
            held = chainlink.lock_is_held_by_other(5, repo=self.repo)
        self.assertTrue(held)

    def test_session_comment_and_close_use_target_repo_and_expected_flags(self) -> None:
        self.configure_fake()
        with patch.dict(os.environ, self.env, clear=True):
            chainlink.session_work(5, repo=self.repo)
            chainlink.comment(5, "done", "result", repo=self.repo)
            chainlink.close(5, repo=self.repo)
        records = [
            json.loads(line)
            for line in (self.records / "chainlink.jsonl").read_text().splitlines()
        ]
        self.assertEqual(records[0]["argv"], ["session", "work", "5"])
        self.assertEqual(records[1]["argv"], ["issue", "comment", "5", "done", "--kind", "result"])
        self.assertEqual(records[2]["argv"], ["issue", "close", "5", "--no-changelog"])
        self.assertTrue(all(record["cwd"] == str(self.repo) for record in records))

    def test_unavailable_lock_subsystem_warns_and_returns_not_locked(self) -> None:
        self.configure_fake(stderr="Error: Lock subsystem unavailable", exit_code=1)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertLogs(chainlink.LOGGER, level="WARNING") as logs:
                claimed = chainlink.lock_claim(5, repo=self.repo)
        self.assertFalse(claimed)
        self.assertIn("continuing without a lock", logs.output[0])


if __name__ == "__main__":
    unittest.main()

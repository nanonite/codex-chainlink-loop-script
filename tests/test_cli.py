from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from chainlink_codex_loop.cli import main
from chainlink_codex_loop.state import WorkspaceLock


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
WORKER_EVENTS = (ROOT / "tests" / "fixtures" / "worker.jsonl").read_text(encoding="utf-8")


class CliIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CLI Test")
        self.git("config", "user.email", "cli-test@example.invalid")
        self.git("config", "gc.auto", "0")
        self.git("config", "maintenance.auto", "false")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        (self.repo / ".chainlink").mkdir()
        (self.repo / ".chainlink" / "issues.db").write_bytes(b"fake database\n")
        self.git("add", "-A")
        self.git("commit", "-m", "base")

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

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=False
        )
        if result.returncode:
            raise AssertionError(f"git {args!r} failed: {result.stderr}")
        return result.stdout.strip()

    def configure(self, *, chainlink: list[dict[str, object]], codex: list[dict[str, object]] | None = None) -> None:
        fixture: dict[str, object] = {"chainlink": {"responses": chainlink}}
        if codex is not None:
            fixture["codex"] = {"responses": codex}
        self.fixture.write_text(json.dumps(fixture), encoding="utf-8")
        self.env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(self.fixture)

    @staticmethod
    def issue(issue_id: int, title: str) -> dict[str, object]:
        return {
            "id": issue_id,
            "title": title,
            "description": "## Acceptance criteria\nComplete this fake issue.",
            "priority": "medium",
            "status": "open",
            "labels": [],
            "comments": [],
        }

    @staticmethod
    def response(value: object) -> dict[str, object]:
        return {"stdout": json.dumps(value)}

    @staticmethod
    def worker(**kwargs: object) -> dict[str, object]:
        return {"stdout": WORKER_EVENTS, "last_message": "worker finished", **kwargs}

    @staticmethod
    def reviewer(verdict: dict[str, object]) -> dict[str, object]:
        return {"stdout": "review events\n", "last_message": json.dumps(verdict)}

    def chainlink_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (self.records / "chainlink.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def codex_records(self) -> list[dict[str, object]]:
        path = self.records / "codex.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.repo)): path.read_bytes()
            for path in self.repo.rglob("*")
            if path.is_file()
        }

    def invoke(self, args: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with patch.dict(os.environ, self.env, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                base = [] if "--base" in args or "--omit-base" in args else ["--base", "main"]
                argv = [arg for arg in args if arg != "--omit-base"]
                code = main(["--repo", str(self.repo), *base, *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_is_read_only_and_prints_exact_worker_and_reviewer_argv(self) -> None:
        issue = self.issue(301, "Preview issue")
        self.configure(chainlink=[self.response(issue), self.response({"locks": {}})])
        before = self.snapshot()
        branches_before = self.git("branch", "--list")

        code, output, stderr = self.invoke([
            "--dry-run", "--model", "worker-model", "--reviewer-model", "review-model",
            "--verify", "python -m unittest", "301",
        ])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Issue #301: Preview issue", output)
        self.assertIn("branch: chainlink/301", output)
        self.assertIn("verify command: python -m unittest", output)
        worker_argv = [
            "codex", "exec", "--json", "-s", "workspace-write", "-m", "worker-model",
            "-o", str(self.repo / ".chainlink-loop" / "logs" / "301" / "worker-1.jsonl.last-message"), "-",
        ]
        reviewer_argv = [
            "codex", "exec", "-s", "read-only", "--output-schema",
            str(ROOT / "chainlink_codex_loop" / "schemas" / "verdict.json"),
            "-o", str(self.repo / ".chainlink-loop" / "logs" / "301" / "verdict-1.json"),
            "-m", "review-model", "-",
        ]
        self.assertIn(f"worker argv: {json.dumps(worker_argv)}", output)
        self.assertIn(f"reviewer argv: {json.dumps(reviewer_argv)}", output)
        self.assertEqual(self.codex_records(), [])
        calls = [record["argv"] for record in self.chainlink_records()]
        self.assertEqual(calls, [["issue", "show", "301", "--json"]])
        self.assertIn("Remote Chainlink issue locks: not checked", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("branch", "--list"), branches_before)
        self.assertFalse((self.repo / ".chainlink-loop").exists())

    def test_dry_run_warns_on_dirty_worktree_but_still_previews(self) -> None:
        (self.repo / "README.md").write_text("modified before dry-run\n", encoding="utf-8")
        issue = self.issue(302, "Dirty preview")
        self.configure(chainlink=[self.response(issue), self.response({"locks": {}})])

        code, output, stderr = self.invoke(["--dry-run", "302"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Issue #302: Dirty preview", output)
        self.assertIn("WARNING: dry-run target has a dirty work tree", stderr)
        self.assertFalse((self.repo / ".chainlink-loop").exists())
        self.assertEqual(self.codex_records(), [])
        self.assertEqual(
            [record["argv"] for record in self.chainlink_records()],
            [["issue", "show", "302", "--json"]],
        )

    def test_preflight_refuses_existing_workspace_lock_without_changing_it(self) -> None:
        with WorkspaceLock(self.repo):
            before = self.snapshot()
            code, output, stderr = self.invoke(["--dry-run", "301"])
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertIn("holds the workspace lock", stderr)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.chainlink_records() if (self.records / "chainlink.jsonl").exists() else [], [])
            self.assertEqual(self.codex_records(), [])

    def test_two_issue_run_records_done_and_escalated_and_exits_one(self) -> None:
        done_issue = self.issue(401, "Approved issue")
        escalated_issue = self.issue(402, "Escalated issue")
        no_locks = self.response({"locks": {}})
        chainlink = [
            self.response(done_issue), no_locks,
            {"stdout": "claimed"}, {"stdout": "session work"}, self.response(done_issue),
            {"stdout": "plan"}, {"stdout": "thread plan"}, {"stdout": "resolution"},
            {"stdout": "closed"}, {"stdout": "released"},
            self.response(escalated_issue), no_locks,
            {"stdout": "claimed"}, {"stdout": "session work"}, self.response(escalated_issue),
            {"stdout": "plan"}, {"stdout": "thread plan"}, {"stdout": "blocker"},
            {"stdout": "released"},
        ]
        approved = {"verdict": "approved", "summary": "All criteria are met.", "findings": []}
        changes = {
            "verdict": "changes-requested",
            "summary": "One criterion remains unmet.",
            "findings": [{"severity": "major", "file": "second.txt", "detail": "Fix this."}],
        }
        codex = [
            self.worker(write_files={"first.txt": "approved work\n"}),
            self.reviewer(approved),
            self.worker(write_files={"second.txt": "partial work\n"}),
            self.reviewer(changes),
        ]
        self.configure(chainlink=chainlink, codex=codex)

        code, output, stderr = self.invoke(["--attempts", "1", "401", "402"])

        self.assertEqual(code, 1, f"stdout={output}\nstderr={stderr}")
        self.assertIn("outcome=done", output)
        self.assertIn("outcome=escalated", output)
        calls = [record["argv"] for record in self.chainlink_records()]
        closes = [argv for argv in calls if argv[:2] == ["issue", "close"]]
        self.assertEqual(closes, [["issue", "close", "401", "--no-changelog"]])
        comments = [argv for argv in calls if argv[:2] == ["issue", "comment"]]
        self.assertTrue(any(argv[2] == "401" and argv[-1] == "resolution" for argv in comments))
        self.assertTrue(any(argv[2] == "402" and argv[-1] == "blocker" for argv in comments))
        self.assertEqual(calls.count(["locks", "release", "401", "--json"]), 1)
        self.assertEqual(calls.count(["locks", "release", "402", "--json"]), 1)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("chainlink/401", self.git("branch", "--list"))
        self.assertIn("chainlink/402", self.git("branch", "--list"))
        state = json.loads((self.repo / ".chainlink-loop" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["outcome"] for item in state["finished_issues"]], ["done", "escalated"]
        )

    def test_run_stops_instead_of_blocking_every_issue_when_repo_is_left_broken(self) -> None:
        first = self.issue(501, "Leaves the repository broken")
        second = self.issue(502, "Must not be touched")
        no_locks = self.response({"locks": {}})
        chainlink = [
            self.response(first), no_locks,
            {"stdout": "claimed"}, {"stdout": "session work"}, self.response(first),
            {"stdout": "plan"}, {"stdout": "thread plan"}, {"stdout": "resolution"},
            {"stdout": "closed"}, {"stdout": "released"},
        ]
        approved = {"verdict": "approved", "summary": "Fine.", "findings": []}
        codex = [self.worker(write_files={"first.txt": "work\n"}), self.reviewer(approved)]
        self.configure(chainlink=chainlink, codex=codex)

        with patch(
            "chainlink_codex_loop.cli.workspace_problem",
            return_value="chainlink/501 is checked out instead of base branch main",
        ):
            code, output, stderr = self.invoke(["501", "502"])

        self.assertEqual(code, 2, f"stdout={output}\nstderr={stderr}")
        self.assertIn("stopping after issue #501", stderr)
        self.assertNotIn("issue #502", output)
        calls = [record["argv"] for record in self.chainlink_records()]
        self.assertFalse([argv for argv in calls if "502" in argv])
        self.assertEqual(len(self.codex_records()), 2)

    def test_summary_identifies_issues_and_base_branch_is_announced(self) -> None:
        issue = self.issue(601, "Approved issue")
        no_locks = self.response({"locks": {}})
        chainlink = [
            self.response(issue), no_locks,
            {"stdout": "claimed"}, {"stdout": "session work"}, self.response(issue),
            {"stdout": "plan"}, {"stdout": "thread plan"}, {"stdout": "resolution"},
            {"stdout": "closed"}, {"stdout": "released"},
        ]
        approved = {"verdict": "approved", "summary": "Fine.", "findings": []}
        codex = [self.worker(write_files={"first.txt": "work\n"}), self.reviewer(approved)]
        self.configure(chainlink=chainlink, codex=codex)

        code, output, stderr = self.invoke(["601"])

        self.assertEqual(code, 0, f"stdout={output}\nstderr={stderr}")
        self.assertIn("base branch: main", output)
        self.assertRegex(output, r"(?m)^#601\s+done\b")

    def test_timeout_defaults_to_an_hour_and_zero_disables_it(self) -> None:
        from chainlink_codex_loop import cli

        self.assertEqual(cli.parse_args(["--base", "main", "1"]).timeout, 3600)
        with patch("chainlink_codex_loop.cli.run_and_record") as run_and_record:
            run_and_record.return_value = None
            for flags, expected in (([], 3600), (["--timeout", "0"], None), (["--timeout", "5"], 5)):
                # The fakes replay responses by call count, so start each run fresh.
                shutil.rmtree(self.records, ignore_errors=True)
                run_and_record.reset_mock()
                issue = self.issue(701, "Timeout check")
                self.configure(chainlink=[self.response(issue), self.response({"locks": {}})])
                code, output, stderr = self.invoke([*flags, "701"])
                self.assertEqual(code, 0, f"stdout={output}\nstderr={stderr}")
                self.assertEqual(run_and_record.call_args.args[1].timeout, expected)
        code, _output, stderr = self.invoke(["--timeout", "-1", "701"])
        self.assertEqual(code, 2)
        self.assertIn("--timeout", stderr)

    def test_dry_run_prints_base_branch_and_refuses_issue_branch_as_base(self) -> None:
        issue = self.issue(801, "Preview")
        self.configure(chainlink=[self.response(issue)])
        code, output, stderr = self.invoke(["--dry-run", "801"])
        self.assertEqual(code, 0, f"stdout={output}\nstderr={stderr}")
        self.assertIn("Base branch: main", output)

        # The base is never inferred from the checked-out branch.
        with self.assertRaises(SystemExit) as missing:
            self.invoke(["--omit-base", "--dry-run", "801"])
        self.assertEqual(missing.exception.code, 2)

        code, _output, stderr = self.invoke(["--base", "no-such-branch", "--dry-run", "801"])
        self.assertEqual(code, 2)
        self.assertIn("no-such-branch", stderr)


if __name__ == "__main__":
    unittest.main()

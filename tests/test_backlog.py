from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chainlink_codex_loop.backlog import SelectionConfig, select_issues


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"


class BacklogSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.record_dir = self.repo / "records"
        self.fixture = self.repo / "fixture.json"
        agent_dir = self.repo / ".chainlink"
        agent_dir.mkdir()
        (agent_dir / "agent.json").write_text(json.dumps({"agent_id": "local"}), encoding="utf-8")
        self.env = os.environ.copy()
        self.env["PATH"] = f"{FAKES}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["CHAINLINK_LOOP_FAKE_DIR"] = str(self.record_dir)
        for key in (
            "CHAINLINK_LOOP_FAKE_STDOUT",
            "CHAINLINK_LOOP_FAKE_STDERR",
            "CHAINLINK_LOOP_FAKE_EXIT_CODE",
        ):
            self.env.pop(key, None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure(self, responses: list[dict[str, object]]) -> None:
        self.fixture.write_text(json.dumps({"chainlink": {"responses": responses}}), encoding="utf-8")
        self.env["CHAINLINK_LOOP_FAKE_FIXTURE"] = str(self.fixture)

    def issue(self, issue_id: int, priority: str = "medium", status: str = "open") -> dict[str, object]:
        return {
            "id": issue_id,
            "title": f"Issue {issue_id}",
            "description": "body",
            "priority": priority,
            "status": status,
        }

    @staticmethod
    def response(value: object, **other: object) -> dict[str, object]:
        return {"stdout": json.dumps(value), **other}

    @staticmethod
    def lock_map(*locked: tuple[int, str]) -> dict[str, object]:
        return {
            "version": 1,
            "locks": {
                str(issue_id): {"agent_id": agent, "branch": f"chainlink/{issue_id}"}
                for issue_id, agent in locked
            },
        }

    def load_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (self.record_dir / "chainlink.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def test_backlog_orders_by_priority_filters_labels_and_caps(self) -> None:
        ready = [
            self.issue(1, "high"),
            self.issue(2, "critical"),
            self.issue(3, "medium"),
            self.issue(4, "low"),
            self.issue(5, "critical"),
        ]
        labelled = [self.issue(1), self.issue(2), self.issue(3), self.issue(5)]
        self.configure(
            [
                self.response(ready), self.response(labelled), self.response(self.lock_map()),
                self.response(ready), self.response(labelled), self.response(self.lock_map()),
                self.response(ready), self.response(labelled), self.response(self.lock_map()),
            ]
        )
        with patch.dict(os.environ, self.env, clear=True):
            selected = list(select_issues(
                SelectionConfig(self.repo, backlog=True, label="wanted", max_issues=3)
            ))
        self.assertEqual([issue["id"] for issue in selected], [2, 5, 1])
        records = self.load_records()
        list_call = next(record for record in records if record["argv"][:3] == ["issue", "list", "--json"])
        self.assertEqual(list_call["argv"], ["issue", "list", "--json", "-l", "wanted", "-s", "open"])

    def test_priority_filter_and_attempted_issues_are_applied(self) -> None:
        ready = [self.issue(1, "critical"), self.issue(2, "high"), self.issue(3, "high")]
        self.configure(
            [
                self.response(ready), self.response(self.lock_map()),
                self.response(ready), self.response(self.lock_map()),
            ]
        )
        attempted: set[str] = {"2"}
        with patch.dict(os.environ, self.env, clear=True):
            selected = list(select_issues(
                SelectionConfig(self.repo, backlog=True, priority="high", max_issues=1),
                attempted=attempted,
            ))
        self.assertEqual([issue["id"] for issue in selected], [3])
        self.assertIn("3", attempted)

    def test_locked_issue_is_skipped_and_failed_issue_is_not_reselected(self) -> None:
        ready = [self.issue(1, "high"), self.issue(2, "medium")]
        empty_locks = self.lock_map()
        self.configure(
            [
                self.response(ready), self.response(self.lock_map((1, "remote"))),
                self.response(empty_locks),
                self.response(ready), self.response(self.lock_map((1, "remote"))),
            ]
        )
        attempted: set[str] = set()
        with patch.dict(os.environ, self.env, clear=True):
            selected = list(select_issues(
                SelectionConfig(self.repo, backlog=True), attempted=attempted
            ))
        self.assertEqual([issue["id"] for issue in selected], [2])
        self.assertEqual(attempted, {"2"})

    def test_ready_is_requeried_and_newly_unblocked_issue_is_selected(self) -> None:
        self.configure(
            [
                self.response([self.issue(10)]), self.response(self.lock_map()),
                self.response([self.issue(11)]), self.response(self.lock_map()),
                self.response([]),
            ]
        )
        with patch.dict(os.environ, self.env, clear=True):
            selected = list(select_issues(SelectionConfig(self.repo, backlog=True)))
        self.assertEqual([issue["id"] for issue in selected], [10, 11])
        ready_calls = [
            record for record in self.load_records()
            if record["argv"] == ["issue", "ready", "--json"]
        ]
        self.assertEqual(len(ready_calls), 3)

    def test_explicit_ids_keep_order_and_skip_missing_closed_or_locked(self) -> None:
        self.configure(
            [
                self.response(self.issue(3)), self.response(self.lock_map()),
                {"stderr": "issue not found", "exit_code": 1},
                self.response(self.issue(4, status="closed")),
                self.response(self.issue(5)), self.response(self.lock_map((5, "remote"))),
                self.response(self.issue(6)), self.response(self.lock_map()),
            ]
        )
        with patch.dict(os.environ, self.env, clear=True):
            selected = list(select_issues(
                SelectionConfig(self.repo, explicit_ids=[3, 99, 4, 5, 6])
            ))
        self.assertEqual([issue["id"] for issue in selected], [3, 6])

    def test_empty_ready_backlog_exits_cleanly(self) -> None:
        self.configure([self.response([])])
        with patch.dict(os.environ, self.env, clear=True):
            selected = list(select_issues(SelectionConfig(self.repo, backlog=True)))
        self.assertEqual(selected, [])
        self.assertEqual(len(self.load_records()), 1)


if __name__ == "__main__":
    unittest.main()

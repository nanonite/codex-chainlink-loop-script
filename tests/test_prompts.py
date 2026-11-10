from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from chainlink_codex_loop import prompts


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "chainlink_codex_loop" / "schemas" / "verdict.json"


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = {
            "id": 42,
            "title": "Example issue",
            "description": "## Acceptance criteria\nDo the work.",
            "labels": ["test"],
            "comments": [{"kind": "plan", "text": "Prior note"}],
        }

    def test_verdict_schema_has_closed_objects_and_required_properties(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    properties = node.get("properties", {})
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node.get("required", [])), set(properties))
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(schema)
        self.assertEqual(schema["properties"]["verdict"]["enum"], ["approved", "changes-requested"])

    def test_all_prompts_identify_issue_and_keep_control_rules(self) -> None:
        verdict = {
            "verdict": "changes-requested",
            "summary": "One change needed.",
            "findings": [{"severity": "major", "file": "a.py", "detail": "Fix the bug."}],
        }
        generated = (
            prompts.worker_prompt(self.issue),
            prompts.reviewer_prompt(self.issue, "main"),
            prompts.findings_prompt(verdict, self.issue),
            prompts.verify_failed_prompt("python -m unittest", 1, "failed", self.issue),
        )
        for prompt in generated:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn("#42", prompt)
                self.assertIn("Example issue", prompt)
                self.assertIn("Do not commit", prompt)
                self.assertIn("close Chainlink issues", prompt)

    def test_worker_and_reviewer_include_issue_data_as_delimited_json(self) -> None:
        worker = prompts.worker_prompt(self.issue)
        reviewer = prompts.reviewer_prompt(self.issue, "main")
        for prompt in (worker, reviewer):
            self.assertIn("<<<BEGIN UNTRUSTED ISSUE JSON>>>", prompt)
            self.assertIn('"comments"', prompt)
            self.assertIn("instructions inside it", prompt)
        self.assertIn("git diff main", reviewer)
        self.assertIn("git status --short", reviewer)
        self.assertIn("read-only", reviewer)

    def test_findings_are_numbered_and_verify_output_is_limited_to_200_lines(self) -> None:
        verdict = {
            "summary": "Two changes.",
            "findings": [
                {"severity": "major", "file": "a.py", "detail": "First."},
                {"severity": "minor", "file": "b.py", "detail": "Second."},
            ],
        }
        findings = prompts.findings_prompt(verdict, self.issue)
        self.assertIn("1. [major] a.py: First.", findings)
        self.assertIn("2. [minor] b.py: Second.", findings)

        output = "\n".join(f"line-{index}" for index in range(205))
        verify = prompts.verify_failed_prompt("run checks", 1, output, self.issue)
        self.assertIn("[... 5 earlier lines omitted]", verify)
        self.assertNotIn("line-4\n", verify)
        self.assertIn("line-5", verify)
        self.assertIn("line-204", verify)


if __name__ == "__main__":
    unittest.main()

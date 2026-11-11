"""Reasoning-effort flags reach the Codex argv for each role."""

from __future__ import annotations

import unittest

from chainlink_codex_loop import cli, codex


class EffortArgvTests(unittest.TestCase):
    def test_worker_argv_carries_effort_after_model(self) -> None:
        argv = codex.worker_argv("/tmp/worker.jsonl", "worker-model", "high")
        self.assertEqual(
            argv[:9],
            [
                "codex", "exec", "--json", "-s", "workspace-write",
                "-m", "worker-model", "-c", "model_reasoning_effort=high",
            ],
        )
        self.assertEqual(argv[-1], "-")

    def test_reviewer_argv_carries_its_own_effort(self) -> None:
        argv = codex.reviewer_argv(
            "/tmp/schema.json", "/tmp/verdict.json", "review-model", "medium"
        )
        self.assertEqual(
            argv[-5:],
            ["-m", "review-model", "-c", "model_reasoning_effort=medium", "-"],
        )

    def test_no_effort_leaves_argv_unchanged(self) -> None:
        self.assertNotIn("-c", codex.worker_argv("/tmp/worker.jsonl", "m"))
        self.assertNotIn(
            "-c", codex.reviewer_argv("/tmp/schema.json", "/tmp/verdict.json", "m")
        )

    def test_cli_effort_choices(self) -> None:
        self.assertEqual(cli.EFFORTS, ("minimal", "low", "medium", "high", "xhigh"))


if __name__ == "__main__":
    unittest.main()

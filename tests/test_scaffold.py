from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"


class ScaffoldTests(unittest.TestCase):
    def test_fake_binary_records_argv_cwd_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            records = temp / "records"
            target = temp / "target"
            target.mkdir()
            env = os.environ.copy()
            env["PATH"] = f"{FAKES}{os.pathsep}{env.get('PATH', '')}"
            env["CHAINLINK_LOOP_FAKE_DIR"] = str(records)
            env["CHAINLINK_LOOP_FAKE_STDOUT"] = "fixture output\n"
            command = subprocess.run(
                ["codex", "exec", "--json"],
                cwd=target,
                input="prompt body",
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(command.returncode, 0)
            self.assertEqual(command.stdout, "fixture output\n")
            record = json.loads((records / "codex.jsonl").read_text().splitlines()[0])
            self.assertEqual(record["argv"], ["exec", "--json"])
            self.assertEqual(record["cwd"], str(target))
            self.assertEqual(record["stdin"], "prompt body")

    def test_cli_requires_ids_or_backlog(self) -> None:
        cases = (
            (["--base", "main"], "provide one or more IDs"),
            (["--base", "main", "--backlog", "17"], "cannot be combined"),
            (["17"], "required: --base"),
        )
        for arguments, expected_error in cases:
            with self.subTest(arguments=arguments):
                command = subprocess.run(
                    [str(ROOT / "chainlink-codex-loop"), *arguments],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(command.returncode, 0)
                self.assertIn(expected_error, command.stderr)


if __name__ == "__main__":
    unittest.main()

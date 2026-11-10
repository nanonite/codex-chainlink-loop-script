from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from chainlink_codex_loop.gitops import (
    DetachedHeadError,
    DirtyWorktreeError,
    GitError,
    GitOps,
    MergeConflictError,
    ensure_clean,
)


class GitOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.run_git("init")
        self.run_git("config", "user.name", "GitOps Test")
        self.run_git("config", "user.email", "gitops-test@example.invalid")
        (self.repo / ".gitignore").write_text(".chainlink/\n", encoding="utf-8")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.run_git("add", "-A")
        self.run_git("commit", "-m", "base")
        self.base = self.run_git("symbolic-ref", "--short", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"git {args!r} failed: {result.stderr}")
        return result

    def current_branch(self) -> str:
        return self.run_git("symbolic-ref", "--short", "HEAD").stdout.strip()

    def status(self) -> str:
        return self.run_git("status", "--porcelain", "--untracked-files=all").stdout

    def test_dirty_tree_and_non_repository_are_refused(self) -> None:
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(DirtyWorktreeError):
            ensure_clean(self.repo)
        with self.assertRaises(DirtyWorktreeError):
            GitOps(self.repo)

        with tempfile.TemporaryDirectory() as non_repo:
            other = Path(non_repo)
            with self.assertRaises(GitError):
                ensure_clean(other)

    def test_runtime_and_ignored_chainlink_files_do_not_dirty_or_commit(self) -> None:
        operations = GitOps(self.repo)
        operations.start_issue(20, "Runtime exclusion")
        (self.repo / ".chainlink-loop").mkdir()
        (self.repo / ".chainlink-loop" / "state.json").write_text("{}", encoding="utf-8")
        (self.repo / ".chainlink").mkdir()
        (self.repo / ".chainlink" / "issues.db").write_text("runtime", encoding="utf-8")
        (self.repo / "wanted.txt").write_text("track me\n", encoding="utf-8")
        self.assertTrue(operations.commit_all("runtime exclusion"))
        tree = self.run_git("ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertIn("wanted.txt", tree)
        self.assertNotIn(".chainlink-loop", tree)
        self.assertNotIn(".chainlink/issues.db", tree)
        self.assertEqual(operations.has_changes(), True)

    def test_branch_creation_reuse_and_change_detection(self) -> None:
        operations = GitOps(self.repo)
        self.assertFalse(operations.has_changes())
        self.assertEqual(operations.start_issue(12, "Branch test"), "chainlink/12")
        self.assertEqual(self.current_branch(), "chainlink/12")
        self.assertFalse(operations.has_changes())
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self.assertTrue(operations.has_changes())
        operations.commit_all("work")
        self.assertTrue(operations.has_changes())
        self.run_git("checkout", operations.base)

        resumed = GitOps(self.repo)
        self.assertEqual(resumed.start_issue(12), "chainlink/12")
        self.assertEqual(self.current_branch(), "chainlink/12")
        self.assertTrue(resumed.has_changes())

    def test_done_path_merges_and_uses_issue_commit_message(self) -> None:
        operations = GitOps(self.repo)
        operations.start_issue(13, "Merged work")
        (self.repo / "merged.txt").write_text("merged\n", encoding="utf-8")
        branch = operations.finish_done(13, merge=True)
        self.assertEqual(branch, "chainlink/13")
        self.assertEqual(self.current_branch(), operations.base)
        self.assertEqual((self.repo / "merged.txt").read_text(encoding="utf-8"), "merged\n")
        self.run_git("show", "-s", "--format=%s", "chainlink/13")
        subject = self.run_git("show", "-s", "--format=%s", "chainlink/13").stdout.strip()
        self.assertEqual(subject, "chainlink #13: Merged work")
        self.assertEqual(self.status(), "")

    def test_done_path_without_merge_returns_to_base_and_leaves_branch(self) -> None:
        operations = GitOps(self.repo)
        operations.start_issue(14, "Unmerged work")
        (self.repo / "unmerged.txt").write_text("branch only\n", encoding="utf-8")
        branch = operations.finish_done(14, merge=False)
        self.assertEqual(branch, "chainlink/14")
        self.assertEqual(self.current_branch(), operations.base)
        self.assertFalse((self.repo / "unmerged.txt").exists())
        self.assertIn("unmerged.txt", self.run_git("ls-tree", "-r", "--name-only", branch).stdout)
        self.assertEqual(self.status(), "")

    def test_no_changes_path_returns_to_base_without_creating_a_commit(self) -> None:
        operations = GitOps(self.repo)
        branch = operations.start_issue(17, "No changes")
        head_before = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(operations.finish_no_changes(17), branch)
        self.assertEqual(self.current_branch(), operations.base)
        self.assertEqual(self.run_git("rev-parse", branch).stdout.strip(), head_before)
        self.assertEqual(self.status(), "")

    def test_merge_conflict_aborts_merge_and_preserves_issue_branch(self) -> None:
        operations = GitOps(self.repo)
        operations.start_issue(15, "Conflicting work")
        (self.repo / "base.txt").write_text("issue branch\n", encoding="utf-8")
        operations.commit_all("issue branch change")
        issue_head = self.run_git("rev-parse", "HEAD").stdout.strip()

        self.run_git("checkout", operations.base)
        (self.repo / "base.txt").write_text("base branch change\n", encoding="utf-8")
        self.run_git("commit", "-am", "base branch change")
        self.run_git("checkout", "chainlink/15")
        with self.assertRaises(MergeConflictError):
            operations.finish_done(15, merge=True)

        self.assertEqual(self.current_branch(), operations.base)
        self.assertEqual((self.repo / "base.txt").read_text(encoding="utf-8"), "base branch change\n")
        self.assertEqual(self.run_git("rev-parse", "chainlink/15").stdout.strip(), issue_head)
        self.assertEqual(self.status(), "")

    def test_failed_path_saves_work_as_wip_and_leaves_base_unchanged(self) -> None:
        operations = GitOps(self.repo)
        operations.start_issue(16, "Failed work")
        (self.repo / "wip.txt").write_text("keep this\n", encoding="utf-8")
        branch = operations.finish_failed(16, "escalated")

        self.assertEqual(branch, "chainlink/16")
        self.assertEqual(self.current_branch(), operations.base)
        self.assertFalse((self.repo / "wip.txt").exists())
        self.assertIn("wip.txt", self.run_git("ls-tree", "-r", "--name-only", branch).stdout)
        subject = self.run_git("show", "-s", "--format=%s", branch).stdout.strip()
        self.assertEqual(subject, "WIP chainlink #16 (escalated)")
        self.assertEqual(self.status(), "")

    def test_detached_head_is_an_error_even_when_base_is_requested(self) -> None:
        self.run_git("checkout", "--detach", "HEAD")
        with self.assertRaises(DetachedHeadError):
            GitOps(self.repo, base=self.base)


if __name__ == "__main__":
    unittest.main()

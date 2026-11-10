"""Git isolation for one issue branch at a time."""

from __future__ import annotations

from pathlib import Path

from .process import run


class GitError(RuntimeError):
    """A Git command failed or the repository is in an invalid state."""

    def __init__(
        self,
        message: str,
        *,
        argv: list[str],
        exit_code: int | None,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr


class DirtyWorktreeError(GitError):
    """The repository has changes before an issue run starts."""


class DetachedHeadError(GitError):
    """The repository has no checked-out branch to use as its base."""


class MergeConflictError(GitError):
    """An issue branch could not be merged cleanly into the base branch."""


def _git(repo: str | Path, args: list[str]) -> tuple[int, str, str]:
    return run(["git", *args], cwd=repo)


def _checked(repo: str | Path, args: list[str]) -> str:
    code, stdout, stderr = _git(repo, args)
    if code:
        argv = ["git", *args]
        raise GitError(
            f"Git command failed with exit code {code}: {' '.join(argv)}",
            argv=argv,
            exit_code=code,
            stderr=stderr,
        )
    return stdout


def _tool_runtime_path(path: str) -> bool:
    normalized = path.strip().strip('"').removeprefix("./")
    return normalized == ".chainlink-loop" or normalized.startswith(".chainlink-loop/")


def _status_paths(repo: str | Path) -> list[str]:
    output = _checked(repo, ["status", "--porcelain", "--untracked-files=all"])
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        candidates = path.split(" -> ", maxsplit=1)
        if not any(_tool_runtime_path(candidate) for candidate in candidates):
            paths.append(path)
    return paths


def ensure_clean(repo: str | Path) -> None:
    """Require a Git work tree with no changes outside tool runtime state."""
    code, stdout, stderr = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if code or stdout.strip() != "true":
        argv = ["git", "rev-parse", "--is-inside-work-tree"]
        raise GitError(
            "The target path is not inside a Git work tree",
            argv=argv,
            exit_code=code,
            stderr=stderr,
        )
    dirty = _status_paths(repo)
    if dirty:
        argv = ["git", "status", "--porcelain", "--untracked-files=all"]
        raise DirtyWorktreeError(
            "Refusing to start with a dirty work tree: " + ", ".join(dirty),
            argv=argv,
            exit_code=0,
            stderr="",
        )


def base_branch(repo: str | Path, requested: str | None = None) -> str:
    """Return the requested local base branch or the branch checked out now."""
    current_code, current, current_stderr = _git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if current_code:
        argv = ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]
        raise DetachedHeadError(
            "Cannot determine the base branch from a detached HEAD",
            argv=argv,
            exit_code=current_code,
            stderr=current_stderr,
        )
    if requested is None:
        return current.strip()

    _checked(repo, ["check-ref-format", "--branch", requested])
    code, _stdout, stderr = _git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{requested}"])
    if code:
        argv = ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{requested}"]
        raise GitError(
            f"Base branch {requested!r} does not exist as a local branch",
            argv=argv,
            exit_code=code,
            stderr=stderr,
        )
    return requested


class GitOps:
    """Stateful Git operations for a single target repository."""

    def __init__(self, repo: str | Path, base: str | None = None) -> None:
        self.repo = Path(repo).expanduser().resolve()
        ensure_clean(self.repo)
        self.base = base_branch(self.repo, base)
        self.current_issue_id: int | str | None = None
        self.current_issue_title = ""

    def start_issue(self, issue_id: int | str, title: str | None = None) -> str:
        branch = f"chainlink/{issue_id}"
        _checked(self.repo, ["check-ref-format", "--branch", branch])
        code, _stdout, stderr = _git(
            self.repo,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        )
        if code == 0:
            _checked(self.repo, ["checkout", branch])
        elif code == 1:
            _checked(self.repo, ["checkout", "-b", branch, self.base])
        else:
            argv = ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            raise GitError(
                f"Could not check for issue branch {branch!r}",
                argv=argv,
                exit_code=code,
                stderr=stderr,
            )
        self.current_issue_id = issue_id
        if title is not None:
            self.current_issue_title = title
        return branch

    def has_changes(self, base: str | None = None) -> bool:
        base_ref = base or self.base
        commit_count = _checked(self.repo, ["rev-list", "--count", f"{base_ref}..HEAD"]).strip()
        if commit_count != "0":
            return True
        changed_paths = _checked(self.repo, ["diff", "--name-only", base_ref]).splitlines()
        if any(not _tool_runtime_path(path) for path in changed_paths):
            return True
        return bool(_status_paths(self.repo))

    def _working_changes(self) -> bool:
        return bool(_status_paths(self.repo))

    def commit_all(self, message: str) -> bool:
        _checked(
            self.repo,
            ["add", "-A", "--", ".", ":(exclude).chainlink-loop"],
        )
        code, _stdout, stderr = _git(self.repo, ["diff", "--cached", "--quiet"])
        if code == 0:
            return False
        if code != 1:
            argv = ["git", "diff", "--cached", "--quiet"]
            raise GitError(
                "Could not inspect staged Git changes",
                argv=argv,
                exit_code=code,
                stderr=stderr,
            )
        _checked(self.repo, ["commit", "-m", message])
        return True

    def finish_done(
        self,
        issue_id: int | str,
        merge: bool = True,
        title: str | None = None,
    ) -> str:
        branch = f"chainlink/{issue_id}"
        issue_title = title or self.current_issue_title or str(issue_id)
        if self._working_changes():
            self.commit_all(f"chainlink #{issue_id}: {issue_title}")
        _checked(self.repo, ["checkout", self.base])
        if not merge:
            self.current_issue_id = None
            self.current_issue_title = ""
            return branch
        merge_args = ["merge", "--no-ff", "--no-edit", branch]
        code, _stdout, stderr = _git(self.repo, merge_args)
        if code:
            _git(self.repo, ["merge", "--abort"])
            raise MergeConflictError(
                f"Could not merge {branch!r} into {self.base!r}; merge was aborted",
                argv=["git", *merge_args],
                exit_code=code,
                stderr=stderr,
            )
        self.current_issue_id = None
        self.current_issue_title = ""
        return branch

    def finish_no_changes(self, issue_id: int | str) -> str:
        """Return to base while preserving an unchanged issue branch."""
        branch = f"chainlink/{issue_id}"
        if self._working_changes():
            raise DirtyWorktreeError(
                "Cannot finish an approved-no-changes issue with working-tree changes",
                argv=["git", "status", "--porcelain", "--untracked-files=all"],
                exit_code=0,
                stderr="",
            )
        _checked(self.repo, ["checkout", self.base])
        self.current_issue_id = None
        self.current_issue_title = ""
        return branch

    def finish_failed(self, issue_id: int | str, outcome: str) -> str:
        branch = f"chainlink/{issue_id}"
        if self._working_changes():
            self.commit_all(f"WIP chainlink #{issue_id} ({outcome})")
        _checked(self.repo, ["checkout", self.base])
        self.current_issue_id = None
        self.current_issue_title = ""
        return branch

    def branch_head(self, branch: str) -> str:
        """Return the commit currently referenced by a local branch."""
        return _checked(self.repo, ["rev-parse", branch]).strip()

# chainlink-codex-loop

`chainlink-codex-loop` is a deterministic driver that picks issues from a Chainlink database and runs Codex against a separate target Git repository. Python code chooses issues, counts attempts, runs verification, validates reviewer verdicts, records outcomes, and decides when to stop. Codex does the implementation and review work.

The loop uses `codex exec` in non-interactive mode. It does not integrate with the Codex TUI, schedule timer prompts, or ask a model to choose the next issue. The project uses only the Python standard library.

## Requirements and install

Use Python 3.10 or newer, Git, Codex CLI, and Chainlink CLI. The workspace lock uses `fcntl.flock`, so run the loop on Linux or macOS. The target must be a Git work tree with a Chainlink database at `.chainlink/issues.db`.

From the checkout root, add the executable shim to `PATH`:

```sh
export PATH="$PWD:$PATH"
chainlink-codex-loop --help
```

There is no package installation step. Keep `chainlink-codex-loop` and the `chainlink_codex_loop/` package together on disk. In another terminal or shell, point `PATH` at this checkout as well.

Check the required tools:

```sh
python3 --version
git --version
codex --version
chainlink --version
```

## Quick start

Set `TARGET_REPO` to the absolute path of the repository whose issues Codex should work on. It must already have a Chainlink database and a clean Git work tree for a real run.

Preview one issue first. Dry-run checks preconditions and selects issues, but does not start Codex, create branches, claim issue locks, write comments, close issues, or create `.chainlink-loop/` state.

```sh
chainlink-codex-loop --repo "$TARGET_REPO" --dry-run 123
```

Then run that issue:

```sh
chainlink-codex-loop --repo "$TARGET_REPO" 123
```

Run several explicit IDs in the order given, with a verification command:

```sh
chainlink-codex-loop --repo "$TARGET_REPO" --attempts 3 --verify 'python3 -m unittest discover -s tests' 123 124
```

Select up to five ready issues with filters. Preview before removing `--dry-run` to start work:

```sh
chainlink-codex-loop --repo "$TARGET_REPO" --backlog --label backend --priority high --max-issues 5 --dry-run
chainlink-codex-loop --repo "$TARGET_REPO" --backlog --label backend --priority high --max-issues 5
```

Backlog mode refreshes Chainlink's ready list after each issue, so closing one issue can make a blocked issue eligible later in the same run. Normal runs skip issues locked by another agent and do not retry an issue already attempted during the current run. Dry-run does not query remote issue locks: Chainlink 0.2.0's lock listing may fetch and write its Git-backed lock cache. A preview can therefore include an issue that another agent currently holds.

## Command-line reference

This is the flag set printed by `chainlink-codex-loop --help`:

| Argument | Meaning |
| --- | --- |
| `ID ...` | Explicit issue IDs, processed in the given order. Missing, closed, or locked issues are skipped. |
| `--repo DIR` | Target Git repository. Defaults to the current directory. |
| `--backlog` | Select currently ready issues instead of explicit IDs. Cannot be combined with IDs. |
| `--label L` | In backlog mode, keep issues with this label. |
| `--priority P` | In backlog mode, keep `critical`, `high`, `medium`, or `low` issues. |
| `--max-issues N` | Stop after selecting at most N issues. Applies to explicit and backlog selection. |
| `--attempts N` | Maximum worker/verify/review rounds per issue. Defaults to 3. |
| `--verify CMD` | Run this shell command in the target repo after each worker turn. A nonzero result is sent back to that issue's worker session. |
| `--base BRANCH` | Local base branch. Defaults to the branch checked out when the run starts. |
| `--no-merge` | Commit approved changes on the issue branch but do not merge them into the base branch. A `done` issue is still recorded and closed. |
| `--model M` | Codex model for worker turns. Defaults to the Codex CLI configuration. |
| `--reviewer-model M` | Codex model for fresh reviewer turns. Defaults to the Codex CLI configuration. |
| `--timeout SECONDS` | Timeout for Codex and verification commands. Defaults to no timeout. |
| `--dry-run` | Perform read-only preflight and selection, then print the proposed branch and worker/reviewer argv. Dirty trees are reported as warnings. |
| `-h`, `--help` | Print the flag list and exit. |

Run `chainlink-codex-loop --help` for the concise parser output. Invalid usage and preflight errors return exit code 2.

## How an issue is handled

For each selected issue, the loop claims a Chainlink issue lock when that feature is available, sets the Chainlink session work item, and creates or checks out `chainlink/<id>`. The worker runs with Codex's `workspace-write` sandbox. If `--verify` is set, the command runs next. A nonzero result is sent to the same worker session and starts another round without a reviewer. After verification passes (or when verification is unset), a fresh reviewer runs with the `read-only` sandbox and the verdict JSON schema. Findings go back to that worker session, up to `--attempts` rounds.

An acquired issue lock is released in a `finally` path for every outcome. If Chainlink explicitly reports that lock support is unavailable, the loop logs a warning and continues without that issue lock. If backlog selection cannot read the lock list, it skips that issue rather than risk duplicate work. One workspace lock, `.chainlink-loop/lock`, prevents a second loop process from using the same target at the same time; the operating system releases the `flock` when its process exits.

| Outcome | Chainlink issue | Git branch |
| --- | --- | --- |
| `done` | Add a resolution comment and close the issue after reviewer approval. | Commit loop changes on `chainlink/<id>`, then merge to the base unless `--no-merge` was set. With `--no-merge`, the issue still closes and the branch remains unmerged. |
| `approved-no-changes` | Leave open with an observation comment for a human to decide whether to close. | Return to the base branch; keep the issue branch. |
| `verify-failed` | Leave open with a blocker comment containing the reason and verification output. | Save remaining changes as a WIP commit when needed, then return to the base branch. |
| `escalated` | Leave open with a blocker comment containing the reason and reviewer findings when available. | Save remaining changes as a WIP commit when needed, then return to the base branch. |

Only `done` closes an issue. A merge failure downgrades the result to `escalated` and leaves the issue open. A failure while recording a comment is logged and does not stop backlog processing.

## State, logs, and resuming work

Runtime data lives under the target repository's `.chainlink-loop/` directory. On first real run, the loop adds it to that repository's private `.git/info/exclude` file.

```text
.chainlink-loop/
  lock
  state.json
  logs/<issue-id>/
    worker-<attempt>.jsonl
    review-<attempt>.jsonl
    verdict-<attempt>.json
    verify-<attempt>.log
```

`state.json` is atomically replaced at run start and after each transition. It records the run configuration, active issue and phase, attempt, worker thread ID, and completed outcomes with reasons. The console prints transition lines and a summary table. Interrupted runs stop the Codex child process group, save failed work on its issue branch, return to the base branch, release locks, print the summary, and exit 130. A second interrupt exits immediately.

The blocker comment and state record include the worker thread ID. To continue a thread interactively from the target repository, use the Codex CLI resume command:

```sh
codex resume THREAD_ID
```

The loop itself uses the non-interactive `codex exec resume THREAD_ID` form so findings and verification output return to the existing worker session.

Exit codes are:

| Code | Meaning |
| ---: | --- |
| `0` | Every attempted issue ended `done` or `approved-no-changes`, or nothing was eligible. |
| `1` | At least one issue ended `verify-failed` or `escalated`. |
| `2` | Invalid usage or a preflight/setup error, such as a dirty tree or held workspace lock. |
| `130` | The run was interrupted by SIGINT or SIGTERM. |

## Safety notes

- A normal run requires a clean target work tree. Fix or commit existing changes before starting. Dry-run permits a dirty tree and prints a warning.
- Each issue gets a `chainlink/<id>` branch. The loop commits work on that branch; it does not commit directly to the base branch.
- `--no-merge` still closes a reviewer-approved `done` issue. The committed branch remains available for a later manual merge.
- Worker commands run with Codex `workspace-write`; reviewer commands run with `read-only`.
- Only one loop can hold the target workspace lock. Chainlink's per-issue lock is separate and may be unavailable when the target has no agent identity or lock backend.
- The control flow is ordinary Python. The model chooses code changes and review findings, not which issue the loop selects, whether findings count as approval, or whether attempts remain.

## Troubleshooting

- **Dirty work tree:** normal runs return 2 and list the dirty paths. Commit or stash those changes. Use dry-run to inspect selection without modifying the target.
- **Workspace lock held:** another loop owns `.chainlink-loop/lock`. Let it finish. A stale lock file without a live `flock` holder does not prevent a new run.
- **Chainlink lock warnings:** Chainlink can print warnings to stderr even when JSON is enabled. The adapter parses JSON from stdout only. If lock claiming is explicitly unavailable, the loop continues without an issue lock. If backlog selection cannot query lock ownership, it skips that issue and still uses the workspace lock for work it does start.
- **`verify-failed`:** the issue remains open, and the branch is saved with any WIP changes. The blocker comment and `verify-<attempt>.log` show the command result. Fix the work or verify command and run the issue again.
- **`escalated`:** inspect the blocker comment, state reason, worker thread ID, and attempt logs. Resume the worker manually with `codex resume THREAD_ID` or run the loop again after addressing the blocker.
- **Smoke-test behavior:** `tests/smoke/run.sh` uses a temporary repo and real Codex/Chainlink binaries; it spends model tokens. An optional `CODEX_LOOP_SMOKE_MODEL` selects the worker and reviewer model. The smoke scenario deliberately makes issue #2's verification fail on its branch, so the loop returns 1 while the smoke script itself exits 0 after validating the expected outcomes.

Run the full fake-based suite with:

```sh
python3 -m unittest discover -s tests
```

Run the real smoke test with a selected model using:

```sh
CODEX_LOOP_SMOKE_MODEL=gpt-6-luna ./tests/smoke/run.sh
```

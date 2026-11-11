You are implementing `chainlink-codex-loop` in this repository. The repo is empty apart from git and a Chainlink issue database holding a 12-issue backlog. Work through that backlog until it is empty.

## What you are building

A Python 3.10+ script, standard library only, that automates OpenAI Codex over Chainlink issues in some other target repository. For each issue it runs a Codex worker (`codex exec`), an optional verify command, and a fresh read-only Codex reviewer that returns a structured verdict; reviewer findings go back to the same worker session with `codex exec resume`, up to an attempt limit. Plain code picks the issues, counts attempts and decides when to stop. Each issue's description holds the detailed spec, including CLI facts already verified against `codex-cli 0.166.0` and `chainlink 0.2.0`.

Chainlink is the issue tracker CLI (`chainlink --help`). In this task it plays two roles: it tracks your backlog in this repo, and it is one of the two tools your script drives. Keep them apart: your backlog lives in this repo's `.chainlink/`; anything your code or tests do with Chainlink must happen in temporary scratch repos.

## How to work the backlog

Repeat until `chainlink issue ready --json` returns an empty list:

1. Pick the issue: `chainlink issue next --json`. Blocking relationships are already set, so only take issues that are ready. Do not reorder or skip ahead.
2. Read it in full: `chainlink issue show <id> --json`, including comments left on earlier issues it depends on.
3. Start it: `chainlink session work <id>`.
4. Implement everything under "Work" and satisfy every item under "Acceptance criteria". Where an issue says to check real CLI behaviour, run the real command and build on what you observe; flag names and JSON shapes not listed in the issue are unverified.
5. Run the whole test suite: `python3 -m unittest discover -s tests`. It must pass before you go on.
6. Commit with the message `#<id>: <issue title>`. One commit or more per issue, never one commit spanning several issues.
7. Record what you did: `chainlink issue comment <id> "<text>" --kind result`. Include what was built, anything that differs from the issue text and why, and any manual check the acceptance criteria ask to have recorded.
8. Close it: `chainlink issue close <id>`.

Chainlink prints WARN lines to stderr even with `--json`; read stdout only.

## Rules

- Close an issue only when every acceptance criterion is met and the tests pass. If you cannot meet one, leave the issue open, add a `--kind blocker` comment that says exactly what is unmet and what you tried, and move to another ready issue if one exists. If nothing is ready, stop and report.
- When an issue's spec turns out to be wrong (a flag does not exist, an event has a different shape), follow the real behaviour, keep the issue's intent, and write the difference in a `--kind decision` comment. Later issues were written from the same assumptions, so carry the correction forward.
- If you find work that no issue covers, create an issue for it (`chainlink issue create "<title>" -d "<description>" -p <priority>`, then `chainlink issue block <id> <blocker>` as needed) so it is tracked, then work it in order.
- Standard library only. No third-party packages, no `pip install`.
- Work on the `main` branch of this repo. Do not push; there is no remote.
- Do not modify or run anything that writes inside `<existing-project>` (a separate real project with its own Chainlink backlog). Issue 10 asks for one `--dry-run` against it, which must leave it untouched; that is the only contact allowed. All other real runs of the tool use throwaway repos under a temp directory.
- Real `codex exec` calls cost tokens. Make the few the issues ask for (recording fixtures in issue 3, the schema check in issue 4, the smoke test in issue 11) and use the fake binaries for everything else.
- Do not edit the description of an existing issue; add comments.

## When you finish

Stop when no ready issues remain. Report: each issue's id and final status, the test suite result, every deviation from the written specs, anything left open with its blocker, and the exact command to run the smoke test.

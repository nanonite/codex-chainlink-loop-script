#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/chainlink-codex-loop-smoke.XXXXXX")"
repo="$scratch/repo"
chainlink_log="$scratch/chainlink-stderr.log"
loop_log="$scratch/loop-transcript.log"

chainlink_in_repo() {
  (cd "$repo" && chainlink "$@")
}

cleanup() {
  status=$?
  if [[ "$status" -eq 0 && "${KEEP_SMOKE_REPO:-0}" != "1" ]]; then
    rm -rf -- "$scratch"
  else
    printf 'Scratch repo retained at %s\n' "$repo" >&2
  fi
}
trap cleanup EXIT

die() {
  printf 'SMOKE FAIL: %s\n' "$*" >&2
  exit 1
}

command -v git >/dev/null || die "git is required"
command -v chainlink >/dev/null || die "chainlink is required"
command -v codex >/dev/null || die "codex is required"

mkdir -p "$repo"
git -C "$repo" init -b main >/dev/null
git -C "$repo" config user.name "Chainlink Codex Loop Smoke"
git -C "$repo" config user.email "chainlink-codex-loop-smoke@example.invalid"
git -C "$repo" config gc.auto 0
git -C "$repo" config maintenance.auto false

chainlink_in_repo init --no-hooks >"$scratch/chainlink-init.log" 2>>"$chainlink_log"
chainlink_in_repo agent init "codex-loop-smoke" >"$scratch/agent-init.log" 2>>"$chainlink_log"

cat >"$repo/.gitignore" <<'EOF'
.chainlink/
EOF
cat >"$repo/app.py" <<'EOF'
def add(left, right):
    return left + right
EOF
cat >"$repo/test_app.py" <<'EOF'
import unittest

from app import add


class AppTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
EOF
git -C "$repo" add .gitignore app.py test_app.py
git -C "$repo" commit -m "smoke: initialize app"

create_issue() {
  local title="$1"
  local description="$2"
  local priority="$3"
  local output
  output="$(chainlink_in_repo issue create "$title" --description "$description" --priority "$priority" 2>>"$chainlink_log")"
  printf '%s\n' "$output" >>"$scratch/created-issues.log"
  local id
  id="$(printf '%s\n' "$output" | sed -nE 's/.*#([0-9]+).*/\1/p' | tail -n 1)"
  if [[ -z "$id" ]]; then
    die "could not parse issue ID from: $output"
  fi
  printf '%s\n' "$id"
}

done_id="$(create_issue \
  "Add a subtraction helper" \
  $'Implement subtract(left, right) in app.py and add unittest coverage in test_app.py. Acceptance criteria: subtract(9, 4) returns 5, subtract(4, 9) returns -5, and python3 -m unittest -q passes.' \
  high)"
verify_id="$(create_issue \
  "Add a multiply helper with a deliberately failing verification" \
  $'Implement multiply(left, right) in app.py and add unittest coverage in test_app.py. Also create verify_attempt_marker.txt containing a short note. Do not edit the externally supplied verification command. Acceptance criteria: multiply(6, 7) returns 42 and python3 -m unittest -q passes; the command-level smoke deliberately keeps this issue open as verify-failed.' \
  high)"
unblocked_id="$(create_issue \
  "Add a square helper after subtraction closes" \
  $'Implement square(value) in app.py and add unittest coverage in test_app.py. This issue is blocked by the subtraction issue and should be picked up by backlog selection only after that blocker closes.' \
  low)"

chainlink_in_repo issue block "$unblocked_id" "$done_id" >"$scratch/block.log" 2>>"$chainlink_log"
chainlink_in_repo issue ready --json >"$scratch/ready-before.json" 2>>"$chainlink_log"
python3 - "$scratch/ready-before.json" "$done_id" "$verify_id" "$unblocked_id" <<'PY'
import json
import sys

ready = {str(issue["id"]) for issue in json.load(open(sys.argv[1], encoding="utf-8"))}
done_id, verify_id, unblocked_id = sys.argv[2:]
assert done_id in ready, f"first issue is not ready: {ready}"
assert verify_id in ready, f"verification issue is not initially ready: {ready}"
assert unblocked_id not in ready, f"blocked issue was ready before its blocker closed: {ready}"
PY

verify_command="python3 -c 'import subprocess,sys; branch=subprocess.check_output([\"git\",\"branch\",\"--show-current\"],text=True).strip(); failing=branch == \"chainlink/${verify_id}\"; print(\"intentional verification failure\" if failing else \"running unittest\"); sys.exit(1 if failing else subprocess.call([\"python3\",\"-m\",\"unittest\",\"-q\"]))'"
args=("--repo" "$repo" "--base" "main" "--backlog" "--attempts" "2" "--verify" "$verify_command")
if [[ -n "${CODEX_LOOP_SMOKE_MODEL:-}" ]]; then
  args+=("--model" "$CODEX_LOOP_SMOKE_MODEL" "--reviewer-model" "$CODEX_LOOP_SMOKE_MODEL")
fi

printf 'Smoke scratch repo: %s\n' "$repo"
printf 'Issue IDs: done=%s verify-failed=%s newly-unblocked=%s\n' "$done_id" "$verify_id" "$unblocked_id"
printf 'Verify command: %s\n' "$verify_command"
printf 'Running:'
printf ' %q' "$root/chainlink-codex-loop" "${args[@]}"
printf '\n'

set +e
"$root/chainlink-codex-loop" "${args[@]}" >"$loop_log" 2>&1
loop_status=$?
set -e
if [[ "$loop_status" -ne 1 ]]; then
  cat "$loop_log"
  die "loop returned $loop_status; expected 1 for one verify-failed issue"
fi

show_issue() {
  local id="$1"
  chainlink_in_repo issue show "$id" --json >"$scratch/issue-$id.json" 2>>"$chainlink_log"
}
show_issue "$done_id"
show_issue "$verify_id"
show_issue "$unblocked_id"

python3 - "$scratch" "$done_id" "$verify_id" "$unblocked_id" <<'PY'
import json
import pathlib
import sys

scratch = pathlib.Path(sys.argv[1])
done_id, verify_id, unblocked_id = sys.argv[2:]
done_issue, failed_issue, unblocked_issue = [
    json.loads((scratch / f"issue-{issue_id}.json").read_text(encoding="utf-8"))
    for issue_id in (done_id, verify_id, unblocked_id)
]
assert done_issue["status"] == "closed", done_issue["status"]
assert failed_issue["status"] == "open", failed_issue["status"]
assert unblocked_issue["status"] == "closed", unblocked_issue["status"]
assert any(comment.get("kind") == "resolution" for comment in done_issue.get("comments", []))
assert any(comment.get("kind") == "blocker" for comment in failed_issue.get("comments", []))
assert any(comment.get("kind") == "resolution" for comment in unblocked_issue.get("comments", []))

state = json.loads((scratch / "repo" / ".chainlink-loop" / "state.json").read_text(encoding="utf-8"))
outcomes = {str(item["id"]): item["outcome"] for item in state["finished_issues"]}
assert outcomes.get(done_id) == "done", outcomes
assert outcomes.get(verify_id) == "verify-failed", outcomes
assert outcomes.get(unblocked_id) == "done", outcomes
assert [str(item["id"]) for item in state["finished_issues"]].index(done_id) < [
    str(item["id"]) for item in state["finished_issues"]
].index(unblocked_id), "blocked issue was not run after its blocker"

logs = scratch / "repo" / ".chainlink-loop" / "logs"
for issue_id in (done_id, verify_id, unblocked_id):
    assert (logs / issue_id / "worker-1.jsonl").is_file(), issue_id
for issue_id in (done_id, unblocked_id):
    assert (logs / issue_id / "review-1.jsonl").is_file(), issue_id
    assert (logs / issue_id / "verdict-1.json").is_file(), issue_id
for attempt in (1, 2):
    assert (logs / verify_id / f"verify-{attempt}.log").is_file(), verify_id
assert (logs / verify_id / "worker-2.jsonl").is_file(), verify_id
PY

if ! git -C "$repo" show-ref --verify --quiet "refs/heads/chainlink/$done_id"; then
  die "done issue branch was not retained"
fi
if ! git -C "$repo" show-ref --verify --quiet "refs/heads/chainlink/$verify_id"; then
  die "verify-failed issue branch was not retained"
fi
if ! git -C "$repo" show-ref --verify --quiet "refs/heads/chainlink/$unblocked_id"; then
  die "newly unblocked issue branch was not retained"
fi
if git -C "$repo" merge-base --is-ancestor "chainlink/$verify_id" main; then
  die "verify-failed issue branch was merged into main"
fi
if git -C "$repo" log main --format=%s | grep -F "WIP chainlink #$verify_id (verify-failed)" >/dev/null; then
  die "verify-failed WIP commit leaked into main"
fi
if ! git -C "$repo" merge-base --is-ancestor "chainlink/$done_id" main; then
  die "done issue branch is not merged into main"
fi
if ! git -C "$repo" merge-base --is-ancestor "chainlink/$unblocked_id" main; then
  die "newly unblocked issue branch is not merged into main"
fi
if [[ "$(git -C "$repo" branch --show-current)" != "main" ]]; then
  die "loop did not return to the base branch"
fi

printf '\n===== loop transcript =====\n'
cat "$loop_log"
printf '\n===== Chainlink stderr (warnings only; JSON was read from stdout) =====\n'
cat "$chainlink_log"
printf '\n===== main git log =====\n'
git -C "$repo" log --oneline main
printf '\n===== smoke assertions =====\n'
printf 'done issue #%s closed and merged\n' "$done_id"
printf 'verify issue #%s remains open with a blocker and unmerged WIP\n' "$verify_id"
printf 'blocked issue #%s was initially absent from ready and completed after #%s closed\n' "$unblocked_id" "$done_id"
printf 'state.json and worker/reviewer/verdict/verify logs are present\n'
printf 'SMOKE PASS (loop exit %s)\n' "$loop_status"

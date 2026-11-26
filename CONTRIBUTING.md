# Contributing to test-ratchet

Issues and pull requests are welcome. For a defect, include the workflow, the
sub-action's inputs and what it reported.

## LLM and agent contributions are welcome

You may use an LLM or coding agent. There is no separate review queue or
expectation that you rewrite its output by hand. Two conditions apply:

1. Disclose the serving model in each commit it authored:

   ```
   Co-Authored-By: <Model Name> <noreply@example.com>
   ```

   Use the plain model name, without a context-window suffix. Inspect all
   outgoing commits, not only the tip:

   ```bash
   git log --format='%h %(trailers:key=Co-Authored-By,valueonly)' origin/main..HEAD
   ```

2. Verify every behavioral claim. Paste the command, actual output and exit
   status.

## A new file is invisible until you name it

`.gitignore` denies by default and names back the files this repository
ships. Reopen a directory before allowing files inside it:

```
*
!tests/
tests/*
!tests/test_aggregate_gate.py
```

After changing ignore rules, seed a junk file in each kept directory, prove
`git check-ignore -v` ignores it, and verify every intended file is tracked.

## Getting it running

Use Python 3.15. The suite needs pytest, installed from the hash-pinned
manifest:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --require-hashes --only-binary=:all: -r requirements-test.txt
python3 -m pytest tests/ -q --tb=short -ra
```

The [tests workflow](.github/workflows/tests.yml) also runs Ruff,
pycodestyle, pylint and pyright, and runs the suite through this
repository's own `plan`, `run` and `report` sub-actions. `report` gates
coverage on the floor recorded in `.github/ci-thresholds.json`, and when a
push to main measures coverage above the recorded measurement by more than
its hysteresis, it raises that record in an automated commit. The same
commit records the per-file test times in `.github/test-times.json`, which
`plan` and `run` read to balance the shards, when a file was added or
removed or a time fell outside its recorded range by more than 25% and 1 s.
The size records (`.github/size-baseline.json`) and the comment-share records
(`.github/comment-share.json`) are tightened on each push to main by the
[size baseline](.github/workflows/size-baseline.yml) and
[comment share](.github/workflows/comment-share.yml) workflows, each of which
commits any changed entry itself, or leaves it to the next push's run when
main has moved past the push it measured; a pull request need not carry them.

## Checks this repository does not run

Two checks the sibling repositories carry are deliberately absent, and
`tests.yml` names them at the top of its header:

- **Gate-base freshness.** A step could refuse a head whose base, `main`, has
  moved on since with a commit that touches a file a required check reads: the
  `require-ci-aggregate` ruleset leaves
  `strict_required_status_checks_policy` false, so a green head vouches only
  for the tree it checked out. Porting the sibling's version needs a
  hand-held required-context list and a derived-path reader, and the reader's
  own suite would raise the frozen `tests/` tree budget. A blanket "rebase
  before merge" would have the effect of setting that policy true, which the
  ruleset deliberately does not. Re-run the affected job on the rebased head
  instead.
- **Commit scope.** A step could refuse a subject whose scope names a
  workflow while its type is not `ci`. This guide has no scope table — the
  type alone carries the rule — so there is no scope rule for such a step to
  enforce.

## Commit messages

Every commit message follows Conventional Commits:

```
type(scope)!: subject
```

The type is one of `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `revert`, `style`, `test`; the scope and `!` are optional. A
breaking change carries `!`. Review checks that each type and `!` match the
change, and a wrong message is reworded before the merge.

Messages automation writes follow it too: Dependabot's are
`ci(deps): bump …` for actions and `build(deps): bump …` for the pip
manifests, and `report`'s is
`chore(ratchet): update the test-ratchet records`.

## Issues

Use the [issue form](.github/ISSUE_TEMPLATE/issue.md). Comment `/claim` on an
open unassigned issue before you start, then confirm your login appears in
its assignees.

## Pull requests

Keep changes small and single-purpose. Claim the issue first, name it in the
pull request's Related Issues and Pull Requests section, and follow the
[pull request template](.github/PULL_REQUEST_TEMPLATE.md). Author and commit
with an email linked to your GitHub account: the pull request gate checks that
every commit's author and committer resolve to you, or that the commit carries
a `Co-Authored-By:` trailer whose email does. File a second defect separately
rather than expanding the patch.

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

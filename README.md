# test-ratchet

test-ratchet is three composite GitHub Actions that run a
repository's tests in time-balanced shards with coverage, for Python, Node and
shell, and gate the coverage floor with a ratchet that only tightens.

## Sub-actions

| Sub-action | Role |
|---|---|
| `plan` | Split the test files into shards balanced by recorded run time. |
| `run` | Run one shard's tests with coverage measured and upload the result. |
| `report` | Combine the shards' coverage, gate it against a committed floor that only rises, and comment on the pull request. |

Each sub-action's inputs and outputs are documented in its own section, and
its job is in the install block below.

## Install

The `test` job below crosses the consumer's own `os` and `python` matrix with
`plan`'s shard list: `fromJSON(needs.plan.outputs.matrix).shard` is one key of
a crossed matrix, and `strategy.matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}`
takes the whole matrix when the shard is the only key. The `report` job runs
after every `test` job, also when one failed, and on a push to the default
branch it commits the raised records with the deploy key of the
`<default branch>-push` environment. `report` gates one cell: while several
cells run, name it with `cell`, as both `report` steps below do.

```yaml
name: tests
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.plan.outputs.matrix }}
    steps:
      - uses: actions/checkout@<sha>
        with:
          persist-credentials: false
      - id: plan
        uses: Nitjsefnie-Actions/test-ratchet/plan@96562a47e9b7fe74b55cc70a4636d1b5d7b542e2 # v2.4.2
        with:
          tests: tests/**/test_*.py
          times: .github/test-times.json
          shards: 4
  test:
    needs: plan
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.14", "3.15"]
        shard: ${{ fromJSON(needs.plan.outputs.matrix).shard }}
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@<sha>
        with:
          persist-credentials: false
      - uses: actions/setup-python@<sha>
        with:
          python-version: ${{ matrix.python }}
      - run: python -m pip install -r requirements.txt
      - uses: Nitjsefnie-Actions/test-ratchet/run@96562a47e9b7fe74b55cc70a4636d1b5d7b542e2 # v2.4.2
        with:
          runner: pytest
          tests: tests/**/test_*.py
          times: .github/test-times.json
          cell: ${{ matrix.os }}-py${{ matrix.python }}
          shard: ${{ matrix.shard }}
          source: src
  report:
    needs: test
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    environment: ${{ github.event_name == 'push' && github.ref_type == 'branch' && github.ref_name == github.event.repository.default_branch && format('{0}-push', github.event.repository.default_branch) || '' }}
    permissions:
      contents: read
      pull-requests: write
      actions: read
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: Nitjsefnie-Actions/test-ratchet/report@96562a47e9b7fe74b55cc70a4636d1b5d7b542e2 # v2.4.2
        with:
          github-token: ${{ github.token }}
          cell: ubuntu-latest-py3.15
          times: .github/test-times.json
          push-key: ${{ secrets.MASTER_PUSH_DEPLOY_KEY }}
```

A pull request from a fork runs with a token that cannot write to the base
repository, so its `report` job cannot comment. A second workflow,
`.github/workflows/test-ratchet-comment.yml`, comments for it once `tests`
completes. It runs `report` on the triggering run's artifacts, checks out the
default branch, and checks out and runs nothing from the pull request.

```yaml
name: test-ratchet comment
on:
  workflow_run:  # zizmor: ignore[dangerous-triggers] nothing from the pull request is checked out or run
    workflows: [tests]
    types: [completed]

permissions: {}

concurrency:
  group: test-ratchet-comment-${{ github.event.workflow_run.head_repository.full_name }}-${{ github.event.workflow_run.head_branch }}
  cancel-in-progress: true

jobs:
  comment:
    if: >-
      github.event.workflow_run.event == 'pull_request'
      && (github.event.workflow_run.head_repository.full_name != github.repository
      || github.event.workflow_run.actor.login == 'dependabot[bot]')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      actions: read
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: Nitjsefnie-Actions/test-ratchet/report@96562a47e9b7fe74b55cc70a4636d1b5d7b542e2 # v2.4.2
        with:
          github-token: ${{ github.token }}
          cell: ubuntu-latest-py3.15
          run-id: ${{ github.event.workflow_run.id }}
```

To make a fork's pull request fail on its coverage as a branch's does, a third
workflow runs `report` with `workflow-run-gate` once every `pull_request` run
of `tests` completes, on that run's artifacts, and with `records-only-rise`
refuses a pull request that lowers, removes or loosens a coverage record. It writes the
same comment, so it stands in for the comment workflow above. Its status lands
on the default branch's latest commit, not on the pull request's head: GitHub
reports a `workflow_run` run there, so the repository must carry the status to
the pull request's head itself before a ruleset can require it.

```yaml
name: test-ratchet gate
on:
  workflow_run:  # zizmor: ignore[dangerous-triggers] nothing from the pull request is checked out or run
    workflows: [tests]
    types: [completed]

permissions: {}

concurrency:
  group: test-ratchet-gate-${{ github.event.workflow_run.head_repository.full_name }}-${{ github.event.workflow_run.head_branch }}
  cancel-in-progress: true

jobs:
  gate:
    if: github.event.workflow_run.event == 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      actions: read
    steps:
      - uses: actions/checkout@<sha>
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: Nitjsefnie-Actions/test-ratchet/report@96562a47e9b7fe74b55cc70a4636d1b5d7b542e2 # v2.4.2
        with:
          github-token: ${{ github.token }}
          cell: ubuntu-latest-py3.15
          run-id: ${{ github.event.workflow_run.id }}
          workflow-run-gate: 'true'
          records-only-rise: 'true'
```

## plan

`plan` splits the repository's test files into shards of similar run time and
outputs the shard matrix. It only reads the repository and the times file, and
it needs a checkout, no setup step and no installed dependency.

Its job is the `plan` job of the install block above.

### Inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `tests` | yes | | Newline-separated glob patterns relative to the repository root; `**` matches any number of directories. |
| `times` | no | none | Path, relative to the repository root, of the committed times file. |
| `cell` | no | every cell in the times file | Newline-separated cell names to predict for. |
| `shards` | one of the two | | Number of shards, an integer of at least 1. |
| `max-minutes` | one of the two | | The longest predicted shard in minutes, a positive number. |

### Outputs

| Output | Meaning |
|---|---|
| `matrix` | Compact JSON `{"shard":["1/N","2/N",...]}`, usable as `strategy.matrix: ${{ fromJSON(...) }}` or as `fromJSON(...).shard`, one key of a crossed matrix. |
| `count` | N, the number of shards. |

### Split rules

- A pattern matches only a file git tracks at the checked-out commit, so
  dependency, virtual-environment and build directories never match. Paths are
  repository-relative POSIX; `\` in a pattern or a times-file key reads as `/`.
- N never exceeds the number of matched files or 256, GitHub's per-matrix job
  limit. When a cap lowers N, the log names the cap and the count that was
  needed.
- Files are sorted by path, then taken in descending predicted seconds (ties by
  path); each goes to the shard with the lowest running total (ties to the lower
  index). Each shard's list is sorted by path. The split is a pure function of
  the matched files, their git blob sizes, the times file and one cell, so every
  runner OS computes the same assignment.
- Predicted seconds in a cell: the median of the file's recorded seconds in
  that cell. A file without a record is predicted as its blob size times the
  median seconds-per-byte of the matched files recorded in that cell, each
  taken at its median. When no matched
  file is recorded in the cell, blob sizes alone set the relative weights. A
  record for a file that no longer matches is ignored. A prediction below 0.001
  (seconds, or bytes without records) counts as 0.001, so files recorded at
  `0.0` or empty files take one shard each instead of all landing in the first.
- `max-minutes`: N is the fewest shards for which, in every named cell with a
  recorded matched file, the largest predicted shard is at most that many
  minutes. A file predicted above the limit on its own gets a shard of its own
  and the log names it. When no named cell has a recorded matched file, N is 1
  and the log says so.
- The log lists each named cell's predicted total per shard.

### Times file

The times file is JSON, committed to the repository and read, never written,
by `plan` and `run`:

```json
{
  "schema_version": 2,
  "cells": {
    "ubuntu-latest-py3.15": {
      "tests/test_a.py": [12.5, 9.1],
      "tests/unit/test_b.py": [3.2]
    }
  }
}
```

A cell name is an opaque string the consumer chooses, for example its OS and
interpreter. Keys are repository-relative POSIX paths and values are lists of 1
to 10 recorded seconds, oldest first, each finite and non-negative. A file at
`schema_version` 1 holds one number per path instead, read as a one-value list;
`report` writes version 2, converting the whole file on its first write, and
`plan`, `run` and `report` read both. A release that predates `schema_version`
2 (v2.2.0 and earlier) refuses a version 2 file, so move the three pins
together, and never move one of them back to such a release alone. A `times`
path naming a missing file means no records, and the log says so once.

### Failure modes

Every failure exits with status 2 and prints one line naming the input, file or
value at fault:

- both `shards` and `max-minutes` are set, or neither is;
- `shards` is not an integer of at least 1, or `max-minutes` is not a positive
  finite number;
- `tests` is empty, or no pattern matches a tracked file (the line names the
  patterns that matched nothing; a pattern that matches nothing beside patterns
  that do only warns);
- the times file is not valid JSON of the shape above;
- the working directory is not inside a git repository, or git cannot be run.

## run

`run` runs one shard's test files with coverage measured and uploads the
result as an artifact. It runs in the consumer's job after the consumer's own
environment steps (language setup, dependency install, service containers),
which stay the consumer's: the action installs no interpreter and changes no
`PATH`, and it needs a checkout, because only files git tracks match. Without
`shard` it runs every matched file, so a repository with one cell uses `run`
alone and needs no `plan` job.

Its job is the `test` job of the install block above.

`tests` and `times` take what they take in `plan`, and `run` applies the same
split to its own cell, so the files a job runs are the ones `plan` assigned to
that shard.

### Inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `runner` | yes | | `pytest`, `vitest` or `node`. |
| `tests` | yes | | Newline-separated glob patterns of the test files, as in `plan`. |
| `times` | no | none | Path, relative to the repository root, of the committed times file, as in `plan`. |
| `cell` | no | `<runner OS>-<major.minor>` | One cell name. The version is that of the `python3` (or `python`) on `PATH` for `pytest` and of the `node` on `PATH` for `vitest` and `node`: `Linux-3.15`, `macOS-24.11`. |
| `shard` | no | every matched file | `i/N` from `plan`'s matrix. |
| `source` | no | the repository's own coverage configuration | Comma-separated sources whose coverage is measured: Python source roots for `pytest` (each becomes `--cov=<root>`), include globs for `vitest` (each becomes `--coverage.include=<glob>`). Every comma separates two entries, so write `a/**,b/**`, not a brace glob such as `{a,b}/**`. `node` refuses it: `javascript` narrows its report. |
| `javascript` | no | every file the report holds | Comma-separated glob patterns, as in `tests` and split as `source` is, of the tracked files `coverage-javascript.xml` keeps, under every runner: `vitest`'s own report and the V8 dumps below are both narrowed to them, so `src/**/*.js` leaves out every file outside `src/` that a test process loaded. It only removes files: a matched file the report does not hold is not added, unless `javascript-all-files` is `true`. |
| `javascript-code-only` | no | `false` | `true` leaves out of the lines converted from the V8 dumps below each line whose non-blank characters all belong to comments, under every runner (see JavaScript from V8 dumps); `vitest`'s own report is not filtered. `false` keeps every non-blank line. |
| `javascript-any-character` | no | `false` | `true` counts a line converted from the V8 dumps below with the most hits of the innermost ranges holding any of its characters, so a line whose first character lies in an untaken branch reads covered when the rest of it ran; `false` counts the innermost range holding its first non-blank character (see JavaScript from V8 dumps). |
| `javascript-all-files` | no | `false` | `true` also puts into `coverage-javascript.xml` each `.js`, `.mjs` or `.cjs` file `javascript` matches that the report does not hold, with every line the V8 dumps below would measure in a whole script at 0 hits, so the report measures the same files whether or not a test loads each one (see JavaScript from V8 dumps). It needs `javascript` set. |
| `javascript-data-lines` | no | none | A Python regular expression for the lines of a page that hold data, not code, such as `const DATA = .*;`: a V8 dump entry whose source is not found once in its tracked file is also placed when it is found exactly once after every line the expression matches whole is masked on both sides, so a page built over other data counts toward the tracked page's lines (see JavaScript from V8 dumps). Blanks around the input are dropped: match indentation with `\s*`. |
| `shell` | no | none | Newline-separated glob patterns, as in `tests`, of tracked bash scripts to measure when the tests run them. Linux only. |
| `args` | no | none | Extra arguments for the runner, split as shell words and placed after the action's own, so `-n 0` overrides `pytest`'s worker count. `node` refuses it. |
| `name` | no | `<cell>-<i>_<N>`, or `<cell>-all` | The artifact name suffix. |

### Outputs

| Output | Meaning |
|---|---|
| `name` | The uploaded artifact's name, `test-ratchet-<name>`. |
| `passed` | `true` or `false`: whether the tests passed and everything the run wrote could be read. |

The artifact is uploaded whether the tests passed or failed. The action's own
result is the test run's: when the tests failed, the step turns red after the
upload, with a message saying why.

### Cell and artifact name

- A cell is one name. `RUNNER_OS` (`Linux`, `macOS`, `Windows`) leads the
  default and the runtime's major and minor version follows. A `cell` of more
  than one line fails the action.
- The artifact name is `test-ratchet-` and the name, and every character of the
  name outside `A-Za-z0-9._-` becomes `_`: shard `1/4` of cell `Linux-3.15`
  uploads `test-ratchet-Linux-3.15-1_4`. The default name holds the cell and the
  shard, so the jobs of a crossed matrix upload distinct artifacts when their
  cells differ: the default cell holds the OS and the runtime version, and an
  own `cell` should hold whatever the matrix crosses. A `vitest` job and a
  `node` job on one OS and Node version get the same default cell, so give one
  of them its own `cell` or `name`.

### Artifact

| File | Content |
|---|---|
| `manifest.json` | `{"schema_version": 1, "runner": "pytest"\|"vitest"\|"node", "cell": "<cell>", "shard": "i/N" or null, "files": [...], "passed": true\|false}`. `files` is the shard's sorted repository-relative paths. Always present. |
| `junit.xml` | The runner's JUnit report, or the action's own under `node`, with a `file` attribute, a repository-relative path, set on every `<testcase>`. A case that matches none of the shard's files has none, and a warning names it. Absent when the runner wrote no report. |
| `coverage-python.xml` | Cobertura line coverage of Python files, from `pytest`. |
| `coverage-javascript.xml` | Cobertura line coverage of JavaScript files: `vitest`'s v8 report merged with the V8 dumps below, or the dumps alone under `pytest` and `node`, narrowed to the files `javascript` matches when it is set. |
| `coverage-shell.xml` | Cobertura line coverage of the scripts `shell` matches. |

Each Cobertura file has `<sources><source>.</source></sources>` and
repository-relative POSIX `filename`s with no leading `./`, whatever the runner
OS, so shards from different OSes merge. It holds line hits only, no branch
data. A file is written only for a language that was measured; a file outside
the repository is dropped. The reports are written when the tests fail too.

### pytest

- The action installs pytest, pytest-cov and pytest-xdist with everything they
  need, each pinned (`requirements-test.txt`, hash-checked, wheels only):
  pluggy, iniconfig, packaging, Pygments, coverage, execnet, and colorama on
  Windows. They go into the `python3`, else `python`, on `PATH`, replacing the
  versions of those packages it already held; CPython 3.11 through 3.15 is
  supported. That interpreter
  must accept `pip install`: set it up with `actions/setup-python`. A system
  Python marked externally managed refuses it, because installers should not
  install into such an interpreter's global context by default
  ([PEP 668](https://peps.python.org/pep-0668/)).
- It runs the shard's files with `-n auto`, `--cov=<root>` for each `source`
  entry (a bare `--cov` when `source` is empty), Cobertura XML and JUnit XML.
  The repository's own coverage configuration (omit, exclude) is kept, and so is
  its `branch` setting, though the Cobertura files report line hits only. Its
  `fail_under` is not applied: a shard's coverage is partial, and the floor
  belongs to `report`.
- The Python processes the tests start are measured too, under the conditions
  in Limits: the action turns on coverage's `subprocess` and `_exit` patches,
  which cover child processes (`multiprocessing` children included). A
  repository's `[run] omit` and `include` patterns name the same files in a
  child as in pytest, a relative one read against the repository root whatever
  the child's working directory. The step's own `COVERAGE_PROCESS_START`,
  `COVERAGE_FILE` and `COV_CORE_*` variables are removed, so a repository's own
  measuring hook does not run twice.
- A plain `unittest` test case is collected as pytest collects it, and the
  action adds no other collection rule.
- A shard runs exactly its files. A path or node id in `args` is ignored with a
  warning that names it, and `-k` narrows within the shard. Paths that the
  repository's `addopts` names are cut to the shard's files too.

### vitest

- The action runs the repository's own installed vitest, with v8 coverage
  restricted to the `source` globs, writing Cobertura XML and JUnit XML through
  `vitest.config.*` or `vite.config.*` at the repository root, which it wraps.
  It needs `node` on `PATH`, and `vitest` and `@vitest/coverage-v8` installed
  in the repository. When either is missing it fails and names the package.
- A shard runs exactly its files: each is given to vitest by exact path, in
  every mode the repository's own globs give it (a test, an in-source test, a
  type test); a file none of them holds runs as a test. The repository's
  coverage thresholds are dropped, because the floor belongs to `report`.

### node

- The action runs the shard's files with Node's built-in test runner,
  `node:test`, through its `run()` API, which takes each file by its path:
  `node --test <path>` would read a name such as `[x].test.mjs` as a glob. The
  files run one at a time, each in its own process, from the repository root.
  It needs Node 22.10 or later on `PATH`, the first whose `node:test` emits
  the `test:summary` event the action takes the run's status from, and nothing
  installed. A run that emits no such event fails, whatever its tests did.
- It writes the JUnit report itself, each case named with its file, because
  Node 22's own JUnit reporter names none. A case is a leaf test: a test with
  subtests (`t.test`) and a `describe` count only through their leaves, so a
  file's time is its leaves' time. A `describe` that fails with no test inside
  it reported is a case too. Its status is `node --test`'s: a failing `todo` test
  does not fail the shard, and a file that throws as it loads does.
- `coverage-javascript.xml` comes from the V8 dumps below alone, so it holds
  every tracked file a test process loaded, the test files included: narrow it
  with `javascript`. `source` and `args` are refused.

### JavaScript from V8 dumps

Every runner measures every Node process the tests start, through
`NODE_V8_COVERAGE`, which the action sets. Browser coverage joins them: the
action also sets `TEST_RATCHET_V8_DIR`, and the V8 coverage JSON that the tests
write into that directory is converted into the same JavaScript report. A dump
is a list of script entries, or an object whose `result` is one. An entry needs
a `url` and the V8 `functions` ranges. Puppeteer's entries are accepted when
the coverage was started with `includeRawScriptCoverage`. A dump in
`NODE_V8_COVERAGE` that is not JSON text in UTF-8 (an empty one, or one cut
short) is left out and named in a notice, and the others are measured; any
other dump that cannot be read, or whose JSON is not a dump, is unreadable
(see below).

- A `file:` URL maps to the tracked file at that path; one outside the
  repository or naming an untracked file is ignored silently. An `http:` or `https:` URL, with its query and fragment dropped
  and percent-encoding decoded, maps to the tracked file that shares the most
  trailing path segments with it, so `/static/js/app.js` picks `web/static/js/app.js`
  over `js/app.js`. When several files share equally many, the one whose whole
  path is the shared part wins; any other tie is skipped and the log names the
  URL and the files. A URL that matches no tracked file is skipped and named in
  the log. Every other URL (`node:` internals, `data:`, an empty URL) is ignored
  silently.
- An entry's offsets are read against the tracked file. An entry that carries
  its own source is used when the source equals the file or is found exactly
  once inside it (an inline `<script>` keeps the page's line numbers). Without
  a source, the script's ranges must span the whole file. With
  `javascript-data-lines` set, a source not found once is also used when it
  is found exactly once after each line the expression matches whole (Python's
  `re.fullmatch`, lines split at `\n`) is masked in both the source and the
  file, so a page built over other data (another one-line `const DATA = …;`)
  keeps the page's line numbers, a masked line counting the source's own hits;
  any other difference still skips it. An entry that fails all of these is
  skipped and named in the log, so transpiled code (a TypeScript loader,
  a bundle) is not mis-numbered. So is an entry whose tracked file is not UTF-8.
- A line with a non-blank character counts the hits of the innermost range
  holding the line's first non-blank character, so comment and brace lines are
  measurable lines. With `javascript-any-character: 'true'` it counts the most
  hits of the innermost ranges holding any of its characters, its indentation
  and comments included, so `} else if (x) {` whose `}` closes an untaken
  branch reads covered when its condition ran. Hits of several dumps add.
- With `javascript-code-only: 'true'`, a line whose non-blank characters all
  belong to comments, a hashbang line (`#!` opening the script) included, is
  left out. Strings, template literals and regular expressions are read as
  such, so a `//` inside one starts no comment. A line holding code keeps the
  hits it counts without the input, also when a comment shares it, and so does
  a brace line. Only the lines converted from the dumps are filtered, not
  `vitest`'s own report.
- The report holds the files a process loaded. A tracked JavaScript file that
  no process loaded is not in it, covered or not, and the dumps are not
  restricted by `source`. `javascript` restricts the whole report, these files
  and `vitest`'s alike. With `javascript-all-files: 'true'`, each `.js`,
  `.mjs` or `.cjs` file `javascript` matches that the report does not hold is
  added as one whole script, every line holding a non-blank character at 0
  hits, or with `javascript-code-only: 'true'` every line holding code; the log
  names any other matched file it leaves out, such as a page. A file whose every
  script the log names as skipped is not held, so it is added at 0 hits too.
  Shards merge their lines, so a file added by one shard and measured by
  another's `vitest` report, whose lines are statements, counts both line sets.

### Shell

With `shell` set, on Linux the action measures the bash scripts the patterns
match whenever the tests run them, directly or through Python or Node child
processes that inherit the environment. It sets `BASH_ENV` to a hook that logs
each command bash runs, and converts the logs into line hits of the matching
tracked scripts. Every such script is in `coverage-shell.xml`, a line no test
ran at 0 hits. A line is measured when bash reports a command on it. Comments,
blank lines, a line holding only `then`, `do`, `else`, `fi`, `done`, `esac`,
`;;`, a case pattern such as `a)`, a `}`, or a `(` or `)` that opens or closes
a `( )` group are not measured, and neither are the lines of a multi-line
command other than the one bash reports for it. A `{` alone on the line after
`g()` is measured, and the `g()` line is not; a `{` alone on a line that opens a
`{ }` group is not measured.

On another runner OS shell is not measured: the log says so once and the run
does not fail for it.

### Limits

Python:

- A Python child is measured when it runs an interpreter whose site-packages
  holds the coverage the action installed (the `python3`, else `python`, on
  `PATH`, or a venv that shares it), processes `site` (`python -S` is not
  measured, `-I` and `-E` are) and inherits the environment (a child started
  with a stripped environment is not measured). An unmeasured child's file still appears under
  `source` with its lines at 0, so it lowers the total instead of going missing.
- The forced coverage settings replace a repository's own `[run] patch` and
  reach coverage the tests start themselves. A repository that sets
  `concurrency = multiprocessing` loses its own configuration (an omit, for
  instance) in those children, which the patches already measure: do not set it.
- A `multiprocessing` pool worker ended by `terminate()`, which the exit of a
  `with Pool()` block does, is not measured, and coverage may warn about its
  data file. `close()` and `join()` measure the worker.
- The shard's files are the only ones pytest collects, except where repository
  code creates items from other files itself (its own `pytest_collect_file`). An
  option that only a conftest in a directory outside the shard defines (a
  directory named by `addopts`) is unknown once that directory is cut, and
  pytest exits 4: define such options in a conftest at the rootdir or on a
  shard file's path.

vitest, the action's selection following vitest 5.1, the version tested:

- A repository whose vitest config sets `test.projects` or `test.workspace` is
  refused. So is a root that `args` (`--root`, `-r`) or a plugin moved away from
  the one the config states: set `root` in the config. So is a `test.dir` that a
  plugin moved away from the repository root the action passes as `--dir`.
- A shard file that the repository's `exclude` holds does not run, as in plain
  vitest: the shard passes when other files ran, and with `passWithNoTests` even
  when none did.
- `includeSource` files must be named by `tests`, because an in-source file
  outside the shard does not run its in-source tests, even when a shard test
  imports it. A shard file under `includeSource` without `import.meta.vitest`
  fails with "No test suite found", so `tests` names only files that hold the
  marker, never all of `src/**`.
- With `typecheck` off, a type-test file is not run, as in plain vitest: a
  shard holding other files passes without it, and one holding only type tests
  fails with "No test files found" (`passWithNoTests` turns that into a pass).
  `--typecheck` in `args` turns the checks on, and `typecheck.only` skips
  runtime files.
- `args` cannot repeat `--config`, `--dir` or a coverage option the action
  sets: vitest refuses before running, and the failure names `args`. An `args`
  that leaves no shard file to run warns.
- A file named with a backslash cannot be loaded by vitest at all.

Shell:

- Bash 5.2 and 5.3 are modelled, each by the way it reports a line number; an
  older bash reads as 5.2 and a newer one as 5.3. A job that runs scripts under
  both loses the multi-line commands only the older bash ran, which read as
  uncovered.
- Only a bash that reads `BASH_ENV` is measured. A script run with a cleared
  environment (`env -i`) is not, and the log names that limit once; neither is a
  script run by `sh` (dash on Ubuntu), in POSIX mode or interactively, nor one
  run by `bash -v`. A `BASH_ENV` of the consumer's is
  read first, unless its value holds `$` or a backquote, which the hook cannot
  chain: it is replaced and a warning says so.
- The hook is visible to a measured script. `$-` gains `T` (`functrace` is on),
  `trap -p` lists a DEBUG trap, the variables named `_test_ratchet_*` are set, and
  a script's RETURN traps reach its functions. A script that sets its own DEBUG
  trap stops being measured from there, and `set +T` stops measurement inside
  functions and subshells. Under `set -x` with
  `BASH_XTRACEFD` the hook's commands appear in the trace, and concurrent
  pipeline processes may trace in another order.
- A script whose syntax the action's reader cannot follow past some line gets a
  warning naming the line, and from there on only the lines bash ran are
  counted.
- A line is credited only where its command is proven to have run. Some lines
  read uncovered that ran: an alias, a command made only of redirections that
  starts with a file descriptor (`2> f`, `>&2`), a command that bash prints
  differently from its source after an `eval`, a subshell-bodied function or a
  sourced file's top level that a trap runs, a `( )` group on the line of a
  command substitution that holds its first command, two relatively sourced
  files of one name and, under bash 5.3, a command that begins with a
  redirection as the first command of a function body. A line can read covered
  without having run only by coincidence: inside a function a trap runs, the
  records of an `eval` or a multi-line command substitution are credited to the
  lines below it unchecked, and elsewhere such a record counts on a line whose
  own command is printed the way it starts.

### Failure modes

These failures before the tests run, among others, exit with status 2; each
prints one line naming the input, file or value at fault, and uploads no
artifact:

- `runner` is none of `pytest`, `vitest` and `node`, `cell` holds more than one line, or
  `shard` is not `i/N` with 1 <= i <= N <= 256;
- `tests` is empty or no pattern matches a tracked file, or the times file is
  invalid (as in `plan`);
- on Linux, `shell` is set and no pattern matches a tracked file (a pattern that
  matches nothing beside patterns that do only warns);
- `javascript` is set and no pattern matches a tracked file, on any runner OS
  (a pattern that matches nothing beside patterns that do only warns);
- `javascript-code-only`, `javascript-any-character` or `javascript-all-files`
  is neither `true` nor `false`, or `javascript-all-files` is `true` with
  `javascript` empty;
- `javascript-data-lines` is not a valid Python regular expression;
- `args` cannot be split as shell words;
- `runner: vitest` without `node` on `PATH`, with a config the action refuses
  (see Limits), or with `args` that moved vitest's root;
- `runner: node` with `source` or `args` set, or without `node` on `PATH`.

`runner: vitest` with `vitest` or `@vitest/coverage-v8` not resolvable from the
repository exits 1 naming the package, and uploads no artifact. So does a
`pytest` run whose `pip install` of the toolchain fails (an interpreter that is
externally managed, or one the pinned wheels do not cover): the step ends with
pip's own message before any test runs.

Something the run left that cannot be read (a V8 dump that is not valid, other
than a Node one that is not JSON text, a coverage report or JUnit report that
is not valid XML, a script `shell` matches
that a test deleted, a script whose code lines `javascript-code-only` cannot
tell, a file `javascript-all-files` adds that is not UTF-8) does not end the
run. That input's scanner reads only the files the report keeps (those
`javascript` matches, when it is set), and refuses a script holding
any of these, naming the file and line: a block comment, template literal or
regular expression still open where the script ends; a string still open where
the script ends or at an unescaped `\n` or `\r`; a line terminator (`\n`, `\r`,
U+2028 or U+2029) inside a regular expression, escaped or not; a `/` after `}`,
or after a token it does not class as ending or preceding an expression (a
keyword such as `default` or `var`, or punctuation such as `...`); or a
backslash outside a string, template literal, regular expression or comment
(an escaped identifier). The artifact holds everything that could be read: that dump is
skipped, that report's file is not written, or that script (a file, or one
inline `<script>` of a page) adds no lines. When vitest's own coverage report is
the unreadable one, `coverage-javascript.xml` is still written from the V8 dumps
when they hold a tracked file. `passed` is `false`, and the step turns red after
the upload with a message that names each unreadable file.

A test run that fails uploads its artifact and turns the step red with one of
these messages, which comes first when a file was also unreadable, and `passed`
is `false`:

- `the tests failed: their reports are in the uploaded artifact`;
- `pytest exited 4, a usage or configuration error: check input args, the
  repository's pytest configuration and its conftest files`;
- `pytest exited 5, no tests were collected: check input args`;
- `vitest exited <code> before running: check input args, which cannot repeat
  --config, --dir or the coverage options the action sets, and the repository's
  vitest configuration`;
- `node exited <code> before running the shard's tests: check the node on PATH,
  which needs node:test's run(), and NODE_OPTIONS`.

## report

`report` downloads the `run` artifacts of one workflow run, merges their
coverage, gates it against the floors committed at the pull request's base,
comments on the pull request, and on a push to the default branch raises the
records that only rise. It needs a checkout with history (`fetch-depth: 0`),
because it reads the floors from the base commit with git, and it runs on the
`python3`, else `python`, on `PATH`, standard library only.

Its job is the `report` job of the install block above; the comment workflow
below the block runs it for fork pull requests, and the gate workflow after
that for every pull request.

### Inputs

| Input | Required | Default | Meaning |
|---|---|---|---|
| `github-token` | yes | | Downloads the artifacts and writes the comment. Pass `github.token`: the comment is found among `github-actions[bot]`'s comments. |
| `thresholds` | no | `.github/ci-thresholds.json` | Path of the thresholds file, relative to the repository root. |
| `times` | no | none | Path of `plan`'s times file, rewritten on the default branch. Without it times are left alone. |
| `cell` | no | the records' `cell`, else the only cell present | The one cell whose coverage is gated and ratcheted. |
| `base-ref` | no | `github.event.pull_request.base.sha` | The commit whose thresholds file holds the floors. Empty reads the checkout's file; on a `workflow_run` event, the pull request's base. |
| `run-id` | no | the current run | The workflow run whose `test-ratchet-*` artifacts are read. |
| `precision` | no | `1` | Decimals each language's coverage is shown and recorded at, a whole number from 1 to 6; above 1 the gate compares the exact coverage (see Gate). |
| `hysteresis` | no | `1.5` | Percentage points coverage must rise above the recorded `measured` before the record moves. The thresholds file's `hysteresis` key overrides it (see Records). |
| `gap` | no | `1.5` | Percentage points between a raised floor and the new `measured`. The thresholds file's `gap` key overrides it (see Records). |
| `workflow-run-gate` | no | `false` | `true` makes a `workflow_run` run whose triggering run was a `pull_request` run fail as that run's `report` does (see the fork section). |
| `records-only-rise` | no | `false` | `true` fails the step when the thresholds file lowers or removes a coverage record, gives a new or changed `measured` a floor below the one a raise would give, or raises, removes or adds a looser `gap` or `hysteresis` key (see Records only rise). |
| `gate-raised-floors` | no | `false` | `true` also gates each language whose floor the pull request raises or adds in its own thresholds file, by the higher of the base's floor and the head's (see Gate). |
| `comment-check` | no | none | The name of a check run that a `workflow_run` run whose triggering run was a `pull_request` run publishes on the pull request's head, concluding whether the comment was written. It needs `checks: write` (see Pull request comment). |
| `push-key` | no | none | SSH deploy key with write access, the `MASTER_PUSH_DEPLOY_KEY` secret of the `<default branch>-push` environment. Without it the new records go to the job summary. |

`report` has no outputs. Its results are the step's status, the job summary,
the pull request comment and, with `comment-check`, a check run.

### Merge

Of the artifacts of `run-id` named `test-ratchet-*`, whichever attempts of the
run uploaded them, only the one of each name with the latest `created_at` is
read: a shard that was not re-run, or whose re-run uploaded no artifact, is
read from the attempt that last uploaded it. They are downloaded into a new
directory under `RUNNER_TEMP` and read as data: nothing in them is imported or
run, and no symlink in them is followed. A download that fails or finds none
is repeated into the emptied directory, at most four times, after 5, 10, 20 and
40 s; a run with no artifact is reported as such only after that. Per language
(`python`, `javascript` for JavaScript and TypeScript, `shell`) the Cobertura
reports of the gated cell's artifacts are merged: a line is covered when any
shard covered it. An artifact whose tests failed is not merged. The job summary
and the log name every shard left out, a failed one and each `i/N` with no
artifact.

The gated cell is the `cell` input, else the `cell` the records name, else the
only cell the artifacts hold.

### Gate

With `precision` at its default of 1, each language's merged coverage is shown
as `coverage report --precision=1` shows a total: rounded to one decimal,
except that a total above 0 shows at least 0.1 and one below 100 at most 99.9,
and that figure is compared with the floor. With `precision` from 2 to 6 the
gate compares the exact coverage, 100 × covered / measurable lines, with the
floor, and the figure shown is that value truncated toward 0 to `precision`
decimals, with no clamp at either end: it never exceeds the coverage, and for
a floor with at most `precision` decimals it is at or above the floor exactly
when the gate passes. No measurable lines count as 100 at every precision. The
job summary, the pull request comment and the log show the figure. The floor
is `coverage.<language>.floor` in the thresholds file at `base-ref`, or in the
checkout when `base-ref` is empty. Below the floor fails the step, showing the
figure and the floor, except on a `workflow_run` event without
`workflow-run-gate`: that run only comments (see the fork section below). A
pull request that lowers a floor in its own thresholds file is still gated by
its base's, and with `records-only-rise` the lowering itself fails the step. A
pull request that raises a floor or adds a record is also gated by its base's,
unless `gate-raised-floors` is `true` (below). A
language with no record is reported and not gated; without a thresholds file
nothing is gated. A language with a record that no merged artifact measured
fails the gate the same way, shown as
`fail: recorded floor but nothing measured`: losing a report (a coverage option
in `args`, an unmatched `source`, every shard failing) never turns its gate off.

With `gate-raised-floors` set to `true`, on a `pull_request` run and on a
gating `workflow_run` run, the thresholds file at the pull request's head is
compared with the file at the merge base of its base and its head, read as
git objects the way Records only rise reads them: nothing from the pull
request is checked out or run. Each language whose head record is new, or
whose head floor is above the merge base's, is gated by the higher of the
base's floor and the head's, at the same `precision`; every other language is
gated by the base's floor. A branch that forked before the base raised,
lowered or removed a record is therefore not gated by the head's stale copy,
and a head floor at or below the merge base's changes nothing. A record the
head adds that no merged artifact measured fails as
`fail: recorded floor but nothing measured`. The failure line, the job summary
and the comment show such a floor as, for example, `96.0 from the head (base
93.5)`, naming the floor gated first. A head file that is not a valid
thresholds file is refused with status 2; a head without one raises nothing.
On every other event the input changes nothing.

Whenever the coverage could be measured, the job summary shows each language's
covered and measurable lines, its figure, its floor and the result, and the
shards left out, also when the step then fails for another reason, such as a
pull request the API cannot resolve.

### Records only rise

With `records-only-rise` set to `true`, the step fails with status 1 when the
thresholds file, compared with an earlier version of it:

- lowers a `coverage.<language>.floor` or `measured`, or removes a record of
  `python`, `javascript` or `shell`;
- holds a record that is new, or whose `measured` changed, with a `floor`
  below the one a raise would give that `measured`: `measured` minus G,
  rounded down to `precision` decimals and never below 0, G being the new
  file's `gap` key, or this run's `gap` input where the file has none;
- raises or removes a top-level `gap` or `hysteresis` key, or adds one above
  this run's input of that name.

The log has one line per finding naming the key and the values that decide
it: the old and new value of what is lowered or raised, what a removed record
or key held, an added key's value and the input it exceeds, and for a floor
below the one a raise would give, the old and new `measured` (`none` where a
record lacks one), the floor and that implied floor; the job summary lists
them. Lowering a key, an equal value, a key added at or below the input, and a
new or changed `measured` whose floor is at or above that floor pass. With no earlier file every record is new, and a file
the change deletes removes every record and key. When neither file holds a key
and the change adds no record and changes no `measured`, only a lowered or
removed record fails, as without these checks.

- On a `pull_request` event and on a gating `workflow_run` run, the file at the
  pull request's head is compared with the file at the merge base of its base
  and its head, so a branch that forked before a commit raised a record is not
  refused for it. The head's file is read as git objects: when the checkout
  lacks the head commit (a fork's, on a `workflow_run` run), it is fetched from
  the checkout's `origin` as `refs/pull/<number>/head` into no branch, with
  `github-token` passed through git's environment rather than its arguments,
  and it must be the run's head commit.
- On a push to the default branch, the file at `github.sha` is compared with
  the file at the push's previous tip; a push that created the branch has no
  previous tip and compares nothing. A failure moves no record.
- Any other run compares nothing.

A raise of the `gap` or `hysteresis` input itself is not checked: it is a
workflow change, not a thresholds file change. To have a raise refused, set
the value as the thresholds file's key, which overrides the input (see
Records).

With the input off a pull request may lower or remove a record; its own run is
still gated by its base's floors.

### Records

The thresholds file is the one this repository commits:

```json
{
  "schema_version": 1,
  "coverage": {
    "python": {"floor": 96.5, "measured": 98.0, "cell": "ubuntu-latest-py3.15"}
  }
}
```

`report` writes only `coverage.python`, `coverage.javascript` and
`coverage.shell`; every other key keeps its value and order. The file is
rewritten as `json.dumps(indent=2)` output, so its own spacing is not kept and
non-ASCII text is written as `\u` escapes. A record is
`{"floor": F, "measured": M}`, plus `"cell"` where one is recorded; a new
record carries the cell it was measured on. The file may also hold top-level
keys `hysteresis` and `gap`, each a number of at least 0, which override the
inputs of the same name; a key the file lacks leaves the input in force.

The records move only on a push to the default branch whose gate (and, with
`records-only-rise`, its records check) passed and
whose artifacts all passed and cover every shard of every cell. When a language's figure exceeds
its recorded `measured` by more than `hysteresis`, `measured` becomes the
figure (rounded at the default `precision`, truncated above it, as the Gate
section shows it) and `floor` becomes that figure minus `gap`, rounded down to
`precision` decimals and never below 0, unless the old floor is higher, which
stays. A language with no record gets one by the same rule.

With `times`, each file's seconds in its cell, summed from the JUnit reports,
are a new measurement of that file's list. The measurement is appended to the
list only when it is an outlier of it: below the list's smallest value by more
than 25% of that value and more than 1.0 s, or above its largest value by more
than 25% of that value and more than 1.0 s; when the list then holds 11 values,
its oldest is dropped. A measurement inside that band changes nothing. A file
new to the cell starts its list with its measurement, and a file no longer in
the cell is removed from it. The times file is committed only when a list
changed or a file was added or removed, and then only those entries change. A
pull request changes neither file.

When a value changed, both files are committed as one commit,
`chore(ratchet): update the test-ratchet records`, by
`github-actions[bot]` on top of `github.sha`, and pushed over SSH with
`push-key` as a fast-forward:

- the checkout's `HEAD` must be `github.sha`;
- a run whose head commit is that records commit commits nothing;
- when the branch has moved past `github.sha`, nothing is pushed and the step
  passes with a notice; the next push records again;
- when the push is refused while the branch still stands at `github.sha`, the
  step fails;
- without `push-key` the new values go to the job summary and nothing is
  committed.

### Pull request comment

On a `pull_request` event, and on a `workflow_run` event whose triggering run
was a `pull_request`, `report` writes one comment on the pull request and edits
it on later runs. It finds its comment by the hidden marker
`<!-- test-ratchet-report -->` among `github-actions[bot]`'s comments. Per
language the comment lists the merged total, the floor and the result, and the
diff coverage: the pull request's added lines that the coverage measures and how
many of them are covered. When one of those lines is uncovered, a table under
`Uncovered added lines:` has a row per language and file holding one: the
file's added lines that language measures (`Added`), how many of them are
covered (`Covered`) and the uncovered ones as ranges (`Missed lines`).
Languages keep the totals table's order and files are sorted; a file whose
measured added lines are all covered gets no row, and with none uncovered the
comment has no such table. It also names the shards left out.

The added lines come from the API's patches of the pull request's files, so no
checkout of the pull request is needed. The patches compare the pull request's
head with its merge base, while a `pull_request` run's checkout, and so its
coverage, is by default the merge of the pull request into its base: where the
base also changed a file the pull request changes, an added line's number can
point at another measured line. A file that adds lines but whose patch GitHub
does not send (a very large diff) is named as not measured. A file GitHub counts
no added lines for is not named: a binary, a pure rename, a mode change, a
deletion.
GitHub lists at most 3000 files of a pull request, so the added lines of files
past those are not counted. Text from the
artifacts (cell and file names, shards left out, refusals) is shown as inline
code in the comment and the job summary, so no link or mention in it renders;
a `|` in a file name is escaped, so its row keeps its cells. A comment longer
than GitHub accepts keeps the totals table and points at the job summary,
which holds the whole text.

The pull request is resolved from the event, never from an artifact: on a
`workflow_run` event from the event's pull requests or, for a fork, from the
pull requests of this repository that hold the run's head commit. With none or
more than one, or when the pull request's head has moved past the run's,
nothing is commented and a notice says why; on a gating `workflow_run` run
(`workflow-run-gate`) each of these is a refusal with status 2 naming it. When
the token may not write the comment (GitHub answers 403 or 404), the comment's
text goes to the job summary with a notice and the step does not fail for it.

With `comment-check` set to a name, a `workflow_run` run whose triggering run
was a `pull_request` run ends by publishing one completed check run of that
name on the triggering run's head commit, the pull request's head. It
concludes `success` when the comment, or the refusal comment, was created or
edited, and `failure` when the comment could not be written (any refused or
failed write, or a refusal that ended the step before it), when the pull
request lookup failed (an API error, or an event payload missing a field the
lookup reads), or when more than one pull request holds the run's head; its
title and summary say which. When no pull request is the destination (none
holds the run's head, its head moved past the run's, or the fork is deleted)
no check run is published, nor on any other event, nor with the input empty,
nor when the step is refused before the lookup (a `workflow-run-gate`,
`records-only-rise` or `gate-raised-floors` that is neither `true` nor
`false`, an empty `github-token`, an unreadable event payload), nor when the
payload names no head commit (a warning says so). The check run reflects the
comment only: the step's status is what it would be without it, except that
publishing it needs `checks: write` in the job's permissions, a write GitHub
refuses (403 or 404) fails the step with status 2 naming that permission, and
any other error of that write is status 2 as any API error is.

### Fork and Dependabot pull requests

A run for a fork's pull request has a read-only token, so its `report` job puts
the comment in the job summary. The comment workflow above runs after `tests`
completes, with the base repository's token, and runs `report` on the
triggering run's artifacts (`run-id`). It checks out the default branch, never
the pull request, for the git objects the floors are read from, and
`base-ref` is the pull request's base. It runs nothing from the pull request:
the artifacts are only read, and only the comment's text reaches the pull
request. It also runs for Dependabot's pull requests, whose `pull_request`
runs get a read-only token unless the workflow's permissions raise it. The
figures in the comment are those of the triggering run's artifacts, which a
fork's own workflow file produced. This run's status does not reach the pull
request: with `comment-check` the comment's outcome does, as a check run on
the pull request's head, and the job then needs `checks: write` beside
`pull-requests: write`.

On a `workflow_run` event without `workflow-run-gate` a figure below its floor
does not fail the step: the comment and the job summary show the `fail`, and
the pull request's own `report` job is the one that fails. This run's status
lands on the default branch's latest commit, which a fork's artifacts must not
turn red. For the same reason a refusal that the triggering run's data causes (an artifact the
failure modes below name, a gated cell no artifact holds, a pull request the
lookup cannot resolve, a deleted fork) ends this run with status 0 and a
warning, and the refusal is in the job summary and, when the pull request is
known, in its comment. A refusal of the workflow's own inputs or of the base
repository's thresholds file still fails it.

With `workflow-run-gate` set to `true`, a `workflow_run` run whose triggering
run was a `pull_request` run is a gate instead: it fails as a `pull_request`
run's `report` does, with status 1 for a figure below its floor or a recorded
floor nothing measured, and status 2 for each refusal above and for a pull
request no single one of which holds the run's head or whose head has moved
past the run's. The comment, and the refusal comment when the pull request is
known, are still written, and the floors are still read from the pull
request's base (and, with `gate-raised-floors`, from its head as the Gate
section says). Its status lands on the default branch's latest commit, so the
repository must carry it to the pull request's head itself: the check run
`comment-check` publishes there concludes from the comment only, not from this
gate. A `workflow_run` run triggered by any other event is unchanged.

### Failure modes

The step fails with status 2 and one line naming the input, file or value at
fault:

- `cell` holds more than one line, `precision` is not a whole number from 1 to
  6, `hysteresis` or `gap` is not a non-negative number, or
  `workflow-run-gate`, `records-only-rise` or `gate-raised-floors` is not
  `true` or `false` (on every event);
- the thresholds file is not valid JSON of the shape above (`schema_version` 1,
  a `floor` from 0 to 100 per record, a `hysteresis` or `gap` key a finite
  number of at least 0), or `base-ref` (or, on a `workflow_run`
  event, the pull request's base) names no commit in the checkout;
- no `test-ratchet-*` artifact was downloaded, the gated cell has none, the
  records name different cells while `cell` is empty, or several cells are
  present with neither;
- an artifact's manifest or report is not valid or is a symlink, two artifacts
  of one runner in one cell disagree on the shard count, or two are the same
  shard; the job summary names it;
- `github-token` is empty, a request to the GitHub API fails, or the API answers
  an error other than a refused comment write;
- the last repeat of the download fails: an artifact's name is not one
  `upload-artifact` accepts, its digest is not the one the API lists, or it
  cannot be unpacked;
- on a default-branch push, a record's `measured` is not a number from 0 to
  100, the checkout's `HEAD` is not `github.sha`, git or `ssh-agent` cannot
  run, `push-key` is not a key `ssh-add` accepts, or an artifact has no
  `junit.xml` while `times` is set;
- on a gating `workflow_run` run, no single pull request holds the run's head,
  or the pull request's head has moved past the run's;
- with `comment-check` set, GitHub refuses the check run (403 or 404): the job
  needs `checks: write` (on a `workflow_run` run with or without
  `workflow-run-gate`);
- with `records-only-rise` or `gate-raised-floors`, either file compared is not
  of the shape above, the pull request's head or base names no commit (a head
  the checkout lacks is first fetched from `origin`, and refused when the fetch
  fails or brings another commit), or the head and the base have no merge base
  in the checkout; with `records-only-rise`, also a `measured` in either file
  is not a number from 0 to 100, or `github.sha` or the push's previous tip
  names no commit.

On a `workflow_run` event without `workflow-run-gate` these end with status 0
instead, as the fork section says: no artifact downloaded, a gated cell no
artifact holds, each artifact refusal of the fourth item, and a pull request
lookup that fails (an API error or a deleted fork).

The step fails with status 1 when a language is below its floor (with
`gate-raised-floors`, a floor from the head too) or has a record that nothing
measured (the comment is still written; not on a `workflow_run` event without
`workflow-run-gate`), when `records-only-rise` finds a lowered, removed or
loosened record or key, or when the records push is refused while the branch
stands still.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

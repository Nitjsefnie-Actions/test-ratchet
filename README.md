# test-ratchet

**Status: in development. No release exists yet, and nothing below is usable
until the first one ships.**

test-ratchet is planned as three composite GitHub Actions that run a
repository's tests in time-balanced shards with coverage, for Python, Node and
shell, and gate the coverage floor with a ratchet that only tightens.

## Planned sub-actions

| Sub-action | Planned role |
|---|---|
| `report` | Combine the shards' coverage and gate it against a committed floor that only rises. |

Each sub-action's inputs and outputs are documented in its own section, and
the install block below grows as sub-actions land.

## Install

The `test` job below crosses the consumer's own `os` and `python` matrix with
`plan`'s shard list: `fromJSON(needs.plan.outputs.matrix).shard` is one key of
a crossed matrix, and `strategy.matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}`
takes the whole matrix when the shard is the only key.

```yaml
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
        uses: Nitjsefnie-Actions/test-ratchet/plan@<sha>
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
      - uses: Nitjsefnie-Actions/test-ratchet/run@<sha>
        with:
          runner: pytest
          tests: tests/**/test_*.py
          times: .github/test-times.json
          cell: ${{ matrix.os }}-py${{ matrix.python }}
          shard: ${{ matrix.shard }}
          source: src
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
- Predicted seconds in a cell: the file's recorded seconds in that cell. A file
  without a record is predicted as its blob size times the median
  seconds-per-byte of the matched files recorded in that cell. When no matched
  file is recorded in the cell, blob sizes alone set the relative weights. A
  record for a file that no longer matches is ignored.
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
  "schema_version": 1,
  "cells": {
    "ubuntu-latest-py3.15": {
      "tests/test_a.py": 12.5,
      "tests/unit/test_b.py": 3.2
    }
  }
}
```

A cell name is an opaque string the consumer chooses, for example its OS and
interpreter. Keys are repository-relative POSIX paths and values are seconds,
finite and non-negative. A `times` path naming a missing file means no records,
and the log says so once.

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
| `runner` | yes | | `pytest` or `vitest`. |
| `tests` | yes | | Newline-separated glob patterns of the test files, as in `plan`. |
| `times` | no | none | Path, relative to the repository root, of the committed times file, as in `plan`. |
| `cell` | no | `<runner OS>-<major.minor>` | One cell name. The version is that of the `python3` (or `python`) on `PATH` for `pytest` and of the `node` on `PATH` for `vitest`: `Linux-3.15`, `macOS-24.11`. |
| `shard` | no | every matched file | `i/N` from `plan`'s matrix. |
| `source` | no | the repository's own coverage configuration | Comma-separated sources whose coverage is measured: Python source roots for `pytest` (each becomes `--cov=<root>`), include globs for `vitest` (each becomes `--coverage.include=<glob>`). Every comma separates two entries, so write `a/**,b/**`, not a brace glob such as `{a,b}/**`. |
| `shell` | no | none | Newline-separated glob patterns, as in `tests`, of tracked bash scripts to measure when the tests run them. Linux only. |
| `args` | no | none | Extra arguments for the runner, split as shell words and placed after the action's own, so `-n 0` overrides `pytest`'s worker count. |
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
  own `cell` should hold whatever the matrix crosses.

### Artifact

| File | Content |
|---|---|
| `manifest.json` | `{"schema_version": 1, "runner": "pytest"\|"vitest", "cell": "<cell>", "shard": "i/N" or null, "files": [...], "passed": true\|false}`. `files` is the shard's sorted repository-relative paths. Always present. |
| `junit.xml` | The runner's JUnit report with a `file` attribute, a repository-relative path, set on every `<testcase>`. A case that matches none of the shard's files has none, and a warning names it. Absent when the runner wrote no report. |
| `coverage-python.xml` | Cobertura line coverage of Python files, from `pytest`. |
| `coverage-javascript.xml` | Cobertura line coverage of JavaScript files: `vitest`'s v8 report merged with the V8 dumps below, or the dumps alone under `pytest`. |
| `coverage-shell.xml` | Cobertura line coverage of the scripts `shell` matches. |

Each Cobertura file has `<sources><source>.</source></sources>` and
repository-relative POSIX `filename`s with no leading `./`, whatever the runner
OS, so shards from different OSes merge. It holds line hits only, no branch
data. A file is written only for a language that was measured; a file outside
the repository is dropped. The reports are written when the tests fail too.

### pytest

- The action installs the pinned pytest, pytest-cov and pytest-xdist
  (`requirements-test.txt`, hash-checked, wheels only) into the `python3`, else
  `python`, on `PATH`; CPython 3.11 through 3.15 is supported. That interpreter
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
  which cover child processes (`multiprocessing` children included). The step's
  own `COVERAGE_PROCESS_START`, `COVERAGE_FILE` and `COV_CORE_*` variables are
  removed, so a repository's own measuring hook does not run twice.
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

### JavaScript from V8 dumps

Both runners measure every Node process the tests start, through
`NODE_V8_COVERAGE`, which the action sets. Browser coverage joins them: the
action also sets `TEST_RATCHET_V8_DIR`, and the V8 coverage JSON that the tests
write into that directory is converted into the same JavaScript report. A dump
is a list of script entries, or an object whose `result` is one. An entry needs
a `url` and the V8 `functions` ranges. Puppeteer's entries are accepted when
the coverage was started with `includeRawScriptCoverage`.

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
  a source, the script's ranges must span the whole file. An entry that fails
  both is skipped and named in the log, so transpiled code (a TypeScript loader,
  a bundle) is not mis-numbered. So is an entry whose tracked file is not UTF-8.
- A line with a non-blank character counts the hits of the innermost range
  holding the line's first non-blank character, so comment and brace lines are
  measurable lines. Hits of several dumps add.
- The report holds the files a process loaded. A tracked JavaScript file that
  no process loaded is not in it, covered or not, and the dumps are not
  restricted by `source`.

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
`g()` is measured, and the `g()` line is not.

On another runner OS shell is not measured: the log says so once and the run
does not fail for it.

### Limits

Python:

- A Python child is measured when it runs an interpreter whose site-packages
  holds the coverage the action installed (the `python` on `PATH`, or a venv
  that shares it), processes `site` (`python -S` is not measured, `-I` and `-E`
  are) and inherits the environment (a child started with a stripped
  environment is not measured). An unmeasured child's file still appears under
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
  the one the config states: set `root` in the config.
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

Every failure before the tests run exits with status 2, prints one line naming
the input, file or value at fault, and uploads no artifact:

- `runner` is neither `pytest` nor `vitest`, `cell` holds more than one line, or
  `shard` is not `i/N` with 1 <= i <= N <= 256;
- `tests` is empty or no pattern matches a tracked file, or the times file is
  invalid (as in `plan`);
- on Linux, `shell` is set and no pattern matches a tracked file (a pattern that
  matches nothing beside patterns that do only warns);
- `args` cannot be split as shell words;
- `runner: vitest` without `node` on `PATH`, with a config the action refuses
  (see Limits), or with `args` that moved vitest's root.

`runner: vitest` with `vitest` or `@vitest/coverage-v8` not resolvable from the
repository exits 1 naming the package, and uploads no artifact. So does a
`pytest` run whose `pip install` of the toolchain fails (an interpreter that is
externally managed, or one the pinned wheels do not cover): the step ends with
pip's own message before any test runs.

Something the run wrote that cannot be read (a V8 dump that is not valid, a
coverage report or JUnit report that is not valid XML) does not end the run.
The artifact holds everything that could be read: that dump is skipped, or that
report's file is not written. `passed` is `false`, and the step turns red after
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
  vitest configuration`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

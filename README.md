# test-ratchet

**Status: in development. No release exists yet, and nothing below is usable
until the first one ships.**

test-ratchet is planned as three composite GitHub Actions that run a
repository's tests in time-balanced shards with coverage, for Python, Node and
shell, and gate the coverage floor with a ratchet that only tightens.

## Planned sub-actions

| Sub-action | Planned role |
|---|---|
| `run` | Run one shard's tests with coverage measured. |
| `report` | Combine the shards' coverage and gate it against a committed floor that only rises. |

Each sub-action's inputs, outputs and install block are documented here when
it lands.

## plan

`plan` splits the repository's test files into shards of similar run time and
outputs the shard matrix. It only reads the repository and the times file, and
it needs a checkout, no setup step and no installed dependency.

```yaml
jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.plan.outputs.matrix }}
    steps:
      - uses: actions/checkout@<sha>
      - id: plan
        uses: Nitjsefnie-Actions/test-ratchet/plan@<sha>
        with:
          tests: tests/**/test_*.py
          times: .github/test-times.json
          shards: 4
  test:
    needs: plan
    strategy:
      matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
    # A crossed matrix takes one key: os: [...], shard: ${{ fromJSON(needs.plan.outputs.matrix).shard }}
```

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

The times file is JSON, committed to the repository and only read by `plan`:

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

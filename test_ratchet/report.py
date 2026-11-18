"""The `report` sub-action: the gated cell's merged coverage against the floors at base-ref."""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

from test_ratchet import ConfigError, cobertura, collect, ghio, times
from test_ratchet.plan import repository_root

THRESHOLDS = '.github/ci-thresholds.json'
Records = Dict[str, Dict[str, Any]]


def percent(covered: int, measurable: int) -> str:
    """What `coverage report --precision=1` shows for `covered` of `measurable` lines."""
    # coverage 7.17.3 results.py:337-342 and display_covered at :404-422: only a true 0 or
    # 100 shows as such, so the ends clamp to 0.1 and 99.9; otherwise round() on the float.
    near0 = 1.0 / 10 ** 1
    value = (100.0 * covered) / measurable if measurable > 0 else 100.0
    if 0 < value < near0:
        value = near0
    elif (100.0 - near0) < value < 100:
        value = 100.0 - near0
    else:
        value = round(value, 1)
    return f'{value:.1f}'


class Total(NamedTuple):
    language: str
    covered: int
    measurable: int
    shown: str
    floor: Optional[Union[int, float]]

    @property
    def below(self) -> bool:
        return self.floor is not None and float(self.shown) < self.floor


class Report(NamedTuple):
    cell: str
    artifacts: List[collect.Artifact]
    left_out: List[str]
    coverage: Dict[str, cobertura.Lines]
    totals: List[Total]
    source: str


def _input(name: str) -> str:
    return os.environ.get(f'TEST_RATCHET_{name}', '').strip()


def records(data: bytes, label: str) -> Records:
    """The coverage records of a thresholds file for the languages `report` gates."""
    try:
        document = json.loads(data)
    except ValueError as error:
        raise ConfigError(f'{label} is not JSON: {error}') from None
    version = document.get('schema_version') if isinstance(document, dict) else None
    if type(version) is not int or version != 1:
        raise ConfigError(f'{label}: key schema_version must be 1')
    coverage = document.get('coverage', {})
    if not isinstance(coverage, dict):
        raise ConfigError(f'{label}: key coverage must be an object')
    found = {}
    for language in collect.LANGUAGES:
        record = coverage.get(language)
        if record is None:
            continue
        floor = record.get('floor') if isinstance(record, dict) else None
        if isinstance(floor, bool) or not isinstance(floor, (int, float)) \
                or not 0 <= floor <= 100:
            raise ConfigError(f'{label}: key coverage.{language}.floor must be a number from 0'
                              f' to 100, got {floor!r}')
        cell = record.get('cell')
        if 'cell' in record and not (isinstance(cell, str) and cell.isprintable()
                                     and cell.strip()):
            raise ConfigError(f'{label}: key coverage.{language}.cell must be one printable'
                              f' line, got {cell!r}')
        found[language] = record
    return found


def _git(repo: Path, *args: str) -> 'subprocess.CompletedProcess[bytes]':
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=False)


def read_thresholds(repo: Path, path: str, base: str) -> Tuple[str, Records]:
    """Where the records were read, and the records: from commit `base`, or from the checkout
    when `base` is empty; a missing file holds none."""
    name = times.normalise(path)
    if not base:
        label = f'thresholds file {name}'
        try:
            data: Optional[bytes] = Path(repo, name).read_bytes()
        except FileNotFoundError:
            data = None
        except OSError as error:
            raise ConfigError(f'{label} cannot be read: {error}') from None
    else:
        label = f'thresholds file {name} at base-ref {base}'
        commit = _git(repo, 'rev-parse', '--verify', '--quiet', '--end-of-options',
                      base + '^{commit}')
        if commit.returncode:
            raise ConfigError(f'input base-ref {base} names no commit in this checkout: fetch'
                              ' it (actions/checkout fetches one commit unless fetch-depth'
                              ' says otherwise)')
        blob = f'{commit.stdout.decode().strip()}:{name}'
        data = None
        if not _git(repo, 'cat-file', '-e', blob).returncode:
            shown = _git(repo, 'cat-file', 'blob', blob)
            if shown.returncode:
                raise ConfigError(f'{label} is not a file')
            data = shown.stdout
    if data is None:
        ghio.notice(f'{label} does not exist: no language is gated')
        return label, {}
    return label, records(data, label)


def measure() -> Report:
    artifacts_dir = _input('ARTIFACTS')
    if not artifacts_dir:
        raise ConfigError('TEST_RATCHET_ARTIFACTS is not set: the action passes the directory'
                          ' it downloaded the artifacts into')
    repo = repository_root()
    artifacts = collect.read(Path(artifacts_dir))
    source, held = read_thresholds(repo, _input('THRESHOLDS') or THRESHOLDS, _input('BASE_REF'))
    cell = collect.gated_cell(artifacts, _input('CELL'), held, source)
    left_out = collect.left_out(artifacts, cell)
    coverage = collect.merge(artifacts, cell, repo)
    totals = []
    for language, lines in coverage.items():
        covered = sum(1 for hits in lines.values() for count in hits.values() if count)
        measurable = sum(len(hits) for hits in lines.values())
        totals.append(Total(language, covered, measurable, percent(covered, measurable),
                            held.get(language, {}).get('floor')))
    return Report(cell, artifacts, left_out, coverage, totals, source)


def _row(total: Total) -> str:
    result = 'not gated: no record' if total.floor is None else 'fail' if total.below else 'pass'
    floor = 'none' if total.floor is None else total.floor
    return (f'| {total.language} | {total.covered} / {total.measurable} | {total.shown}%'
            f' | {floor} | {result} |')


def summary(report: Report) -> str:
    text = [f'### test-ratchet coverage, cell `{report.cell}`', '']
    if report.totals:
        text += ['| Language | Lines covered | Coverage | Floor | Result |',
                 '|---|---|---|---|---|'] + [_row(total) for total in report.totals]
        text += ['', f'Floors from the {report.source}.']
    else:
        text.append('No coverage was merged.')
    if report.left_out:
        text += ['', 'Left out of the merge:'] + [f'- {item}' for item in report.left_out]
    return '\n'.join(text) + '\n'


def main() -> int:
    try:
        result = measure()
        ghio.add_summary(summary(result))
    except ConfigError as error:
        print(f'test-ratchet report: {error}', file=sys.stderr)
        return 2
    for item in result.left_out:
        ghio.warning(f'left out of the merge: {item}')
    for total in result.totals:
        print(f'test-ratchet report: {total.language} {total.shown}% ({total.covered} /'
              f' {total.measurable} lines), floor '
              + ('none' if total.floor is None else str(total.floor)))
    below = [total for total in result.totals if total.below]
    for total in below:
        print(f'test-ratchet report: {total.language} coverage {total.shown}% is below the'
              f' floor {total.floor} ({result.source})', file=sys.stderr)
    return 1 if below else 0

"""The `report` sub-action: the gated cell's merged coverage against the floors at base-ref."""
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

from test_ratchet import ConfigError, cobertura, collect, ghio, times
from test_ratchet.plan import repository_root

THRESHOLDS = '.github/ci-thresholds.json'
Records = Dict[str, Dict[str, Any]]


def percent(covered: int, measurable: int, precision: int = 1) -> str:
    """What `coverage report --precision=1` shows for `covered` of `measurable` lines, or
    above precision 1 the exact value truncated."""
    if precision > 1:
        # In integers: a division in floats or Decimal can round up before it truncates.
        scale = 10 ** precision
        whole, part = divmod(covered * 100 * scale // measurable if measurable > 0
                             else 100 * scale, scale)
        return f'{whole}.{part:0{precision}d}'
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
    precision: int = 1

    @property
    def below(self) -> bool:
        if self.floor is None:
            return False
        if self.precision == 1:
            return float(self.shown) < self.floor
        # 0 measurable lines count as 100: 0 < floor * 0 never holds.
        return Decimal(self.covered) * 100 < Decimal(repr(self.floor)) * self.measurable


class Report(NamedTuple):
    cell: str
    artifacts: List[collect.Artifact]
    left_out: List[str]
    coverage: Dict[str, cobertura.Lines]
    totals: List[Total]
    source: str
    unmeasured: Dict[str, Union[int, float]]  # recorded floors no merged coverage meets


def env_input(name: str) -> str:
    return os.environ.get(f'TEST_RATCHET_{name}', '').strip()


def flag(name: str) -> bool:
    """Input `name`, 'true' or 'false', default 'false'."""
    text = env_input(name.upper().replace('-', '_')) or 'false'
    if text not in ('true', 'false'):
        raise ConfigError(f"input {name} must be 'true' or 'false', got {text!r}")
    return text == 'true'


def read_precision() -> int:
    """Input precision: the decimals coverage is shown and recorded at, 1 to 6, default 1."""
    text = env_input('PRECISION') or '1'
    if not (text.isascii() and text.isdigit() and 1 <= int(text) <= 6):
        raise ConfigError(f'input precision must be a whole number from 1 to 6, got {text!r}')
    return int(text)


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


def find_commit(repo: Path, ref: str) -> str:
    """The commit `ref` names in the checkout, '' when it names none."""
    done = _git(repo, 'rev-parse', '--verify', '--quiet', '--end-of-options', ref + '^{commit}')
    return '' if done.returncode else done.stdout.decode().strip()


def commit(repo: Path, ref: str, what: str) -> str:
    """The commit `ref` names in the checkout, refused naming `what` when it names none."""
    found = find_commit(repo, ref)
    if not found:
        raise ConfigError(f'{what} {ref} names no commit in this checkout: fetch it'
                          ' (actions/checkout fetches one commit unless fetch-depth says'
                          ' otherwise)')
    return found


def blob(repo: Path, sha: str, name: str, label: str) -> Optional[bytes]:
    """File `name` at commit `sha`, None when absent."""
    path = f'{sha}:{name}'
    if _git(repo, 'cat-file', '-e', path).returncode:
        return None
    shown = _git(repo, 'cat-file', 'blob', path)
    if shown.returncode:
        raise ConfigError(f'{label} is not a file')
    return shown.stdout


def read_thresholds(repo: Path, path: str, base: str,
                    origin: str = 'input base-ref') -> Tuple[str, Records]:
    """Where the records were read ('' for no file), and the records: from commit `base`, or
    from the checkout when `base` is empty."""
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
        data = blob(repo, commit(repo, base, origin), name, label)
    if data is None:
        ghio.notice(f'{label} does not exist: no language is gated')
        return '', {}
    return label, records(data, label)


def measure(base: str, precision: int) -> Report:
    """The report, gated by the floors at input base-ref, else at `base`, else in the checkout."""
    artifacts_dir = env_input('ARTIFACTS')
    if not artifacts_dir:
        raise ConfigError('TEST_RATCHET_ARTIFACTS is not set: the action passes the directory'
                          ' it downloaded the artifacts into')
    repo = repository_root()
    artifacts = collect.read(Path(artifacts_dir))
    given = env_input('BASE_REF')
    source, held = read_thresholds(repo, env_input('THRESHOLDS') or THRESHOLDS, given or base,
                                   'input base-ref' if given else "the pull request's base")
    cell = collect.gated_cell(artifacts, env_input('CELL'), held, source)
    left_out = collect.left_out(artifacts, cell)
    coverage = collect.merge(artifacts, cell, repo)
    totals = []
    for language, lines in coverage.items():
        covered = sum(1 for hits in lines.values() for count in hits.values() if count)
        measurable = sum(len(hits) for hits in lines.values())
        totals.append(Total(language, covered, measurable,
                            percent(covered, measurable, precision),
                            held.get(language, {}).get('floor'), precision))
    unmeasured = {language: record['floor'] for language, record in held.items()
                  if language not in coverage}
    return Report(cell, artifacts, left_out, coverage, totals, source, unmeasured)


def code(text: str) -> str:
    """`text` as one inline code span, so no link, markup or mention in it renders."""
    return '`' + ''.join(char if char.isprintable() else ' '
                         for char in text.replace('`', "'")) + '`'


def row(total: Total) -> str:
    result = 'not gated: no record' if total.floor is None else 'fail' if total.below else 'pass'
    floor = 'none' if total.floor is None else total.floor
    return (f'| {total.language} | {total.covered} / {total.measurable} | {total.shown}%'
            f' | {floor} | {result} |')


def unmeasured_row(language: str, floor: Union[int, float]) -> str:
    return (f'| {language} | nothing measured | none | {floor} | fail: recorded floor but'
            ' nothing measured |')


def summary(report: Report) -> str:
    text = [f'### test-ratchet coverage, cell {code(report.cell)}', '']
    if report.totals or report.unmeasured:
        text += ['| Language | Lines covered | Coverage | Floor | Result |',
                 '|---|---|---|---|---|'] + [row(total) for total in report.totals]
        text += [unmeasured_row(*item) for item in report.unmeasured.items()]
        text += ['', f'Floors from the {report.source}.'] * bool(report.source)
    else:
        text.append('No coverage was merged.')
    if report.left_out:
        text += ['', 'Left out of the merge:'] + [f'- {code(item)}' for item in report.left_out]
    return '\n'.join(text) + '\n'


def main() -> int:
    # Both build on this module's report, so they are imported once this one exists.
    from test_ratchet import comment, ratchet, rise  # pylint: disable=import-outside-toplevel
    # Out of the environment before the first child process: only the push may hold the key.
    key = os.environ.pop('TEST_RATCHET_PUSH_KEY', '').strip()
    token = os.environ.pop('TEST_RATCHET_GITHUB_TOKEN', '').strip()
    try:
        gate, rises = flag('workflow-run-gate'), flag('records-only-rise')
    except ConfigError as error:
        return _refused(error)
    gating = False
    try:
        gating = comment.gating(gate)
        pull, lookup = comment.target(token, gating), None
    except ConfigError as error:
        # Refused after the summary below is written: the totals are shown whatever happens.
        pull, lookup = None, error
    try:
        ratchet.amounts()
        precision = read_precision()
        result = measure(pull.base if pull else '', precision)
        ghio.add_summary(summary(result))
    except ConfigError as error:
        if lookup:
            print(f'test-ratchet report: {lookup}', file=sys.stderr)
        return _refused(error, pull, gating)
    if lookup:
        return _refused(lookup, None, gating)
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
    for language, floor in result.unmeasured.items():
        print(f'test-ratchet report: {language} has a recorded floor {floor} but nothing'
              f' measured it ({result.source})', file=sys.stderr)
    failing = bool(below or result.unmeasured)
    try:
        if pull:
            comment.publish(pull, result)
        if failing and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run' and not gating:
            # A comment-only run: the pull request's own run gates, and this one would fail
            # the default branch's commit on a fork's artifacts.
            print('test-ratchet report: a workflow_run event only comments: the gate fails'
                  " the pull request's own run")
            return 0
        lowered, basis = rise.lowered(token, pull, gating) if rises else ([], '')
        for line in lowered:
            print(f'test-ratchet report: {line} ({basis})', file=sys.stderr)
        if lowered:
            ghio.add_summary(f'### test-ratchet records lowered\n\nThe {basis}:\n\n'
                             + ''.join(f'- {line}\n' for line in lowered))
        return 1 if failing or lowered else ratchet.ratchet(result, key, precision)
    except ConfigError as error:
        return _refused(error, None, gating)


def _refused(error: ConfigError, pull: Any = None, gating: bool = False) -> int:
    from test_ratchet import comment
    print(f'test-ratchet report: {error}', file=sys.stderr)
    if isinstance(error, collect.ArtifactError):
        ghio.add_summary('### test-ratchet report refused the artifacts\n\n'
                         + code(str(error)) + '\n')
    if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_run' \
            or not isinstance(error, (collect.ArtifactError, comment.Unresolved)):
        return 2
    if not gating:
        # The pull request's run caused it, and this run's status lands on the default branch.
        ghio.warning(f'a workflow_run event only comments, so it does not fail on: {error}')
    if isinstance(error, comment.Unresolved):
        ghio.add_summary('### test-ratchet report found no pull request\n\n'
                         + code(str(error)) + '\n')
    try:
        if pull:
            comment.refusal(pull, str(error))
    except ConfigError as failed:
        print(f'test-ratchet report: {failed}', file=sys.stderr)
        return 2
    return 2 if gating else 0

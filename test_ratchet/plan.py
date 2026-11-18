"""The `plan` sub-action: the shard matrix for a repository's test files."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from test_ratchet import ConfigError, ghio, split, times


def repository_root() -> Path:
    """The git top level of the working directory, where tracked paths are rooted."""
    try:
        found = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True,
                               text=True, check=False)
    except OSError as error:
        raise ConfigError(f'cannot run git to find the repository root: {error}') from error
    if found.returncode:
        raise ConfigError('the working directory is not inside a git repository: '
                          + found.stderr.strip())
    return Path(found.stdout.strip())


def _count_inputs() -> Tuple[Optional[int], Optional[float]]:
    shards = os.environ.get('TEST_RATCHET_SHARDS', '').strip()
    minutes = os.environ.get('TEST_RATCHET_MAX_MINUTES', '').strip()
    if bool(shards) == bool(minutes):
        raise ConfigError('inputs shards and max-minutes conflict: '
                          + ('both are set' if shards else 'neither is set')
                          + ', give exactly one of them')
    if shards:
        if not re.fullmatch('[0-9]+', shards):
            raise ConfigError(f'input shards must be an integer of at least 1, got {shards!r}')
        return int(shards), None
    try:
        return None, float(minutes)
    except ValueError:
        raise ConfigError(f'input max-minutes must be a positive number, got {minutes!r}') \
            from None


def _totals(name: str, files: Sequence[str], sizes: Dict[str, int],
            cells: times.Cells, count: int) -> str:
    predictions = split.predict(files, sizes, cells, name)
    measured = split.has_records(files, cells, name)
    shards = split.assign(predictions, count)
    label = f'cell {name}' if measured else f'cell {name} (blob size in bytes, no records)'
    return label + ': ' + ', '.join(
        f'{index}/{count} {sum(predictions[path] for path in shard):.1f}'
        f'{"s" if measured else ""} (files: {len(shard)})'
        for index, shard in enumerate(shards, 1))


def plan() -> None:
    patterns = split.parse_patterns(os.environ.get('TEST_RATCHET_TESTS', ''))
    shards, minutes = _count_inputs()
    repo = repository_root()
    tracked = split.tracked_files(repo)
    files = split.match(patterns, tracked)
    times_path = os.environ.get('TEST_RATCHET_TIMES', '').strip()
    cells = times.load(Path(repo, times_path) if times_path else None)
    named: List[str] = list(dict.fromkeys(
        line.strip() for line in os.environ.get('TEST_RATCHET_CELL', '').splitlines()
        if line.strip())) or sorted(cells)
    count, logs = split.count_for(files, tracked, cells, named, shards, minutes)
    for line in logs:
        print(line)
    for name in named:
        print(_totals(name, files, tracked, cells, count))
    ghio.set_output('matrix', json.dumps({'shard': [f'{index}/{count}'
                                                    for index in range(1, count + 1)]},
                                         separators=(',', ':')))
    ghio.set_output('count', str(count))


def main() -> int:
    try:
        plan()
    except ConfigError as error:
        print(f'test-ratchet plan: {error}', file=sys.stderr)
        return 2
    return 0

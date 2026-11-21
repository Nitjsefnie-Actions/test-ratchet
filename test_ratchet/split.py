"""The shard split: a pure function of the matched files, their blob sizes, the times
file, one cell and the shard count, so `plan` and every `run` compute the same one."""
import heapq
import math
import re
import statistics
import subprocess
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from test_ratchet import ConfigError, ghio, times

__all__ = ['ConfigError', 'MAX_SHARDS', 'parse_patterns', 'tracked_files', 'match', 'predict',
           'has_records', 'assign', 'count_for', 'parse_shard', 'shard_files']

MAX_SHARDS = 256
# Seconds, or bytes without records: zero weights then take one shard each, not all the first.
MIN_PREDICTION = 0.001
Repo = Union[str, Path]


def parse_patterns(text: str, name: str = 'tests') -> List[str]:
    patterns = [times.normalise(line.strip()) for line in text.splitlines() if line.strip()]
    if not patterns:
        raise ConfigError(f'input {name} names no glob pattern')
    return patterns


def tracked_files(repo: Repo) -> Dict[str, int]:
    """Path -> git blob size of every regular file committed at HEAD."""
    try:
        listing = subprocess.run(['git', '-C', str(repo), 'ls-tree', '--full-tree', '-r', '-l',
                                  '-z', 'HEAD'], capture_output=True, check=False)
    except OSError as error:
        raise ConfigError(f'cannot run git to list the files tracked in {repo}: {error}') from error
    if listing.returncode:
        stderr = listing.stderr.decode('utf-8', 'replace').strip()
        raise ConfigError(f'git ls-tree failed in {repo}: {stderr}')
    files = {}
    for entry in listing.stdout.split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, kind, _, size = meta.split()
        if kind == b'blob' and mode != b'120000':
            files[path.decode('utf-8', 'surrogateescape')] = int(size)
    return files


def _class(text: str, start: int) -> Tuple[Optional[str], int]:
    """Regex for the `[...]` opening at `start`, and the index after it; None if unclosed."""
    end = start + 1
    negate = end < len(text) and text[end] in '!^'
    end += negate
    end += end < len(text) and text[end] == ']'
    end = text.find(']', end)
    if end < 0:
        return None, start + 1
    body = text[start + 1 + negate:end]
    body = ''.join('\\' + char if char in '\\[]^&~|' else char for char in body)
    return f'(?!/)[{"^" if negate else ""}{body}]', end + 1


def _segment(text: str) -> str:
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if char == '*':
            out.append('[^/]*')
        elif char == '?':
            out.append('[^/]')
        elif char == '[':
            regex, after = _class(text, index - 1)
            out.append(re.escape(char) if regex is None else regex)
            index = after
        else:
            out.append(re.escape(char))
    return ''.join(out)


def _compile(pattern: str, name: str) -> 're.Pattern[str]':
    parts = pattern.split('/')
    out = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == '**':
            out.append('.*' if last else '(?:[^/]+/)*')
        else:
            out.append(_segment(part) + ('' if last else '/'))
    try:
        return re.compile(''.join(out))
    except re.error as error:
        raise ConfigError(f'input {name} pattern {pattern} is invalid: {error}') from error


def match(patterns: Sequence[str], tracked: Dict[str, int], name: str = 'tests') -> List[str]:
    matched = set()
    empty = []
    for pattern in patterns:
        regex = _compile(pattern, name)
        hits = [path for path in tracked if regex.fullmatch(path)]
        if not hits:
            empty.append(pattern)
        matched.update(hits)
    if not matched:
        raise ConfigError(f'input {name} matched no file git tracks: ' + ', '.join(empty))
    if empty:
        ghio.warning(f'input {name} patterns matched no file git tracks: ' + ', '.join(empty))
    return sorted(matched)


def has_records(files: Sequence[str], cells: times.Cells, cell: str) -> bool:
    recorded = cells.get(cell, {})
    return any(path in recorded for path in files)


def predict(files: Sequence[str], sizes: Dict[str, int], cells: times.Cells,
            cell: str) -> Dict[str, float]:
    recorded = cells.get(cell, {})
    known = {path: statistics.median(recorded[path]) for path in files if path in recorded}
    rates = [seconds / sizes[path] for path, seconds in known.items() if sizes[path]]
    if not known:
        weights = {path: float(sizes[path]) for path in files}
    elif rates:
        rate = statistics.median(rates)
        weights = {path: known.get(path, sizes[path] * rate) for path in files}
    else:
        # Every recorded file is empty, so no rate exists: unrecorded files take the median time.
        typical = statistics.median(known.values())
        weights = {path: known.get(path, typical) for path in files}
    return {path: max(weight, MIN_PREDICTION) for path, weight in weights.items()}


def assign(predictions: Mapping[str, float], count: int) -> List[List[str]]:
    totals = [(0.0, index) for index in range(count)]
    shards: List[List[str]] = [[] for _ in range(count)]
    for path, seconds in sorted(predictions.items(), key=lambda item: (-item[1], item[0])):
        total, index = heapq.heappop(totals)
        shards[index].append(path)
        heapq.heappush(totals, (total + seconds, index))
    return [sorted(shard) for shard in shards]


def _fits(predictions: Dict[str, float], count: int, limit: float) -> bool:
    return all(len(shard) == 1 or math.fsum(predictions[path] for path in shard) <= limit
               for shard in assign(predictions, count))


def _fewest(predictions: Dict[str, float], limit: float) -> int:
    """No count below this can fit: each file over the limit needs a shard of its own."""
    over = sum(1 for seconds in predictions.values() if seconds > limit)
    rest = math.fsum(seconds for seconds in predictions.values() if seconds <= limit)
    return min(max(1, over + math.ceil(rest / limit)), len(predictions))


def _count_for_minutes(files: Sequence[str], sizes: Dict[str, int], cells: times.Cells,
                       names: Sequence[str], max_minutes: float, logs: List[str]) -> int:
    measured = [name for name in names if has_records(files, cells, name)]
    if not measured:
        logs.append('max-minutes has nothing to measure: no named cell records a matched file,'
                    ' so the count is 1 (cells: ' + (', '.join(names) or 'none') + ')')
        return 1
    limit = max_minutes * 60
    predicted = [predict(files, sizes, cells, name) for name in measured]
    for name, predictions in zip(measured, predicted):
        for path in sorted(path for path, seconds in predictions.items() if seconds > limit):
            logs.append(f'{path} alone is predicted at {predictions[path]:.1f}s in cell {name},'
                        f' above max-minutes {max_minutes:g}: it gets a shard of its own')
    count = max(_fewest(predictions, limit) for predictions in predicted)
    while not all(_fits(predictions, count, limit) for predictions in predicted):
        count += 1
    return count


def count_for(files: Sequence[str], sizes: Dict[str, int], cells: times.Cells,
              names: Sequence[str], shards: Optional[int] = None,
              max_minutes: Optional[float] = None) -> Tuple[int, List[str]]:
    """The shard count N and the log lines explaining how it was reached."""
    if (shards is None) == (max_minutes is None):
        raise ConfigError('inputs shards and max-minutes conflict: give exactly one of them')
    logs: List[str] = []
    if shards is not None:
        if shards < 1:
            raise ConfigError(f'input shards must be at least 1, got {shards}')
        needed = shards
    else:
        assert max_minutes is not None
        if not (math.isfinite(max_minutes) and max_minutes > 0):
            raise ConfigError(f'input max-minutes must be a positive number, got {max_minutes}')
        needed = _count_for_minutes(files, sizes, cells, names, max_minutes, logs)
    cap, reason = min((len(files), 'the matched file count'),
                      (MAX_SHARDS, "GitHub's 256-job matrix limit"))
    if needed > cap:
        logs.append(f'shard count capped at {cap} by {reason}; {needed} were needed')
        needed = cap
    return needed, logs


def parse_shard(text: str) -> Tuple[int, int]:
    found = re.fullmatch(r'([0-9]+)/([0-9]+)', text.strip())
    index, count = (int(found.group(1)), int(found.group(2))) if found else (0, 0)
    if not 1 <= index <= count <= MAX_SHARDS:
        raise ConfigError(f'input shard must be i/N with 1 <= i <= N <= {MAX_SHARDS},'
                          f' got {text!r}')
    return index, count


def shard_files(repo: Repo, patterns_text: str, times_path: Optional[str], cell: str,
                shard: str) -> List[str]:
    """The files of shard "i/N"; a relative `times_path` is read from `repo`."""
    index, count = parse_shard(shard)
    tracked = tracked_files(repo)
    files = match(parse_patterns(patterns_text), tracked)
    cells = times.load(Path(repo, times_path) if times_path else None)
    return assign(predict(files, tracked, cells, cell), count)[index - 1]

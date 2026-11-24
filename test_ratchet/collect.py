"""The artifacts `run` uploaded, as `report` downloads them: read, sorted by cell, merged."""
import contextlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from test_ratchet import ConfigError, cobertura, ghio, split

LANGUAGES = ('python', 'javascript', 'shell')
RUNNERS = ('pytest', 'vitest', 'node')


class ArtifactError(ConfigError):
    """A refusal the downloaded artifacts' own data causes."""


class Artifact(NamedTuple):
    name: str
    path: Path
    runner: str
    cell: str
    shard: Tuple[int, int]
    files: List[str]
    passed: bool


def payload(artifact: str, path: Path) -> Optional[Path]:
    """`path`, None when absent; a symlink is refused, whatever it points at."""
    if path.is_symlink():
        raise ArtifactError(f'artifact {artifact}: {path.name} is a symlink')
    return path if path.exists() else None


def _manifest(name: str, folder: Path) -> Artifact:
    path = payload(name, folder / 'manifest.json')
    if path is None:
        raise ArtifactError(f'artifact {name} has no manifest.json')
    try:
        data: Any = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ArtifactError(f'artifact {name}: manifest.json is not readable UTF-8 JSON: {error}') \
            from None
    if not isinstance(data, dict):
        raise ArtifactError(f'artifact {name}: manifest.json is not an object')

    def wrong(key: str, wanted: str) -> ConfigError:
        return ArtifactError(f'artifact {name}: manifest.json key {key} must be {wanted},'
                             f' got {data.get(key)!r}')

    version, runner, cell, shard, files, passed = (data.get(key) for key in (
        'schema_version', 'runner', 'cell', 'shard', 'files', 'passed'))
    if type(version) is not int or version != 1:
        raise wrong('schema_version', '1')
    if runner not in RUNNERS:
        raise wrong('runner', f'{", ".join(RUNNERS[:-1])} or {RUNNERS[-1]}')
    if not (isinstance(cell, str) and cell.isprintable() and cell.strip()):
        raise wrong('cell', 'one printable line')
    index = (1, 1) if shard is None else None
    if isinstance(shard, str):
        with contextlib.suppress(ConfigError):
            index = split.parse_shard(shard)
    if index is None:
        raise wrong('shard', 'i/N or null')
    if not (isinstance(files, list) and all(isinstance(item, str) for item in files)):
        raise wrong('files', 'a list of paths')
    if not isinstance(passed, bool):
        raise wrong('passed', 'true or false')
    return Artifact(name, folder, runner, cell, index, files, passed)


def read(root: Path) -> List[Artifact]:
    """Every artifact under `root`: one per subdirectory, or `root` itself when it is the only
    one, which download unpacks there."""
    if not root.is_dir():
        raise ConfigError(f'the artifacts directory {root} is not a directory')
    if (root / 'manifest.json').exists():
        return [_manifest('(the only artifact downloaded)', root)]
    found = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            raise ArtifactError(f'artifact {entry.name} is a symlink')
        found.append(_manifest(entry.name, entry))
    return found


def gated_cell(artifacts: Sequence[Artifact], given: str,
               records: Mapping[str, Mapping[str, Any]], label: str) -> str:
    """The input `cell`, else the records' cell, else the only cell the artifacts hold."""
    cells = sorted({artifact.cell for artifact in artifacts})
    if '\n' in given:
        raise ConfigError(f'input cell names one cell, got {given!r}')
    named = {language: record['cell'] for language, record in records.items()
             if 'cell' in record}
    if not given and len(set(named.values())) > 1:
        raise ConfigError(f'{label}: the records name different cells: '
                          + ', '.join(f'{language} {cell}' for language, cell in named.items())
                          + ': set input cell')
    others = [f'{language} ({cell})' for language, cell in named.items() if cell != given]
    if given and others:
        ghio.notice(f'input cell {given} overrides the cell the records name: '
                    + ', '.join(others) + f': their floors gate cell {given}')
    given = given or next(iter(named.values()), '')
    if not given and len(cells) > 1:
        raise ConfigError('input cell is empty and no record names a cell, but the artifacts'
                          ' hold several cells: ' + ', '.join(cells) + ': set input cell')
    given = given or (cells[0] if cells else '')
    if given not in cells:
        raise ArtifactError((f'no downloaded artifact is of cell {given}: the artifacts hold'
                             ' cells ' + ', '.join(cells)) if cells
                            else 'no test-ratchet artifact was downloaded')
    return given


def left_out(artifacts: Sequence[Artifact], cell: str) -> List[str]:
    """Each artifact of `cell` whose tests failed and each shard of its i/N set with no
    artifact, per runner; a runner's shard counts that disagree and repeated shards are
    refused."""
    held = [artifact for artifact in artifacts if artifact.cell == cell]
    counts: Dict[str, int] = {}
    for runner in sorted({artifact.runner for artifact in held}):
        mine = [artifact for artifact in held if artifact.runner == runner]
        if len({artifact.shard[1] for artifact in mine}) > 1:
            raise ArtifactError(f'the {runner} artifacts of cell {cell} disagree on the shard'
                                ' count: ' + ', '.join(f'{artifact.name} ({artifact.shard[0]}/'
                                                       f'{artifact.shard[1]})'
                                                       for artifact in mine))
        counts[runner] = mine[0].shard[1]
    seen: Dict[Tuple[str, int], str] = {}
    for artifact in held:
        key = (artifact.runner, artifact.shard[0])
        if key in seen:
            raise ArtifactError(f'artifacts {seen[key]} and {artifact.name} are both {key[0]}'
                                f' shard {key[1]}/{artifact.shard[1]} of cell {cell}: give each'
                                ' matrix job its own cell')
        seen[key] = artifact.name
    return ([f'{artifact.name}: its tests failed' for artifact in held if not artifact.passed]
            + [f'{runner} shard {index}/{count} of cell {cell}: no artifact'
               for runner, count in counts.items()
               for index in range(1, count + 1) if (runner, index) not in seen])


def merge(artifacts: Sequence[Artifact], cell: str, repo: Path) -> Dict[str, cobertura.Lines]:
    """Per language measured, the coverage of `cell`'s passed artifacts merged."""
    merged = {}
    for language in LANGUAGES:
        reports = []
        for artifact in artifacts:
            if artifact.cell == cell and artifact.passed:
                path = payload(artifact.name, artifact.path / f'coverage-{language}.xml')
                if path:
                    try:
                        reports.append(cobertura.parse(path, repo))
                    except ConfigError as error:
                        raise ArtifactError(f'artifact {artifact.name}: {error}') from None
        if reports:
            merged[language] = cobertura.merge(reports)
    return merged

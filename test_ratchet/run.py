"""The `run` sub-action: one shard's tests under coverage, written as the artifact `report`
reads."""
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from test_ratchet import ConfigError, cobertura, ghio, junit, shellcov, split, v8
from test_ratchet.plan import repository_root

MEASURERS = ('COVERAGE_PROCESS_START', 'COVERAGE_FILE')
PLUGIN = Path(__file__).with_name('plugin')
# Only the pytest process imports the plugin: no environment variable carries it.
# Like -m, the absolute directory leads the path; -c would put '' there, which follows chdir.
BOOT = ('import os, sys; sys.path[0] = sys.path[0] or os.getcwd();'
        ' sys.path.append(sys.argv.pop(1)); import pytest; raise SystemExit(pytest.main())')
# _exit saves a multiprocessing child's data (it ends in os._exit), sigterm a pool worker's;
# a fork keeps measuring.
PATCH = '[run]\npatch = subprocess, _exit\nsigterm = true\n'


def _input(name: str) -> str:
    return os.environ.get(f'TEST_RATCHET_{name}', '').strip()


def _cell() -> str:
    cell = _input('CELL')
    if '\n' in cell:
        raise ConfigError(f'input cell names one cell, got {cell!r}')
    runner_os = os.environ.get('RUNNER_OS') or platform.system()
    return cell or f'{runner_os}-{sys.version_info[0]}.{sys.version_info[1]}'


def child_env(added: Mapping[str, str]) -> Dict[str, str]:
    """This step's environment without the measurers a parent armed, plus `added`."""
    env = {key: value for key, value in os.environ.items()
           if key not in MEASURERS and not key.startswith('COV_CORE_')}
    env.update(added)
    return env


FAILURES = {
    4: "pytest exited 4, a usage or configuration error: check input args, the repository's"
       ' pytest configuration and its conftest files',
    5: 'pytest exited 5, no tests were collected: check input args'}


def _pytest(repo: Path, files: List[str], work: Path, env: Mapping[str, str]) -> int:
    try:
        extra = shlex.split(_input('ARGS'))
    except ValueError as error:
        raise ConfigError(f'input args cannot be split as shell words: {error}') from None
    sources = [root.strip() for root in _input('SOURCE').split(',') if root.strip()]
    (work / 'files.txt').write_text(''.join(path + '\n' for path in files), encoding='utf-8')
    # Coverage lays this over the repository's config (config.py, COVERAGE_FORCE_CONFIG).
    # shortcut: it replaces a repository's [run] patch, reaches coverage the tests run, and is
    # all concurrency = multiprocessing children read; merge configs if a repository needs it.
    (work / 'patch.ini').write_text(PATCH, encoding='utf-8')
    # A shard's coverage is partial, so the repository's fail_under is report's to apply.
    command = [sys.executable, '-c', BOOT, str(PLUGIN), '-p', 'test_ratchet_shard',
               f'--test-ratchet-shard={work / "files.txt"}', '-n', 'auto',
               *([f'--cov={root}' for root in sources] or ['--cov']), '--cov-fail-under=0',
               f'--cov-report=xml:{work / "coverage.xml"}', f'--junitxml={work / "junit.xml"}',
               '-o', 'junit_family=xunit1', *extra, f'@{work / "files.txt"}']
    env = dict(env, COVERAGE_FILE=str(work / '.coverage'),
               COVERAGE_FORCE_CONFIG=str(work / 'patch.ini'))
    print('test-ratchet run: ' + shlex.join(command), flush=True)
    return subprocess.run(command, cwd=repo, env=env, check=False).returncode


def _write(path: Path, lines: cobertura.Lines) -> None:
    if lines:
        path.write_text(cobertura.dump(lines), encoding='utf-8')


def run() -> None:
    temp = os.environ.get('RUNNER_TEMP')
    if not temp:
        raise ConfigError('RUNNER_TEMP is not set: run this inside a GitHub Actions step')
    runners = {'pytest': _pytest}
    runner = _input('RUNNER')
    if runner not in runners:
        raise ConfigError(f'input runner must be {" or ".join(runners)}, got {runner!r}')
    repo = repository_root()
    cell = _cell()
    shard: Optional[str] = None
    if _input('SHARD'):
        shard = '{}/{}'.format(*split.parse_shard(_input('SHARD')))
    files = split.shard_files(repo, os.environ.get('TEST_RATCHET_TESTS', ''),
                              _input('TIMES') or None, cell, shard or '1/1')
    name = _input('NAME') or f'{cell}-{shard.replace("/", "_") if shard else "all"}'
    top = Path(tempfile.mkdtemp(prefix='test-ratchet-run-', dir=temp))
    artifact, work = top / 'artifact', top / 'work'
    node_dumps, browser_dumps, shell_logs = work / 'node-v8', work / 'browser-v8', work / 'shell'
    for directory in (artifact, node_dumps, browser_dumps):
        directory.mkdir(parents=True)
    added = {'NODE_V8_COVERAGE': str(node_dumps), 'TEST_RATCHET_V8_DIR': str(browser_dumps)}
    shell = os.environ.get('TEST_RATCHET_SHELL', '')
    if shell.strip():
        added.update(shellcov.arm(shell_logs, os.environ))
    print(f'test-ratchet run: cell {cell}, shard {shard or "all"}: ' + ', '.join(files),
          flush=True)
    returncode = runners[runner](repo, files, work, child_env(added))
    passed = returncode == 0
    if (work / 'coverage.xml').is_file():
        _write(artifact / 'coverage-python.xml', cobertura.parse(work / 'coverage.xml', repo))
    dumps = sorted(node_dumps.glob('*.json')) + sorted(browser_dumps.glob('*.json'))
    if dumps:
        _write(artifact / 'coverage-javascript.xml', v8.convert(dumps, repo))
    if shell.strip():
        _write(artifact / 'coverage-shell.xml', shellcov.collect(repo, shell, shell_logs))
    if (work / 'junit.xml').is_file():
        (artifact / 'junit.xml').write_text(junit.rewrite(work / 'junit.xml', files),
                                            encoding='utf-8')
    manifest = {'schema_version': 1, 'runner': runner, 'cell': cell, 'shard': shard,
                'files': files, 'passed': passed}
    (artifact / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n',
                                            encoding='utf-8')
    ghio.set_output('name', 'test-ratchet-' + re.sub(r'[^A-Za-z0-9._-]', '_', name))
    ghio.set_output('dir', str(artifact))
    ghio.set_output('passed', 'true' if passed else 'false')
    ghio.set_output('failure', '' if passed else FAILURES.get(
        returncode, 'the tests failed: their reports are in the uploaded artifact'))


def main() -> int:
    try:
        run()
    except ConfigError as error:
        print(f'test-ratchet run: {error}', file=sys.stderr)
        return 2
    return 0

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
from pathlib import Path, PurePath
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from test_ratchet import ConfigError, cobertura, ghio, junit, shellcov, split, v8
from test_ratchet.plan import repository_root

MEASURERS = ('COVERAGE_PROCESS_START', 'COVERAGE_FILE')
PLUGIN = Path(__file__).with_name('plugin')
# Only the pytest process imports the plugin: no environment variable carries it.
# Like -m, the absolute directory leads the path; -c would put '' there, which follows chdir.
BOOT = ('import os, sys; sys.path[0] = sys.path[0] or os.getcwd();'
        ' sys.path.append(sys.argv.pop(1)); import pytest; raise SystemExit(pytest.main())')
# _exit saves a multiprocessing child's data (it ends in os._exit); a fork keeps measuring.
PATCH = '[run]\npatch = subprocess, _exit\n'
VITEST_CONFIGS = [name + ext for name in ('vitest.config', 'vite.config')
                  for ext in ('.ts', '.mts', '.cts', '.js', '.mjs', '.cjs')]
# Wraps the repository's config, `base`: each shard file, exact (vitest's path filters match
# substrings), takes every mode vitest 5.1.0's globs give it (index.Di8au7q4.js:3653, 6333,
# 6622), typecheck even when off, else runs as a test. The floor is report's. vitest globs
# from the root left once args (:3741) and plugins are laid over the config (:8796).
VITEST_CONFIG = r"""import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
const vitest = createRequire(%(vitest)s);
const root = %(root)s;
const files = %(files)s;
export default async (env) => {
  const config = { ...(await (typeof base === 'function' ? base(env) : base)) };
  const test = config.test ?? {};
  const refuse = (why) => {
    writeFileSync(%(refused)s, why);
    throw new Error(`test-ratchet run: ${why}`);
  };
  if (test.projects || test.workspace) {
    refuse("the repository's vitest config sets test.projects, whose own include a shard"
      + ' cannot cut to its files');
  }
  const stated = resolve(test.root || config.root || '');
  const check = (final) => {
    if (resolve(final.root) !== stated) {
      refuse(`vitest's root is ${final.root}, moved by input args or a plugin from the ${stated}`
        + " its config states, which each shard file's mode was read from: set root in the"
        + " repository's vitest config instead");
    }
    const dir = resolve(final.test.dir || final.root);
    if (dir !== resolve(root)) {
      refuse(`vitest's test dir is ${dir}, moved by a plugin from the ${resolve(root)}`
        + " the action sets, which the shard's files are named from");
    }
  };
  config.plugins = [...(config.plugins ?? []), { name: 'test-ratchet-run', enforce: 'post',
    configResolved: { order: 'post', handler: check } }];
  const { glob, escapePath } = vitest('tinyglobby');
  const defaults = vitest('vitest/config').configDefaults;
  const checker = { ...defaults.typecheck, ...test.typecheck };
  const exclude = test.exclude ?? defaults.exclude;
  const cwd = resolve(root, test.dir || test.root || config.root || '');
  const held = async (include, ignore) => new Set((await glob(include, {
    dot: true, cwd, ignore, expandDirectories: false,
  })).map((file) => resolve(cwd, file).replaceAll('\\', '/')));
  const [run, source, typed] = await Promise.all([held(test.include ?? defaults.include, exclude),
    held(test.includeSource ?? [], exclude), held(checker.include, checker.exclude)]);
  const modes = files.map((file) => {
    const path = `${root}/${file}`;
    const found = [run.has(path), source.has(path)
      && readFileSync(path, 'utf-8').includes('import.meta.vitest'), typed.has(path)];
    return found.some(Boolean) ? found : [true, false, false];
  });
  const escape = %(escape)s;
  const exact = (mode) => files.filter((_, at) => modes[at][mode]).map(escape);
  config.test = { ...test, include: exact(0), includeSource: exact(1),
                  typecheck: { ...test.typecheck, include: exact(2) },
                  coverage: { ...test.coverage, thresholds: {} } };
  return config;
};
"""
# vitest's package.json and bin, then @vitest/coverage-v8's package.json, from the repository.
NODE = ("const r = p => { try { return require.resolve(p, { paths: [process.argv[1]] }); }"
        " catch { return ''; } };"
        " const v = r('vitest/package.json');"
        " console.log(JSON.stringify([process.versions.node, v, v && require('path').join(v, '..',"
        " require(v).bin.vitest), r('@vitest/coverage-v8/package.json')]));")


class Missing(Exception):
    """A package the runner needs is not installed: the run fails (exit 1) naming it."""


def _input(name: str) -> str:
    return os.environ.get(f'TEST_RATCHET_{name}', '').strip()


def _cell(version: str) -> str:
    cell = _input('CELL')
    if '\n' in cell:
        raise ConfigError(f'input cell names one cell, got {cell!r}')
    runner_os = os.environ.get('RUNNER_OS') or platform.system()
    return cell or f'{runner_os}-{".".join(version.split(".")[:2])}'


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


def _args() -> List[str]:
    try:
        return shlex.split(_input('ARGS'))
    except ValueError as error:
        raise ConfigError(f'input args cannot be split as shell words: {error}') from None


def _sources() -> List[str]:
    return [root.strip() for root in _input('SOURCE').split(',') if root.strip()]


def _node(repo: Path) -> Tuple[str, str, str]:
    """node's version and the repository's vitest package.json and entry, checked before
    anything runs."""
    try:
        found = subprocess.run(['node', '-e', NODE, str(repo)], capture_output=True, text=True,
                               check=False)
    except OSError as error:
        raise ConfigError(f'input runner vitest needs node on PATH: {error}') from None
    try:
        version, package, vitest, coverage = json.loads(found.stdout)
    except ValueError:
        said = (found.stderr or found.stdout).strip()[-300:]
        raise ConfigError(f'node exited {found.returncode} without naming the vitest packages'
                          f' of {repo}: {said!r}') from None
    for name, path in (('vitest', package), ('@vitest/coverage-v8', coverage)):
        if not path:
            raise Missing(f'input runner vitest needs the package {name}, which node cannot'
                          f' resolve from {repo}: install it in the repository')
    return version, package, vitest


def _uri(path: PurePath) -> str:
    """A file URL for an absolute POSIX or Windows path, whichever OS runs this."""
    posix = path.as_posix()
    return 'file://' + ('' if posix.startswith('/') else '/') + quote(posix, safe='/:')


def vitest_config(repo: PurePath, base: Optional[PurePath], package: PurePath,
                  files: List[str], refused: PurePath, windows: bool = os.name == 'nt') -> str:
    """The config vitest runs with: `base` is the repository's own config file, if any."""
    # tinyglobby's own escape on Windows, where a name holds no backslash; elsewhere every
    # glob character, and the backslash that escape leaves alone before one.
    escape = 'escapePath' if windows else r"(file) => file.replace(/[\\()[\]{}*?|!+@]/g, '\\$&')"
    return ((f'import base from {json.dumps(_uri(base))};\n' if base else 'const base = {};\n')
            + VITEST_CONFIG % {'vitest': json.dumps(_uri(package)), 'escape': escape,
                               'root': json.dumps(repo.as_posix()), 'files': json.dumps(files),
                               'refused': json.dumps(str(refused))})


def _vitest(repo: Path, files: List[str], work: Path, env: Mapping[str, str],
            node: Tuple[str, str, str]) -> Tuple[int, Path, str]:
    base = next((repo / name for name in VITEST_CONFIGS if (repo / name).is_file()), None)
    (work / 'vitest.config.mjs').write_text(
        vitest_config(repo, base, Path(node[1]), files, work / 'refused'), encoding='utf-8')
    report, extra = work / 'vitest-coverage', _args()
    command = ['node', node[2], 'run', f'--config={work / "vitest.config.mjs"}', f'--dir={repo}',
               '--coverage.enabled', '--coverage.provider=v8', '--coverage.reportOnFailure',
               *[f'--coverage.include={glob}' for glob in _sources()],
               '--coverage.reporter=cobertura', f'--coverage.reportsDirectory={report}',
               '--reporter=default', '--reporter=junit',
               f'--outputFile.junit={work / "junit.xml"}', *extra]
    print('test-ratchet run: ' + shlex.join(command), flush=True)
    returncode = subprocess.run(command, cwd=repo, env=env, check=False).returncode
    if (work / 'refused').exists():
        raise ConfigError((work / 'refused').read_text(encoding='utf-8'))
    if not (work / 'junit.xml').exists():
        # vitest refuses its command line, a repeated single-valued option among them, unstarted.
        return returncode, report / 'cobertura-coverage.xml', (
            f'vitest exited {returncode} before running: check input args, which cannot repeat'
            " --config, --dir or the coverage options the action sets, and the repository's"
            ' vitest configuration')
    if extra and '<testcase' not in (work / 'junit.xml').read_text(encoding='utf-8'):
        ghio.warning(f'input args {shlex.join(extra)} left none of the shard\'s files to run')
    return returncode, report / 'cobertura-coverage.xml', ''


def _pytest(repo: Path, files: List[str], work: Path,
            env: Mapping[str, str]) -> Tuple[int, Path, str]:
    extra = _args()
    sources = _sources()
    (work / 'files.txt').write_text(''.join(path + '\n' for path in files), encoding='utf-8')
    # Coverage lays this over the repository's config (config.py, COVERAGE_FORCE_CONFIG).
    # shortcut: it replaces a repository's [run] patch, reaches coverage the tests run, and is
    # all concurrency = multiprocessing children read; merge configs if a repository needs it.
    # A Pool worker ended by terminate(), the with exit too, is unmeasured (close() and join()
    # measure it): the sigterm setting that saves it can hang such a pool.
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
    returncode = subprocess.run(command, cwd=repo, env=env, check=False).returncode
    ignored = work / 'files.txt.ignored'
    if ignored.is_file():
        names = ', '.join(ignored.read_text(encoding='utf-8').splitlines())
        ghio.warning(f'input args names {names}: ignored, a shard runs exactly its files'
                     ' (-k narrows them)')
    return returncode, work / 'coverage.xml', FAILURES.get(returncode, '')


def _write(path: Path, lines: cobertura.Lines) -> None:
    if lines:
        path.write_text(cobertura.dump(lines), encoding='utf-8')


def run() -> None:
    temp = os.environ.get('RUNNER_TEMP')
    if not temp:
        raise ConfigError('RUNNER_TEMP is not set: run this inside a GitHub Actions step')
    runners = ('pytest', 'vitest')
    runner = _input('RUNNER')
    if runner not in runners:
        raise ConfigError(f'input runner must be {" or ".join(runners)}, got {runner!r}')
    repo = repository_root()
    node = _node(repo) if runner == 'vitest' else None
    cell = _cell(node[0] if node else platform.python_version())
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
    armed = shellcov.arm(shell_logs, os.environ) if shell.strip() else {}
    scripts = split.match(split.parse_patterns(shell, 'shell'), split.tracked_files(repo),
                          'shell') if armed else []
    added.update(armed)
    print(f'test-ratchet run: cell {cell}, shard {shard or "all"}: ' + ', '.join(files),
          flush=True)
    env = child_env(added)
    returncode, report, meaning = _vitest(repo, files, work, env, node) if node else _pytest(
        repo, files, work, env)
    unread: List[str] = []
    measured: cobertura.Lines = {}
    try:
        measured = cobertura.parse(report, repo) if report.is_file() else {}
    except ConfigError as error:
        unread.append(str(error))
    dumps = sorted(node_dumps.glob('*.json')) + sorted(browser_dumps.glob('*.json'))
    javascript = v8.convert(dumps, repo, unread) if dumps else {}
    _write(artifact / 'coverage-python.xml', {} if node else measured)
    _write(artifact / 'coverage-javascript.xml',
           cobertura.merge([measured, javascript]) if node else javascript)
    try:
        _write(artifact / 'coverage-shell.xml', shellcov.collect(repo, shell, shell_logs, scripts))
    except ConfigError as error:
        unread.append(str(error))
    # vitest creates its report as it starts, so a run that dies early leaves it empty.
    if (work / 'junit.xml').is_file() and (work / 'junit.xml').stat().st_size:
        try:
            (artifact / 'junit.xml').write_text(junit.rewrite(work / 'junit.xml', files),
                                                encoding='utf-8')
        except ConfigError as error:
            unread.append(str(error))
    passed = returncode == 0 and not unread
    failed = [] if returncode == 0 else [
        meaning or 'the tests failed: their reports are in the uploaded artifact']
    manifest = {'schema_version': 1, 'runner': runner, 'cell': cell, 'shard': shard,
                'files': files, 'passed': passed}
    (artifact / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n',
                                            encoding='utf-8')
    ghio.set_output('name', 'test-ratchet-' + re.sub(r'[^A-Za-z0-9._-]', '_', name))
    ghio.set_output('dir', str(artifact))
    ghio.set_output('passed', 'true' if passed else 'false')
    ghio.set_output('failure', ghio.escape('; '.join(failed + unread)))


def main() -> int:
    try:
        run()
    except ConfigError as error:
        print(f'test-ratchet run: {error}', file=sys.stderr)
        return 2
    except Missing as error:
        print(f'test-ratchet run: {error}', file=sys.stderr)
        return 1
    return 0

"""The default branch's records: raise-only coverage records, per-file times, one commit."""
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from test_ratchet import ConfigError, collect, ghio, junit, report, times

SUBJECT = 'chore(ratchet): update the test-ratchet records'
BOT_NAME = 'github-actions[bot]'
BOT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'
# A file's seconds move when they change by more than both: a share of the record and seconds.
TIMES_SHARE = 0.25
TIMES_SECONDS = 1.0
# GitHub's published SSH host keys (the REST meta endpoint's ssh_keys), pinned.
KNOWN_HOSTS = ''.join(f'github.com {key}\n' for key in (
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl',
    'ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7'
    'opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=',
    'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gv'
    'wnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWC'
    'qBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7Gid'
    'jMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4'
    'dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXm'
    'PUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk='))


def _decimal(name: str, default: str) -> Decimal:
    text = report.env_input(name.upper()) or default
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = Decimal('NaN')
    if not value.is_finite() or value < 0:
        raise ConfigError(f'input {name} must be a non-negative number, got {text!r}')
    return value


def amounts() -> Dict[str, Decimal]:
    """Inputs hysteresis and gap."""
    return {name: _decimal(name, '1.5') for name in report.AMOUNTS}


def percentage(value: Any, name: str) -> Any:
    """`value`, the `measured` of record `name`, refused unless a number from 0 to 100."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ConfigError(f'{name}.measured must be a number from 0 to 100, got {value!r}')
    return value


def implied_floor(measured: Decimal, gap: Decimal, precision: int) -> Decimal:
    """The floor `gap` below `measured`, rounded down to `precision` decimals, never below 0."""
    # Clamped before the quantize, which a huge gap would push past the context's digits.
    return max(Decimal(0), measured - gap).quantize(Decimal(10) ** -precision, ROUND_FLOOR)


def moved(record: Optional[Dict[str, Any]], shown: str, cell: str, hysteresis: Decimal,
          gap: Decimal, name: str = 'coverage record',
          precision: int = 1) -> Optional[Dict[str, Any]]:
    """The record after a run measuring `shown`, or None while it stays."""
    new = Decimal(shown)
    floor = implied_floor(new, gap, precision)
    if record is None:
        return {'floor': float(floor), 'measured': float(new), 'cell': cell}
    old = percentage(record.get('measured'), name)
    if new - Decimal(repr(old)) <= hysteresis:
        return None
    raised = {**record, 'measured': float(new)}
    raised['floor'] = record['floor'] if Decimal(repr(record['floor'])) >= floor else float(floor)
    if 'cell' in record:
        raised['cell'] = cell
    return raised


def times_moved(old: Optional[Dict[str, float]], new: Dict[str, float]) -> bool:
    return old is None or set(old) != set(new) or any(
        abs(new[name] - old[name]) > max(TIMES_SHARE * old[name], TIMES_SECONDS) for name in new)


def _number(value: Any) -> str:
    return 'none' if value is None else str(value)


def _thresholds(path: Path, name: str, result: report.Report,
                precision: int) -> Tuple[Optional[str], List[str]]:
    label = f'thresholds file {name}'
    data = path.read_bytes() if path.is_file() else None
    held, keys = report.thresholds(data, label) if data is not None else ({}, {})
    document: Dict[str, Any] = json.loads(data) if data is not None else {'schema_version': 1}
    coverage = document.setdefault('coverage', {})
    amount = {**amounts(), **keys}
    lines = []
    for total in result.totals:
        old = held.get(total.language)
        new = moved(old, total.shown, result.cell, amount['hysteresis'], amount['gap'],
                    f'{label}: key coverage.{total.language}', precision)
        if new is not None:
            coverage[total.language] = new
            before = old or {}
            lines.append(f'{total.language}: measured {_number(before.get("measured"))} ->'
                         f' {new["measured"]}, floor {_number(before.get("floor"))} ->'
                         f' {new["floor"]}')
    return (json.dumps(document, indent=2) + '\n' if lines else None), lines


def _times(path: Path, artifacts: List[collect.Artifact]) -> Tuple[Optional[str], List[str]]:
    recorded = times.load(path)
    measured: Dict[str, Dict[str, float]] = {}
    for artifact in artifacts:
        found = collect.payload(artifact.name, artifact.path / 'junit.xml')
        if found is None:
            raise collect.ArtifactError(f'artifact {artifact.name} has no junit.xml: its times'
                                        ' cannot be recorded')
        try:
            seconds = junit.seconds_per_file(found)
        except ConfigError as error:
            raise collect.ArtifactError(f'artifact {artifact.name}: {error}') from None
        cell = measured.setdefault(artifact.cell, {})
        for name in map(times.normalise, artifact.files):
            cell[name] = cell.get(name, 0.0) + seconds.get(name, 0.0)
    changed = {cell: new for cell, new in measured.items()
               if times_moved(recorded.get(cell), new)}
    if not changed:
        return None, []
    document = (json.loads(path.read_bytes()) if path.is_file()
                else {'schema_version': 1, 'cells': {}})
    for cell, new in changed.items():
        document['cells'][cell] = {name: round(new[name], 2) for name in sorted(new)}
    return json.dumps(document, indent=2) + '\n', [
        f'times of cell {cell}: {len(new)} files, {math.fsum(new.values()):.1f} s'
        for cell, new in changed.items()]


def _default_branch() -> str:
    path = os.environ.get('GITHUB_EVENT_PATH', '')
    try:
        with open(path, encoding='utf-8') as handle:
            branch = json.load(handle)['repository']['default_branch']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f'GITHUB_EVENT_PATH {path!r} names no event payload with'
                          f' repository.default_branch: {error!r}') from None
    if not isinstance(branch, str):
        raise ConfigError(f'GITHUB_EVENT_PATH {path!r}: repository.default_branch is {branch!r}')
    return branch


def _git(where: Path, *args: str, data: Optional[bytes] = None,
         env: Optional[Dict[str, str]] = None) -> 'subprocess.CompletedProcess[bytes]':
    return subprocess.run(['git', '-C', str(where), *args], input=data, env=env,
                          capture_output=True, check=False)


def _out(done: 'subprocess.CompletedProcess[bytes]') -> str:
    if done.returncode:
        raise ConfigError(f'git {done.args[3]} failed: {done.stderr.decode(errors="replace")}')
    return done.stdout.decode().strip()


def _agent(key: str) -> Tuple['subprocess.Popen[str]', str]:
    """An ssh-agent of this process's own, in the foreground, holding `key`."""
    try:
        agent = subprocess.Popen(['ssh-agent', '-D', '-s'], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
    except OSError as error:
        raise ConfigError(f'cannot run ssh-agent for input push-key: {error}') from None
    try:
        found = re.match(r'SSH_AUTH_SOCK=([^;]+);',
                         agent.stdout.readline() if agent.stdout else '')
        if found is None:
            raise ConfigError('ssh-agent did not start')
        added = subprocess.run(['ssh-add', '-'], input=key if key.endswith('\n') else key + '\n',
                               env=dict(os.environ, SSH_AUTH_SOCK=found.group(1)),
                               capture_output=True, text=True, check=False)
        if added.returncode:
            raise ConfigError(f'input push-key is not an SSH private key ssh-add accepts:'
                              f' {added.stderr.strip()}')
    except BaseException as error:
        agent.kill()
        agent.communicate()
        if isinstance(error, OSError):
            raise ConfigError(f'cannot run ssh-add for input push-key: {error}') from None
        raise
    return agent, found.group(1)


def _moved_past(sha: str, branch: str, tip: str) -> int:
    message = (f'{branch} moved past {sha} before the records push (now {tip}): this run pushes'
               ' nothing and a later push run records again')
    ghio.notice(message)
    ghio.add_summary(f'Records not committed: {message}.\n')
    return 0


def _push(files: Dict[str, str], branch: str, sha: str, key: str) -> int:
    remote = os.environ.get('TEST_RATCHET_PUSH_REMOTE') or (
        f'git@github.com:{os.environ.get("GITHUB_REPOSITORY", "")}.git')
    with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP') or None) as scratch:
        hosts = Path(scratch, 'known_hosts')
        hosts.write_text(KNOWN_HOSTS, encoding='utf-8')
        agent, socket = _agent(key)
        try:
            env = dict(os.environ, SSH_AUTH_SOCK=socket, GIT_SSH_COMMAND=(
                'ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o'
                f' GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile={shlex.quote(str(hosts))}'),
                GIT_AUTHOR_NAME=BOT_NAME, GIT_AUTHOR_EMAIL=BOT_EMAIL,
                GIT_COMMITTER_NAME=BOT_NAME, GIT_COMMITTER_EMAIL=BOT_EMAIL)
            # A fresh bare repository: nothing fetched is checked out, imported or run.
            repo = Path(scratch, 'repo.git')
            _out(_git(Path(scratch), 'init', '-q', '--bare', str(repo), env=env))

            def tip() -> str:
                _out(_git(repo, 'fetch', '-q', '--no-tags', '--depth=1', remote,
                          f'refs/heads/{branch}', env=env))
                return _out(_git(repo, 'rev-parse', 'FETCH_HEAD', env=env))

            fetched = tip()
            if fetched != sha:
                return _moved_past(sha, branch, fetched)
            _out(_git(repo, 'read-tree', sha, env=env))
            for name, text in files.items():
                blob = _out(_git(repo, 'hash-object', '-w', '--stdin', data=text.encode(),
                                 env=env))
                _out(_git(repo, 'update-index', '--add', '--cacheinfo',
                          f'100644,{blob},{name}', env=env))
            tree = _out(_git(repo, 'write-tree', env=env))
            commit = _out(_git(repo, 'commit-tree', tree, '-p', sha, '-m', SUBJECT, env=env))
            pushed = _git(repo, 'push', '-q', remote, f'{commit}:refs/heads/{branch}', env=env)
            if not pushed.returncode:
                print(f'test-ratchet report: pushed the records commit {commit} to {branch}')
                return 0
            now = tip()
            if now != sha:
                return _moved_past(sha, branch, now)
            print(f'test-ratchet report: the records push was refused while {branch} stood still'
                  f' at {sha}: {pushed.stderr.decode(errors="replace").strip()}', file=sys.stderr)
            return 1
        finally:
            agent.terminate()
            agent.communicate()


def ratchet(result: report.Report, key: str, precision: int) -> int:
    """On a push to the default branch whose artifacts all passed: move the records."""
    if os.environ.get('GITHUB_EVENT_NAME') != 'push':
        return 0
    branch = _default_branch()
    if os.environ.get('GITHUB_REF') != f'refs/heads/{branch}':
        return 0
    left = [item for cell in sorted({artifact.cell for artifact in result.artifacts})
            for item in collect.left_out(result.artifacts, cell)]
    if left:
        print('test-ratchet report: records unchanged: ' + '; '.join(left))
        return 0
    sha = os.environ.get('GITHUB_SHA', '')
    repo = report.repository_root()
    head = _out(_git(repo, 'rev-parse', 'HEAD'))
    if head != sha:
        raise ConfigError(f'the checkout HEAD {head} is not github.sha {sha} (GITHUB_SHA):'
                          ' check out github.sha so the records describe the tested tree')
    if _out(_git(repo, 'log', '-1', '--format=%s', 'HEAD')) == SUBJECT:
        print('test-ratchet report: HEAD is the records commit: records unchanged')
        return 0
    name = times.normalise(report.env_input('THRESHOLDS') or report.THRESHOLDS)
    text, lines = _thresholds(Path(repo, name), name, result, precision)
    files = {name: text} if text else {}
    times_name = times.normalise(report.env_input('TIMES'))
    if times_name:
        text, more = _times(Path(repo, times_name), result.artifacts)
        files.update({times_name: text} if text else {})
        lines += more
    if not files:
        print('test-ratchet report: no record moved')
        return 0
    ghio.add_summary('Records moved:\n\n' + ''.join(f'- {line}\n' for line in lines))
    if not key:
        ghio.add_summary('Records not committed: input push-key is empty.\n')
        return 0
    return _push(files, branch, sha, key)

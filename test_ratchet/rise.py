"""Input records-only-rise: a change to the thresholds file may raise coverage records, never lower
or remove one."""
import base64
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from test_ratchet import ConfigError, collect, ghio, report, times
from test_ratchet.comment import Target, _event
from test_ratchet.ratchet import _default_branch, _git, _number


def _measured(records: report.Records, label: str) -> Dict[str, Any]:
    """Each record's `measured`, None where it has none."""
    found = {}
    for language, record in records.items():
        value = record.get('measured')
        if 'measured' in record and (isinstance(value, bool) or not isinstance(
                value, (int, float)) or not 0 <= value <= 100):
            raise ConfigError(f'{label}: key coverage.{language}.measured must be a number from 0'
                              f' to 100, got {value!r}')
        found[language] = value
    return found


def compare(old: report.Records, new: report.Records, labels: Tuple[str, str]) -> List[str]:
    """One line per record `new` removes and per floor or measured it lowers."""
    was, now = _measured(old, labels[0]), _measured(new, labels[1])
    found = []
    for language in (language for language in collect.LANGUAGES if language in old):
        key, floor = f'coverage.{language}', old[language]['floor']
        if language not in new:
            found.append(f'{key} removed: it held floor {floor} and measured'
                         f' {_number(was[language])}')
            continue
        if new[language]['floor'] < floor:
            found.append(f'{key}.floor lowered from {floor} to {new[language]["floor"]}')
        if was[language] is not None and (now[language] is None
                                          or now[language] < was[language]):
            found.append(f'{key}.measured lowered from {was[language]} to'
                         f' {_number(now[language])}')
    return found


def _commit(repo: Path, sha: str) -> str:
    done = _git(repo, 'rev-parse', '--verify', '--quiet', '--end-of-options', sha + '^{commit}')
    return '' if done.returncode else done.stdout.decode().strip()


def _known(repo: Path, sha: str, what: str) -> str:
    commit = _commit(repo, sha)
    if not commit:
        raise ConfigError(f'{what} {sha} names no commit in this checkout: fetch it'
                          ' (actions/checkout fetches one commit unless fetch-depth says'
                          ' otherwise)')
    return commit


def _head(repo: Path, sha: str, number: Any, token: str) -> str:
    """The pull request's head commit, fetched from origin's refs/pull/<number>/head when the
    checkout lacks it (a fork's, on a workflow_run run): only its objects, no local branch."""
    commit = _commit(repo, sha)
    if commit:
        return commit
    # Through the environment, as actions/checkout configures it, so no argv holds the token.
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
    secret = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    env = dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0=f'http.{server}/.extraheader',
               GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {secret}')
    ref = f'refs/pull/{number}/head'
    done = _git(repo, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin', ref,
                env=env)
    if done.returncode:
        raise ConfigError(f"the pull request's head {sha} is not in the checkout, and fetching"
                          f" origin's {ref} failed: {done.stderr.decode(errors='replace')}")
    fetched = _commit(repo, 'FETCH_HEAD')
    if fetched != sha:
        raise ConfigError(f"origin's {ref} is {fetched}, not the pull request's head {sha}")
    return fetched


def _merge_base(repo: Path, head: str, base: str, number: Any, token: str) -> Tuple[str, str]:
    new = _head(repo, head, number, token)
    base = _known(repo, base, "the pull request's base")
    found = _git(repo, 'merge-base', base, new)
    if found.returncode:
        raise ConfigError(f"the pull request's head {new} and base {base} have no merge base in"
                          ' this checkout: fetch their history')
    return found.stdout.decode().strip(), new


def lowered(token: str, pull: Optional[Target], gating: bool) -> Tuple[List[str], str]:
    """The records this run's thresholds file lowers or removes, and what was compared: a pull
    request's head against its merge base with the base, a default-branch push against the
    previous tip. Any other run compares nothing."""
    name = os.environ.get('GITHUB_EVENT_NAME', '')
    repo = report.repository_root()
    what = 'the merge base'
    if name == 'workflow_run' and gating and pull:
        old, new = _merge_base(repo, pull.head, pull.base, pull.number, token)
    elif name == 'pull_request':
        try:
            event = _event(name)['pull_request']
            head, base, number = event['head']['sha'], event['base']['sha'], event['number']
        except (KeyError, TypeError) as error:
            raise ConfigError(f'GITHUB_EVENT_PATH: the pull_request payload has no {error}') \
                from None
        old, new = _merge_base(repo, str(head), str(base), number, token)
    elif name == 'push' and os.environ.get('GITHUB_REF') == f'refs/heads/{_default_branch()}':
        before = _event(name).get('before')
        if not isinstance(before, str):
            raise ConfigError(f'GITHUB_EVENT_PATH: the push payload has no before, got'
                              f' {before!r}')
        if set(before) == {'0'}:
            ghio.notice('records-only-rise: the push created the branch, so there is no'
                        ' earlier thresholds file to compare')
            return [], ''
        new = _known(repo, os.environ.get('GITHUB_SHA', ''), 'GITHUB_SHA')
        old, what = _known(repo, before, "the push's before"), 'the previous tip'
    else:
        return [], ''
    path = times.normalise(report.env_input('THRESHOLDS') or report.THRESHOLDS)
    labels = (f'thresholds file {path} at {what} {old}', f'thresholds file {path} at {new}')
    files = [report.records(data, label) if data is not None else {}
             for data, label in ((report.blob(repo, old, path, labels[0]), labels[0]),
                                 (report.blob(repo, new, path, labels[1]), labels[1]))]
    return compare(files[0], files[1], labels), f'{labels[1]} against {labels[0]}'

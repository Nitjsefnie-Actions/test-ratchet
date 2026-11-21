"""Input records-only-rise: coverage records may rise, never fall or go, and a floor keeps up
with its measured."""
import base64
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from test_ratchet import ConfigError, collect, ghio, report, times
from test_ratchet.comment import Target, _event
from test_ratchet.ratchet import _default_branch, _git, _number, amounts, implied_floor, \
    percentage

Thresholds = Tuple[report.Records, Dict[str, Decimal]]


def _measured(records: report.Records, label: str) -> Dict[str, Any]:
    """Each record's `measured`, None where it has none."""
    return {language: percentage(record['measured'], f'{label}: key coverage.{language}')
            if 'measured' in record else None for language, record in records.items()}


def compare(old_file: Thresholds, new_file: Thresholds, labels: Tuple[str, str],
            inputs: Dict[str, Decimal], precision: int) -> List[str]:
    """One line per record `new` removes, per floor or measured it lowers, per new or changed
    measured whose floor is below the one a raise would give, and per AMOUNTS key it removes,
    raises or adds above this run's input."""
    (old, old_keys), (new, new_keys) = old_file, new_file
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
    gap = new_keys.get('gap', inputs['gap'])
    for language in (language for language in collect.LANGUAGES if language in new):
        measured = now[language]
        if measured is None or (language in old and measured == was[language]):
            continue
        floor = implied_floor(Decimal(repr(measured)), gap, precision)
        if Decimal(repr(new[language]['floor'])) < floor:
            found.append(f'coverage.{language}.measured changed from'
                         f' {_number(was.get(language))} to {measured} with floor'
                         f' {new[language]["floor"]} below its implied floor {floor}')
    for name in report.AMOUNTS:
        if name in old_keys and name not in new_keys:
            found.append(f'{name} removed: it held {old_keys[name]}')
        elif name in old_keys and new_keys[name] > old_keys[name]:
            found.append(f'{name} raised from {old_keys[name]} to {new_keys[name]}')
        elif name not in old_keys and name in new_keys and new_keys[name] > inputs[name]:
            found.append(f'{name} added at {new_keys[name]}, above the input {name}'
                         f' {inputs[name]}')
    return found


def _head(repo: Path, sha: str, number: Any, token: str) -> str:
    """The pull request's head commit, fetched from origin's refs/pull/<number>/head when the
    checkout lacks it (a fork's, on a workflow_run run): only its objects, no local branch."""
    found = report.find_commit(repo, sha)
    if found:
        return found
    # In the environment, never argv; the empty value first drops a header a checkout that
    # persisted its credentials left, so one Authorization is sent.
    key = f"http.{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/.extraheader"
    secret = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    env = dict(os.environ, GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0=key, GIT_CONFIG_VALUE_0='',
               GIT_CONFIG_KEY_1=key, GIT_CONFIG_VALUE_1=f'AUTHORIZATION: basic {secret}')
    ref = f'refs/pull/{number}/head'
    done = _git(repo, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', 'origin', ref,
                env=env)
    if done.returncode:
        raise ConfigError(f"the pull request's head {sha} is not in the checkout, and fetching"
                          f" origin's {ref} failed: {done.stderr.decode(errors='replace')}")
    fetched = report.find_commit(repo, 'FETCH_HEAD')
    if fetched != sha:
        raise ConfigError(f"origin's {ref} is {fetched}, not the pull request's head {sha}")
    return fetched


def _merge_base(repo: Path, head: str, base: str, number: Any, token: str) -> Tuple[str, str]:
    new = _head(repo, head, number, token)
    base = report.commit(repo, base, "the pull request's base")
    found = _git(repo, 'merge-base', base, new)
    if found.returncode:
        raise ConfigError(f"the pull request's head {new} and base {base} have no merge base in"
                          ' this checkout: fetch their history')
    return found.stdout.decode().strip(), new


def lowered(token: str, pull: Optional[Target], gating: bool,
            precision: int) -> Tuple[List[str], str]:
    """What this run's thresholds file loosens (`compare`), and what was compared: a pull
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
        new = report.commit(repo, os.environ.get('GITHUB_SHA', ''), 'GITHUB_SHA')
        old, what = report.commit(repo, before, "the push's before"), 'the previous tip'
    else:
        return [], ''
    path = times.normalise(report.env_input('THRESHOLDS') or report.THRESHOLDS)
    labels = (f'thresholds file {path} at {what} {old}', f'thresholds file {path} at {new}')
    files = [report.thresholds(data, label) if data is not None else ({}, {})
             for data, label in ((report.blob(repo, old, path, labels[0]), labels[0]),
                                 (report.blob(repo, new, path, labels[1]), labels[1]))]
    return (compare(files[0], files[1], labels, amounts(), precision),
            f'{labels[1]} against {labels[0]}')
